import sqlite3

from advisor.db.migrate import migrate_database, recreate_empty_market_daily_table


REQUIRED_TABLES = {
    "advisor_runs",
    "data_quality_checks",
    "securities",
    "market_daily",
    "market_sources",
    "trading_sessions",
    "trading_session_observations",
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
    "research_scope_snapshots",
    "research_scope_invocations",
    "research_scope_invocation_attempts",
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


def test_recreate_empty_market_daily_table_refuses_facts_and_restores_current_contract(tmp_path):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute("DROP TABLE market_daily")
    connection.execute(
        """
        CREATE TABLE market_daily (
          code TEXT NOT NULL, trade_date TEXT NOT NULL, open REAL NOT NULL,
          high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
          volume REAL NOT NULL, amount REAL NOT NULL, source TEXT NOT NULL,
          fetched_at TEXT NOT NULL, as_of_date TEXT NOT NULL,
          content_hash TEXT NOT NULL, quality_status TEXT NOT NULL,
          PRIMARY KEY(code, trade_date)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO market_daily VALUES (
          '600000', '2026-08-07', 1, 1, 1, 1, 100, 100, 'old',
          '2026-08-07T21:00:00+08:00', '2026-08-07', 'hash', 'passed'
        )
        """
    )
    connection.commit()
    connection.close()

    try:
        recreate_empty_market_daily_table(db_path)
    except RuntimeError as error:
        assert "非空" in str(error)
    else:
        raise AssertionError("non-empty legacy table must not be replaced")

    connection = sqlite3.connect(db_path)
    connection.execute("DELETE FROM market_daily")
    connection.commit()
    connection.close()
    recreate_empty_market_daily_table(db_path)

    connection = sqlite3.connect(db_path)
    try:
        columns = {row[1]: row[2].upper() for row in connection.execute("PRAGMA table_info(market_daily)")}
        assert columns["volume"] == "INTEGER"
        assert "source_at" in columns
        assert "adj_factor" not in columns
    finally:
        connection.close()
