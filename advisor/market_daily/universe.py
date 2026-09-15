"""Build, persist and select deterministic historical Shanghai-Shenzhen universes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Protocol

from advisor.market_daily.contracts import MarketSecurity
from advisor.market_daily.providers.exchanges import ExchangeSecurity, UniverseSourceError
from advisor.market_daily.repository import MarketDailyRepository


class UniverseError(RuntimeError):
    """Official universe inputs cannot be sealed into one trustworthy snapshot."""


class UniverseAdapter(Protocol):
    def fetch(self) -> tuple[ExchangeSecurity, ...]: ...


_STATUS_PRIORITY = {"active": 0, "suspended": 1, "delisted": 2}
_HASH_DOMAIN = b"a-hunter:market-daily-universe:v1\0"


@dataclass(frozen=True)
class UniverseSnapshot:
    """A stable snapshot of all eligible historical common A shares."""

    securities: tuple[MarketSecurity, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.securities, tuple) or not self.securities:
            raise UniverseError("股票池快照不能为空")
        if any(not isinstance(security, MarketSecurity) for security in self.securities):
            raise UniverseError("股票池包含无效证券")
        ordered = tuple(sorted(self.securities, key=lambda security: security.code))
        if len({security.code for security in ordered}) != len(ordered):
            raise UniverseError("股票池存在重复代码")
        canonical = [
            {
                "code": security.code,
                "content_hash": security.content_hash,
            }
            for security in ordered
        ]
        digest = hashlib.sha256(
            _HASH_DOMAIN
            + json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        object.__setattr__(self, "securities", ordered)
        object.__setattr__(self, "content_hash", digest)

    def for_window(self, start_date: date, end_date: date) -> tuple[MarketSecurity, ...]:
        if not isinstance(start_date, date) or isinstance(start_date, datetime):
            raise UniverseError("开始日期无效")
        if not isinstance(end_date, date) or isinstance(end_date, datetime) or end_date < start_date:
            raise UniverseError("结束日期无效")
        return tuple(
            security
            for security in self.securities
            if security.list_date <= end_date
            and (security.delist_date is None or security.delist_date >= start_date)
        )


class UniverseBuilder:
    """Fail closed on cross-page identity or listing-interval conflicts."""

    def seal(self, records: Iterable[ExchangeSecurity]) -> UniverseSnapshot:
        grouped: dict[str, list[ExchangeSecurity]] = {}
        for record in records:
            if not isinstance(record, ExchangeSecurity):
                raise UniverseError("股票池来源包含无效记录")
            grouped.setdefault(record.code, []).append(record)
        if not grouped:
            raise UniverseError("交易所没有返回可用普通 A 股")
        securities: list[MarketSecurity] = []
        for code in sorted(grouped):
            records_for_code = grouped[code]
            first = records_for_code[0]
            known_delist_dates = {
                record.delist_date
                for record in records_for_code
                if record.delist_date is not None
            }
            if len(known_delist_dates) > 1:
                raise UniverseError(f"{code} 的上市区间信息冲突")
            for candidate in records_for_code[1:]:
                if candidate.exchange != first.exchange:
                    raise UniverseError(f"{code} 的交易所信息冲突")
                if candidate.list_date != first.list_date:
                    raise UniverseError(f"{code} 的上市区间信息冲突")
                if candidate.name != first.name:
                    raise UniverseError(f"{code} 的证券简称信息冲突")
            selected = max(
                records_for_code,
                key=lambda item: (_STATUS_PRIORITY[item.status], item.source_at.isoformat(), item.source),
            )
            try:
                securities.append(selected.to_market_security())
            except UniverseSourceError as error:
                raise UniverseError(str(error)) from error
        return UniverseSnapshot(tuple(securities))


class HistoricalUniverseService:
    """Refresh exchange truth without deleting or rewriting historical intervals."""

    def __init__(self, repository: MarketDailyRepository, adapters: tuple[UniverseAdapter, ...]) -> None:
        if not adapters:
            raise ValueError("at least one exchange adapter is required")
        self._repository = repository
        self._adapters = adapters
        self._builder = UniverseBuilder()

    def refresh(self) -> UniverseSnapshot:
        records: list[ExchangeSecurity] = []
        for adapter in self._adapters:
            try:
                records.extend(adapter.fetch())
            except UniverseSourceError as error:
                raise UniverseError(str(error)) from error
        snapshot = self._builder.seal(records)
        for security in snapshot.securities:
            self._repository.upsert_security(security)
        return snapshot
