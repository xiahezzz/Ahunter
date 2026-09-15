"""Bounded, whole-observation fallback chain for Market Daily providers."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterable

from advisor.market_daily.contracts import CanonicalDailyBar
from advisor.market_daily.providers.contracts import DailyBarProvider, MarketProviderError


@dataclass(frozen=True)
class ProviderAttempt:
    source: str
    attempt_number: int
    outcome: str
    message: str | None = None


@dataclass(frozen=True)
class ProviderChainResult:
    bars: tuple[CanonicalDailyBar, ...]
    selected_source: str
    attempts: tuple[ProviderAttempt, ...]


class SingleProvider:
    """Adapt one bounded provider to the engine result contract, with no fallback."""

    def __init__(self, provider: DailyBarProvider) -> None:
        self.provider = provider

    def fetch_daily_bars(self, code: str, start: date, end: date) -> ProviderChainResult:
        try:
            bars = ProviderChain._complete_response(self.provider, code, start, end)
        except Exception as error:
            raise MarketProviderError("新浪行情源未返回完整日线") from error
        return ProviderChainResult(
            bars,
            self.provider.source,
            (ProviderAttempt(self.provider.source, 1, "passed"),),
        )


class ProviderChain:
    """Try primary at most twice, then fallback exactly once if still needed."""

    def __init__(
        self,
        primary: DailyBarProvider,
        fallback: DailyBarProvider,
        *,
        retry_delay_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if primary.source == fallback.source:
            raise ValueError("primary and fallback must be different sources")
        if not isinstance(retry_delay_seconds, (int, float)) or retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must be non-negative")
        self.primary = primary
        self.fallback = fallback
        self.retry_delay_seconds = float(retry_delay_seconds)
        self._sleep = sleep

    def fetch_daily_bars(self, code: str, start: date, end: date) -> ProviderChainResult:
        attempts: list[ProviderAttempt] = []
        for attempt_number in (1, 2):
            try:
                bars = self._complete_response(self.primary, code, start, end)
                attempts.append(ProviderAttempt(self.primary.source, attempt_number, "passed"))
                return ProviderChainResult(bars, self.primary.source, tuple(attempts))
            except Exception as error:
                attempts.append(
                    ProviderAttempt(self.primary.source, attempt_number, "failed", type(error).__name__)
                )
                if attempt_number == 1 and self.retry_delay_seconds:
                    self._sleep(self.retry_delay_seconds)
        try:
            bars = self._complete_response(self.fallback, code, start, end)
            attempts.append(ProviderAttempt(self.fallback.source, 1, "passed"))
            return ProviderChainResult(bars, self.fallback.source, tuple(attempts))
        except Exception as error:
            attempts.append(ProviderAttempt(self.fallback.source, 1, "failed", type(error).__name__))
            raise MarketProviderError("主备行情源均未返回完整日线") from error

    @staticmethod
    def _complete_response(
        provider: DailyBarProvider, code: str, start: date, end: date
    ) -> tuple[CanonicalDailyBar, ...]:
        bars = provider.fetch_daily_bars(code, start, end)
        if not isinstance(bars, tuple):
            raise MarketProviderError("行情源未返回完整日线集合")
        if not bars:
            if getattr(provider, "allows_empty_daily_bars", False):
                return ()
            raise MarketProviderError("行情源未返回完整日线集合")
        dates: set[date] = set()
        for bar in bars:
            if not isinstance(bar, CanonicalDailyBar):
                raise MarketProviderError("行情源返回了非规范日线")
            if bar.code != code or bar.trade_date < start or bar.trade_date > end:
                raise MarketProviderError("行情源返回了不属于请求的日线")
            if bar.trade_date in dates:
                raise MarketProviderError("行情源返回了重复日线")
            dates.add(bar.trade_date)
        ordered = tuple(sorted(bars, key=lambda bar: bar.trade_date))
        if ordered != bars:
            raise MarketProviderError("行情源返回日线未按日期升序排列")
        return bars
