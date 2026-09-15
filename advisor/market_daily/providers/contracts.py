"""Narrow provider boundary for complete, canonical daily-bar observations."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from advisor.market_daily.contracts import CanonicalDailyBar


class MarketProviderError(RuntimeError):
    """One provider could not return a complete, trustworthy observation."""


class DailyBarProvider(Protocol):
    source: str
    endpoint: str

    def fetch_daily_bars(self, code: str, start: date, end: date) -> tuple[CanonicalDailyBar, ...]: ...
