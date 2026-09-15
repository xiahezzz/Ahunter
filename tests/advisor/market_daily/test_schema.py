import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from advisor.db.migrate import migrate_database
from advisor.market_daily.contracts import AdjustmentFactor, CanonicalDailyBar, MarketSecurity
from advisor.market_daily.repository import MarketDailyRepository


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI)


def make_security() -> MarketSecurity:
    return MarketSecurity(
        code="600519",
        name="贵州茅台",
        exchange="SH",
        list_date=date(2001, 8, 27),
        delist_date=None,
        status="active",
        is_st=False,
        source="sse",
        source_at=NOW,
        fetched_at=NOW,
    )


def make_bar(close: float = 1405.0) -> CanonicalDailyBar:
    return CanonicalDailyBar(
        code="600519",
        trade_date=date(2026, 8, 7),
        open=1400.0,
        high=1410.0,
        low=1390.0,
        close=close,
        volume=100_000,
        amount=140_500_000.0,
        source="eastmoney",
        source_at=NOW,
        fetched_at=NOW,
    )


def test_migration_creates_market_daily_contract_tables(tmp_path: Path):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    assert {
        "market_adjustment_factors",
        "market_daily_absences",
        "trading_sessions",
        "market_daily_requests",
        "market_daily_runs",
        "market_daily_run_items",
        "market_daily_service_leases",
    }.issubset(names)
    table_info = {
        row[1]: row for row in connection.execute("PRAGMA table_info(market_daily)")
    }
    market_columns = {name: row[2].upper() for name, row in table_info.items()}
    assert market_columns["volume"] == "INTEGER"
    assert table_info["amount"][3] == 0
    assert "source_at" in market_columns
    assert "adj_factor" not in market_columns


def test_repository_keeps_one_canonical_bar_and_separate_factor(tmp_path: Path):
    repository = MarketDailyRepository(tmp_path / "advisor.sqlite")
    repository.upsert_security(make_security())

    assert repository.insert_bar(make_bar()) == "inserted"
    assert repository.insert_bar(make_bar()) == "unchanged"
    assert repository.insert_bar(make_bar(close=1406.0)) == "conflicted"
    assert repository.bar_for("600519", date(2026, 8, 7)).close == 1405.0

    factor = AdjustmentFactor(
        code="600519",
        trade_date=date(2026, 8, 7),
        factor=1.0,
        source="tdx",
        source_at=NOW,
        fetched_at=NOW,
        algorithm_version="factors@1",
    )
    assert repository.upsert_factor(factor) == "inserted"
    assert repository.factor_for("600519", date(2026, 8, 7)).factor == 1.0
    raw_hash = repository.bar_for("600519", date(2026, 8, 7)).content_hash

    updated_factor = AdjustmentFactor(
        code="600519",
        trade_date=date(2026, 8, 7),
        factor=0.5,
        source="tdx",
        source_at=NOW,
        fetched_at=NOW,
        algorithm_version="factors@1",
    )
    assert repository.upsert_factor(updated_factor) == "updated"
    assert repository.bar_for("600519", date(2026, 8, 7)).content_hash == raw_hash


def test_repository_round_trips_a_sina_bar_without_historical_amount(tmp_path: Path):
    repository = MarketDailyRepository(tmp_path / "advisor.sqlite")
    bar = CanonicalDailyBar(
        code="600519",
        trade_date=date(2026, 8, 7),
        open=1400.0,
        high=1410.0,
        low=1390.0,
        close=1405.0,
        volume=100_000,
        amount=None,
        source="sina",
        source_at=NOW,
        fetched_at=NOW,
    )

    assert repository.insert_bar(bar) == "inserted"
    stored = repository.bar_for("600519", date(2026, 8, 7))
    assert stored.amount is None
    assert stored.content_hash == bar.content_hash
