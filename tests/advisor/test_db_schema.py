import sqlite3

from advisor.db.migrate import migrate_database


REQUIRED_TABLES = {
    "advisor_runs",
    "data_quality_checks",
    "securities",
    "market_daily",
    "market_sources",
    "events_normalized",
    "evidence",
    "analyst_outputs",
    "stock_profiles",
    "stock_profile_history",
    "advice",
    "reviews",
    "ledger_accounts",
    "ledger_transactions",
    "positions",
    "portfolio_snapshots",
    "chart_assets",
    "report_archive",
}


def table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {row[0] for row in rows}


def test_migration_creates_required_tables(tmp_path):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    assert REQUIRED_TABLES.issubset(table_names(connection))


def test_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
