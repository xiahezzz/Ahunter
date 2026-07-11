from datetime import date
from pathlib import Path

from advisor.data_sources.backfill import backfill_daily_bars
from advisor.data_sources.contracts import DailyBar, MarketDataProvider
from advisor.db.migrate import migrate_database
from advisor.db.repository import connect


class FakeProvider(MarketDataProvider):
    def fetch_daily_bars(self, code: str, start: date, end: date) -> list[DailyBar]:
        return [
            DailyBar(
                code=code,
                trade_date=date(2026, 7, 10),
                open=10.0,
                high=10.8,
                low=9.8,
                close=10.5,
                volume=1000000,
                amount=10500000,
                source="fake_free_source",
                fetched_at="2026-07-11T08:00:00+08:00",
                as_of_date=date(2026, 7, 10),
                content_hash="bar-1",
            )
        ]


def test_backfill_writes_daily_bars(tmp_path: Path):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    inserted = backfill_daily_bars(
        db_path,
        FakeProvider(),
        ["600519"],
        date(2023, 7, 11),
        date(2026, 7, 10),
    )
    assert inserted == 1
    row = connect(db_path).execute(
        "SELECT code, close, source FROM market_daily"
    ).fetchone()
    assert dict(row) == {
        "code": "600519",
        "close": 10.5,
        "source": "fake_free_source",
    }


def test_backfill_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    first = backfill_daily_bars(
        db_path,
        FakeProvider(),
        ["600519"],
        date(2023, 7, 11),
        date(2026, 7, 10),
    )
    second = backfill_daily_bars(
        db_path,
        FakeProvider(),
        ["600519"],
        date(2023, 7, 11),
        date(2026, 7, 10),
    )
    assert first == 1
    assert second == 0
