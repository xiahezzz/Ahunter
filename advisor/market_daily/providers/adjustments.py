"""Eastmoney-only complete-source adjustment-factor adapter."""

from __future__ import annotations

import math
import time
from datetime import date, datetime
from typing import Any, Callable, Iterable, Protocol

import requests

from advisor.market_daily.adjustments import ALGORITHM_VERSION, AdjustmentError, normalize_forward_factors
from advisor.market_daily.contracts import AdjustmentFactor, MarketContractError, exchange_for_code
from advisor.market_daily.providers._http import pinned_https_session
from advisor.market_daily.providers.contracts import MarketProviderError
from advisor.market_daily.providers.eastmoney import (
    EASTMONEY_KLINE_FALLBACK_ADDRESSES,
    EASTMONEY_KLINE_HOSTNAME,
)


class _HttpSession(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any: ...


class EastmoneyAdjustmentFactorProvider:
    """Derive factors from one complete Eastmoney response pair, at most twice."""

    source = "eastmoney_adjustment"
    endpoint = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    _headers = {"Accept": "application/json, text/plain, */*", "User-Agent": "A-Hunter-Market-Daily/1.0"}

    def __init__(
        self,
        session: _HttpSession | None = None,
        fallback_sessions: Iterable[_HttpSession] = (),
        clock: Callable[[], datetime] | None = None,
        *,
        timeout_seconds: float = 20,
        max_attempts: int = 2,
        retry_delay_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= 2:
            raise ValueError("max_attempts must be within 1..2")
        if not isinstance(retry_delay_seconds, (int, float)) or retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must be non-negative")
        primary_session = session if session is not None else requests.Session()
        fallback_sessions = tuple(fallback_sessions)
        if session is None and not fallback_sessions:
            fallback_sessions = tuple(
                pinned_https_session(EASTMONEY_KLINE_HOSTNAME, address)
                for address in EASTMONEY_KLINE_FALLBACK_ADDRESSES
            )
        self._sessions = (primary_session, *fallback_sessions)
        self._active_session_index = 0
        self._clock = clock or (lambda: datetime.now().astimezone())
        self.timeout_seconds = float(timeout_seconds)
        self.max_attempts = max_attempts
        self.retry_delay_seconds = float(retry_delay_seconds)
        self._sleep = sleep

    def fetch_adjustment_factors(
        self, code: str, start: date, end: date
    ) -> tuple[AdjustmentFactor, ...]:
        try:
            exchange = exchange_for_code(code)
        except MarketContractError as error:
            raise MarketProviderError("证券代码不属于沪深 A 股范围") from error
        if code.startswith("689"):
            raise MarketProviderError("CDR 不在 Market Daily 范围内")
        if not isinstance(start, date) or not isinstance(end, date) or start > end:
            raise MarketProviderError("复权请求日期范围无效")
        fetched_at = self._clock()
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None or end > fetched_at.date():
            raise MarketProviderError("复权抓取时间或日期范围无效")
        secid = f"{'1' if exchange == 'SH' else '0'}.{code}"
        last_error: MarketProviderError | None = None
        for attempt_number in range(1, self.max_attempts + 1):
            try:
                return self._fetch_complete_pair(code, secid, start, end, fetched_at)
            except MarketProviderError as error:
                last_error = error
                if attempt_number < self.max_attempts and self.retry_delay_seconds:
                    self._sleep(self.retry_delay_seconds)
        assert last_error is not None
        attempt_text = "一次" if self.max_attempts == 1 else "两次"
        raise MarketProviderError(f"复权来源在{attempt_text}尝试后仍不可用") from last_error

    def _fetch_complete_pair(
        self, code: str, secid: str, start: date, end: date, fetched_at: datetime
    ) -> tuple[AdjustmentFactor, ...]:
        raw = _close_rows(self._request(secid, start, end, fqt="0"), code, start, end)
        forward = _close_rows(self._request(secid, start, end, fqt="1"), code, start, end)
        if tuple(raw) != tuple(forward):
            raise MarketProviderError("复权来源的日期集合不完整一致")
        try:
            return normalize_forward_factors(
                code,
                tuple((trade_date, forward[trade_date] / raw[trade_date]) for trade_date in raw),
                source=self.source,
                source_at=fetched_at,
                fetched_at=fetched_at,
                algorithm_version=ALGORITHM_VERSION,
            )
        except AdjustmentError as error:
            raise MarketProviderError("复权因子规范化失败") from error

    def _request(self, secid: str, start: date, end: date, *, fqt: str) -> object:
        params = {
            "secid": secid,
            "ut": "fa5fd1943c7b386f172d6893dbfba10b",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101",
            "fqt": fqt,
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
                    raise MarketProviderError("复权来源返回了非 JSON 内容")
                payload = response.json()
            except (MarketProviderError, requests.RequestException, ValueError, OSError, AttributeError) as error:
                last_error = error
                continue
            self._active_session_index = index
            return payload
        raise MarketProviderError("复权来源请求失败") from last_error


def _close_rows(payload: object, code: str, start: date, end: date) -> dict[date, float]:
    if not isinstance(payload, dict) or payload.get("rc") not in (None, 0, "0"):
        raise MarketProviderError("复权来源响应无效")
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("code") != code or not isinstance(data.get("klines"), list):
        raise MarketProviderError("复权来源数据主体无效")
    result: dict[date, float] = {}
    previous: date | None = None
    for line in data["klines"]:
        if not isinstance(line, str):
            raise MarketProviderError("复权来源日线行无效")
        fields = [field.strip() for field in line.split(",")]
        if len(fields) < 3:
            raise MarketProviderError("复权来源日线字段不完整")
        try:
            trade_date = date.fromisoformat(fields[0])
            close = float(fields[2])
        except (TypeError, ValueError, OverflowError) as error:
            raise MarketProviderError("复权来源日线字段无效") from error
        if not math.isfinite(close) or close <= 0 or not start <= trade_date <= end:
            raise MarketProviderError("复权来源日线超出范围或无效")
        if previous is not None and trade_date <= previous:
            raise MarketProviderError("复权来源日期必须升序唯一")
        previous = trade_date
        result[trade_date] = close
    if not result:
        raise MarketProviderError("复权来源没有返回日线")
    return result
