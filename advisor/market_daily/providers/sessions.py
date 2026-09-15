"""Benchmark-index observations used only to establish completed trading sessions."""

from __future__ import annotations

from datetime import date
from typing import Protocol


class _SinaIndexProvider(Protocol):
    def fetch_index_daily_bars(
        self, index_code: str, market: str, start: date, end: date
    ) -> tuple[object, ...]: ...


class SinaIndexSessionProvider:
    """Observe one exchange benchmark through the shared Sina adapter."""

    def __init__(self, provider: _SinaIndexProvider, *, market: str) -> None:
        if market not in {"SH", "SZ"}:
            raise ValueError("market must be SH or SZ")
        self._provider = provider
        self.market = market
        self.index_code = "000001" if market == "SH" else "399001"
        self.source = "sina_sh_index" if market == "SH" else "sina_sz_index"

    def observe_sessions(self, start: date, end: date) -> tuple[date, ...]:
        bars = self._provider.fetch_index_daily_bars(self.index_code, self.market, start, end)
        return tuple(bar.trade_date for bar in bars)
