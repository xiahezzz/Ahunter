"""Eastmoney HTTP adapter for complete, unadjusted daily-bar observations."""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any, Callable, Iterable, Protocol

import requests

from advisor.market_daily.contracts import CanonicalDailyBar, MarketContractError, exchange_for_code
from advisor.market_daily.providers._http import pinned_https_session
from advisor.market_daily.providers.contracts import MarketProviderError


EASTMONEY_KLINE_HOSTNAME = "push2his.eastmoney.com"
EASTMONEY_KLINE_FALLBACK_ADDRESSES = ("117.184.45.167", "61.129.129.48", "101.226.30.136")


class _HttpSession(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any: ...


class EastmoneyDailyBarProvider:
    """Fetch raw (``fqt=0``) daily bars and normalize lots to shares."""

    source = "eastmoney"
    endpoint = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    _hostname = EASTMONEY_KLINE_HOSTNAME
    _fallback_addresses = EASTMONEY_KLINE_FALLBACK_ADDRESSES
    _headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "A-Hunter-Market-Daily/1.0",
    }

    def __init__(
        self,
        session: _HttpSession | None = None,
        fallback_sessions: Iterable[_HttpSession] = (),
        clock: Callable[[], datetime] | None = None,
        *,
        endpoint: str | None = None,
        timeout_seconds: float = 20,
        max_concurrency: int = 4,
    ) -> None:
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not isinstance(max_concurrency, int) or max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        primary_session = session if session is not None else requests.Session()
        fallback_sessions = tuple(fallback_sessions)
        if session is None and not fallback_sessions:
            fallback_sessions = tuple(
                pinned_https_session(self._hostname, address)
                for address in self._fallback_addresses
            )
        self._sessions = (primary_session, *fallback_sessions)
        self._active_session_index = 0
        self._clock = clock or (lambda: datetime.now().astimezone())
        self.endpoint = endpoint or self.endpoint
        self.timeout_seconds = float(timeout_seconds)
        self.max_concurrency = max_concurrency

    def fetch_daily_bars(self, code: str, start: date, end: date) -> tuple[CanonicalDailyBar, ...]:
        try:
            exchange = exchange_for_code(code)
        except MarketContractError as error:
            raise MarketProviderError("证券代码不属于沪深 A 股范围") from error
        if code.startswith("689"):
            raise MarketProviderError("CDR 不在 Market Daily 范围内")
        return self._fetch_with_secid(
            code,
            f"{'1' if exchange == 'SH' else '0'}.{code}",
            start,
            end,
        )

    def fetch_index_daily_bars(
        self, index_code: str, market: str, start: date, end: date
    ) -> tuple[CanonicalDailyBar, ...]:
        """Use the same HTTP/parser boundary for one exchange benchmark index."""

        if not isinstance(index_code, str) or re.fullmatch(r"\d{6}", index_code) is None:
            raise MarketProviderError("基准指数代码无效")
        if market not in {"SH", "SZ"}:
            raise MarketProviderError("基准指数市场无效")
        return self._fetch_with_secid(index_code, f"{'1' if market == 'SH' else '0'}.{index_code}", start, end)

    def _fetch_with_secid(
        self, code: str, secid: str, start: date, end: date
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
        payload = self._request(secid, start, end)
        return parse_eastmoney_daily_bars(
            payload,
            code=code,
            start=start,
            end=end,
            fetched_at=fetched_at,
            source=self.source,
        )

    def _request(self, secid: str, start: date, end: date) -> object:
        params = {
            "secid": secid,
            "ut": "fa5fd1943c7b386f172d6893dbfba10b",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101",
            "fqt": "0",
            "beg": start.strftime("%Y%m%d"),
            "end": end.strftime("%Y%m%d"),
            "lmt": "10000",
        }
        last_error: Exception | None = None
        for offset in range(len(self._sessions)):
            index = (self._active_session_index + offset) % len(self._sessions)
            try:
                response = self._sessions[index].get(
                    self.endpoint,
                    params=params,
                    headers=self._headers,
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                content_type = getattr(response, "headers", {}).get("Content-Type", "")
                if not isinstance(content_type, str) or not content_type.lower().startswith("application/json"):
                    raise MarketProviderError("东方财富返回了非 JSON 内容")
                payload = response.json()
            except (MarketProviderError, requests.RequestException, ValueError, OSError, AttributeError) as error:
                last_error = error
                continue
            self._active_session_index = index
            return payload
        raise MarketProviderError("东方财富日线请求失败") from last_error


def parse_eastmoney_daily_bars(
    payload: object,
    *,
    code: str,
    start: date,
    end: date,
    fetched_at: datetime,
    source: str = "eastmoney",
) -> tuple[CanonicalDailyBar, ...]:
    """Parse an entire response atomically; invalid rows reject the whole response."""

    if not isinstance(payload, dict):
        raise MarketProviderError("东方财富响应不是对象")
    if payload.get("rc") not in (None, 0, "0"):
        raise MarketProviderError("东方财富返回了错误状态")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise MarketProviderError("东方财富响应缺少数据主体")
    returned_code = data.get("code")
    if not isinstance(returned_code, str) or returned_code.strip() != code:
        raise MarketProviderError("东方财富返回了错误证券代码")
    rows = data.get("klines")
    if not isinstance(rows, list) or not rows:
        raise MarketProviderError("东方财富没有返回请求区间日线")
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise MarketProviderError("抓取时间必须带时区")
    bars: list[CanonicalDailyBar] = []
    for row in rows:
        bars.append(
            _parse_eastmoney_row(
                row,
                code=code,
                fetched_at=fetched_at,
                source=source,
            )
        )
    dates = [bar.trade_date for bar in bars]
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise MarketProviderError("东方财富日线日期必须严格升序且唯一")
    if any(trade_date < start or trade_date > end for trade_date in dates):
        raise MarketProviderError("东方财富日线超出请求闭区间")
    return tuple(bars)


def _parse_eastmoney_row(
    row: object,
    *,
    code: str,
    fetched_at: datetime,
    source: str,
) -> CanonicalDailyBar:
    if not isinstance(row, str):
        raise MarketProviderError("东方财富日线行不是文本")
    fields = [field.strip() for field in row.split(",")]
    if len(fields) < 7:
        raise MarketProviderError("东方财富日线字段不完整")
    try:
        trade_date = date.fromisoformat(fields[0])
    except ValueError as error:
        raise MarketProviderError("东方财富日线日期无效") from error
    open_price = _positive_number(fields[1], "开盘价")
    close = _positive_number(fields[2], "收盘价")
    high = _positive_number(fields[3], "最高价")
    low = _positive_number(fields[4], "最低价")
    lots = _nonnegative_number(fields[5], "成交量")
    if not lots.is_integer():
        raise MarketProviderError("东方财富成交量必须是整数手")
    volume = int(lots) * 100
    amount = _positive_number(fields[6], "成交额")
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
        raise MarketProviderError("东方财富日线不满足规范契约") from error


def _positive_number(value: object, field_name: str) -> float:
    number = _nonnegative_number(value, field_name)
    if number <= 0:
        raise MarketProviderError(f"东方财富{field_name}必须大于 0")
    return number


def _nonnegative_number(value: object, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise MarketProviderError(f"东方财富{field_name}无效") from error
    if not math.isfinite(number) or number < 0:
        raise MarketProviderError(f"东方财富{field_name}无效")
    return number
