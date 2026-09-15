"""Direct ``tdxpy`` TCP fallback for complete daily-bar observations."""

from __future__ import annotations

import importlib
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Mapping, Protocol

from advisor.market_daily.contracts import CanonicalDailyBar, MarketContractError, exchange_for_code
from advisor.market_daily.providers.contracts import MarketProviderError


class _TdxClient(Protocol):
    def connect(self, ip: str, port: int, time_out: float = 5.0) -> bool: ...

    def disconnect(self) -> Any: ...

    def get_security_bars(
        self, category: int, market: int, code: str, start: int, count: int
    ) -> object: ...

    def get_index_bars(self, category: int, market: int, code: str, start: int, count: int) -> object: ...


@dataclass(frozen=True)
class TdxServer:
    host: str
    port: int = 7709

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host.strip() or len(self.host) > 255:
            raise ValueError("TDX host must be non-empty")
        if not isinstance(self.port, int) or isinstance(self.port, bool) or not 1 <= self.port <= 65535:
            raise ValueError("TDX port must be within 1..65535")


DEFAULT_TDX_SERVERS = (
    # These are independent public TDX endpoints.  A source attempt may try
    # this short list internally, but never falls back to a different market-
    # data source here.
    TdxServer("180.153.18.170", 7709),
    TdxServer("60.12.136.250", 7709),
    TdxServer("115.238.56.198", 7709),
)


def _default_client_factory() -> _TdxClient:
    try:
        module = importlib.import_module("tdxpy.hq")
        factory = getattr(module, "TdxHq_API")
    except (ImportError, AttributeError) as error:
        raise MarketProviderError("TDX 依赖未安装或不可用") from error
    return factory(heartbeat=False, auto_retry=False, raise_exception=False)


class TdxDailyBarProvider:
    """Page direct TDX daily bars; never blend a partial response with another source."""

    source = "tdx"
    endpoint = "tdx_tcp"

    def __init__(
        self,
        client_factory: Callable[[], _TdxClient] | None = None,
        clock: Callable[[], datetime] | None = None,
        *,
        servers: tuple[TdxServer, ...] = DEFAULT_TDX_SERVERS,
        timeout_seconds: float = 8.0,
        page_size: int = 800,
        max_pages: int = 20,
    ) -> None:
        if not servers or any(not isinstance(server, TdxServer) for server in servers):
            raise ValueError("at least one TDX server is required")
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not isinstance(page_size, int) or not 1 <= page_size <= 800:
            raise ValueError("page_size must be within 1..800")
        if not isinstance(max_pages, int) or max_pages <= 0:
            raise ValueError("max_pages must be positive")
        self._client_factory = client_factory or _default_client_factory
        self._clock = clock or (lambda: datetime.now().astimezone())
        self.servers = servers
        self.timeout_seconds = float(timeout_seconds)
        self.page_size = page_size
        self.max_pages = max_pages

    def fetch_daily_bars(self, code: str, start: date, end: date) -> tuple[CanonicalDailyBar, ...]:
        try:
            exchange = exchange_for_code(code)
        except MarketContractError as error:
            raise MarketProviderError("证券代码不属于沪深 A 股范围") from error
        if code.startswith("689"):
            raise MarketProviderError("CDR 不在 Market Daily 范围内")
        return self._fetch_with_method(1 if exchange == "SH" else 0, code, start, end, "get_security_bars")

    def fetch_index_daily_bars(
        self, index_code: str, market: str, start: date, end: date
    ) -> tuple[CanonicalDailyBar, ...]:
        """Use the direct index endpoint for session benchmarks, not stock bars."""

        if not isinstance(index_code, str) or re.fullmatch(r"\d{6}", index_code) is None:
            raise MarketProviderError("基准指数代码无效")
        if market not in {"SH", "SZ"}:
            raise MarketProviderError("基准指数市场无效")
        return self._fetch_with_method(1 if market == "SH" else 0, index_code, start, end, "get_index_bars")

    def _fetch_with_method(
        self, market: int, code: str, start: date, end: date, method_name: str
    ) -> tuple[CanonicalDailyBar, ...]:
        if not isinstance(start, date) or isinstance(start, datetime):
            raise MarketProviderError("开始日期无效")
        if not isinstance(end, date) or isinstance(end, datetime) or end < start:
            raise MarketProviderError("结束日期无效")
        fetched_at = self._clock()
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            raise MarketProviderError("抓取时钟必须带时区")
        if end > fetched_at.date():
            raise MarketProviderError("不能请求未来日期的日线")
        for server in self.servers:
            client: _TdxClient | None = None
            try:
                client = self._client_factory()
                # ``tdxpy`` returns its connected API object while small test
                # doubles and older releases return ``True``.  Both are
                # successful connections; only a false-y result means that
                # this endpoint was not usable.
                if not client.connect(server.host, server.port, time_out=self.timeout_seconds):
                    continue
                return self._fetch_from_connected_client(
                    client, market, code, start, end, fetched_at, method_name
                )
            except MarketProviderError:
                # A complete syntactic but untrustworthy TDX response should
                # allow another configured server to be tried once.
                continue
            except Exception:
                continue
            finally:
                if client is not None:
                    try:
                        client.disconnect()
                    except Exception:
                        pass
        raise MarketProviderError("通达信日线连接或读取失败")

    def _fetch_from_connected_client(
        self,
        client: _TdxClient,
        market: int,
        code: str,
        start: date,
        end: date,
        fetched_at: datetime,
        method_name: str,
    ) -> tuple[CanonicalDailyBar, ...]:
        by_date: dict[date, CanonicalDailyBar] = {}
        oldest_seen: date | None = None
        reached_history_end = False
        for page_index in range(self.max_pages):
            offset = page_index * self.page_size
            try:
                method = getattr(client, method_name)
                raw_page = method(9, market, code, offset, self.page_size)
            except Exception as error:
                raise MarketProviderError("通达信日线分页读取失败") from error
            page = _require_page(raw_page)
            if not page:
                reached_history_end = True
                break
            parsed_page = tuple(
                _parse_tdx_row(row, code=code, fetched_at=fetched_at, source=self.source) for row in page
            )
            page_dates = [bar.trade_date for bar in parsed_page]
            if len(page_dates) != len(set(page_dates)):
                raise MarketProviderError("通达信分页包含重复日期")
            if page_dates != sorted(page_dates) and page_dates != sorted(page_dates, reverse=True):
                raise MarketProviderError("通达信分页日期无序")
            for bar in parsed_page:
                if bar.trade_date in by_date:
                    raise MarketProviderError("通达信分页边界重复")
                by_date[bar.trade_date] = bar
            oldest_page_date = min(page_dates)
            oldest_seen = oldest_page_date if oldest_seen is None else min(oldest_seen, oldest_page_date)
            if oldest_seen <= start:
                break
            if len(page) < self.page_size:
                reached_history_end = True
                break
        else:
            raise MarketProviderError("通达信分页超过安全上限")
        selected = tuple(bar for trade_date, bar in sorted(by_date.items()) if start <= trade_date <= end)
        if not selected:
            raise MarketProviderError("通达信没有返回请求区间日线")
        if oldest_seen is None:
            raise MarketProviderError("通达信没有返回日线")
        # A short final page means the stock did not have older history, which
        # is valid for a newly listed stock.  An empty first page is not.
        if not reached_history_end and oldest_seen > start:
            raise MarketProviderError("通达信未覆盖请求开始日期")
        return selected


def _require_page(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, (list, tuple)):
        raise MarketProviderError("通达信分页响应无效")
    if any(not isinstance(row, Mapping) for row in value):
        raise MarketProviderError("通达信日线行无效")
    return tuple(value)


def _parse_tdx_row(
    row: Mapping[str, object], *, code: str, fetched_at: datetime, source: str
) -> CanonicalDailyBar:
    trade_date = _tdx_date(row)
    open_price = _positive_number(row.get("open"), "开盘价")
    close = _positive_number(row.get("close"), "收盘价")
    high = _positive_number(row.get("high"), "最高价")
    low = _positive_number(row.get("low"), "最低价")
    volume = _integer_shares(row.get("vol"))
    amount = _positive_number(row.get("amount"), "成交额")
    try:
        return CanonicalDailyBar(
            code=code,
            trade_date=trade_date,
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
            amount=amount,
            source=source,
            source_at=fetched_at,
            fetched_at=fetched_at,
            as_of_date=trade_date,
        )
    except MarketContractError as error:
        raise MarketProviderError("通达信日线不满足规范契约") from error


def _tdx_date(row: Mapping[str, object]) -> date:
    raw_datetime = row.get("datetime")
    if isinstance(raw_datetime, datetime):
        return raw_datetime.date()
    if isinstance(raw_datetime, str):
        try:
            return datetime.fromisoformat(raw_datetime).date()
        except ValueError:
            try:
                return date.fromisoformat(raw_datetime[:10])
            except ValueError:
                pass
    try:
        year = int(row["year"])
        month = int(row["month"])
        day = int(row["day"])
        return date(year, month, day)
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise MarketProviderError("通达信日线日期无效") from error


def _positive_number(value: object, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise MarketProviderError(f"通达信{field_name}无效") from error
    if not math.isfinite(number) or number <= 0:
        raise MarketProviderError(f"通达信{field_name}必须大于 0")
    return number


def _integer_shares(value: object) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise MarketProviderError("通达信成交量无效") from error
    if not math.isfinite(number) or number <= 0 or not number.is_integer():
        raise MarketProviderError("通达信成交量必须是正整数股")
    return int(number)
