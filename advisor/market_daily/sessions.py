"""Observed (never guessed) Shanghai-Shenzhen trading-session facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from advisor.market_daily.contracts import ObservedTradingSession, SessionObservationReceipt
from advisor.market_daily.providers.contracts import MarketProviderError
from advisor.market_daily.repository import MarketDailyRepository


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_READY_AT = time(21, 0)


class SessionObservationError(RuntimeError):
    """A session source did not establish a trustworthy common trading date."""


class SessionObservationProvider(Protocol):
    source: str

    def observe_sessions(self, start: date, end: date) -> tuple[date, ...]: ...


@dataclass(frozen=True)
class SessionRefreshResult:
    status: str
    latest_session: date | None
    inserted: int
    session_hash: str


class ObservedSessionService:
    """Persist only the intersection independently observed by both providers."""

    def __init__(
        self,
        repository: MarketDailyRepository,
        primary: SessionObservationProvider,
        fallback: SessionObservationProvider,
    ) -> None:
        if primary.source == fallback.source:
            raise ValueError("session primary and fallback must be different")
        self._repository = repository
        self._primary = primary
        self._fallback = fallback

    def refresh(self, start: date, end: date, as_of: datetime) -> SessionRefreshResult:
        start = _require_date(start, "开始日期")
        end = _require_date(end, "结束日期")
        if start > end:
            raise SessionObservationError("开始日期不能晚于结束日期")
        local_as_of = _to_shanghai(as_of)
        if local_as_of.time() < _READY_AT:
            raise SessionObservationError("交易日观察只能在 21:00 后执行")
        primary_dates = _validate_dates(
            _observe(self._primary, start, end), start, end, local_as_of, self._primary.source
        )
        fallback_dates = _validate_dates(
            _observe(self._fallback, start, end), start, end, local_as_of, self._fallback.source
        )
        if primary_dates != fallback_dates:
            raise SessionObservationError("主备行情源观察到的交易日不一致")
        inserted = 0
        for trade_date in primary_dates:
            fact = ObservedTradingSession(
                trade_date=trade_date,
                primary_source=self._primary.source,
                fallback_source=self._fallback.source,
                primary_observed_at=local_as_of,
                fallback_observed_at=local_as_of,
                fetched_at=local_as_of,
            )
            if self._repository.upsert_session(fact) == "inserted":
                inserted += 1
        session_hash = self._repository.session_set_hash(start, end)
        latest_session = self._repository.latest_session_on_or_before(end)
        self._repository.record_session_observation(
            SessionObservationReceipt(
                observed_at=local_as_of,
                primary_source=self._primary.source,
                fallback_source=self._fallback.source,
                latest_session=latest_session,
                session_set_hash=session_hash,
            )
        )
        return SessionRefreshResult(
            status="updated" if inserted else "no_op",
            latest_session=latest_session,
            inserted=inserted,
            session_hash=session_hash,
        )

    def latest_completed_session(self, as_of: datetime) -> date:
        local_as_of = _to_shanghai(as_of)
        cutoff = local_as_of.date() if local_as_of.time() >= _READY_AT else local_as_of.date() - timedelta(days=1)
        latest = self._repository.latest_session_on_or_before(cutoff)
        if latest is None:
            raise SessionObservationError("本地没有已证明的交易日")
        return latest

    def sessions_for(self, start: date, end: date) -> tuple[date, ...]:
        return self._repository.sessions_for(start, end)


def _to_shanghai(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SessionObservationError("当前时间必须带时区")
    return value.astimezone(_SHANGHAI)


def _require_date(value: object, field_name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise SessionObservationError(f"{field_name}必须是日期")
    return value


def _validate_dates(
    values: object,
    start: date,
    end: date,
    as_of: datetime,
    source: str,
) -> tuple[date, ...]:
    if not isinstance(values, tuple):
        raise SessionObservationError(f"{source} 的交易日结果无效")
    if any(not isinstance(value, date) or isinstance(value, datetime) for value in values):
        raise SessionObservationError(f"{source} 的交易日结果无效")
    if values != tuple(sorted(values)) or len(values) != len(set(values)):
        raise SessionObservationError(f"{source} 的交易日必须升序且唯一")
    if any(value < start or value > end or value > as_of.date() for value in values):
        raise SessionObservationError(f"{source} 的交易日超出允许范围")
    return values


def _observe(provider: SessionObservationProvider, start: date, end: date) -> tuple[date, ...]:
    try:
        return provider.observe_sessions(start, end)
    except MarketProviderError as error:
        raise SessionObservationError(
            f"{provider.source} 交易日来源不可用：{str(error)}"
        ) from error
