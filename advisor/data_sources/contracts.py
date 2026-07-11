from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass(frozen=True)
class DailyBar:
    code: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    source: str
    fetched_at: str
    as_of_date: date
    content_hash: str
    adj_factor: float | None = None
    limit_up: float | None = None
    limit_down: float | None = None


class MarketDataProvider(Protocol):
    def fetch_daily_bars(self, code: str, start: date, end: date) -> list[DailyBar]:
        raise NotImplementedError
