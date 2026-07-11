from datetime import date

from advisor.data_sources.contracts import DailyBar


class TradingAgentsFreeSourceProvider:
    """Thin adapter boundary for free A-share sources from TradingAgents-astock."""

    def fetch_daily_bars(self, code: str, start: date, end: date) -> list[DailyBar]:
        raise RuntimeError(
            "Live free-source fetching is unavailable in this contract task; tests use a deterministic provider"
        )
