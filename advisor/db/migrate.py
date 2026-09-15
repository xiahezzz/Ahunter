import sqlite3
from pathlib import Path


SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def migrate_database(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        for table, column, definition in (
            ("lagent_bundle_rows", "file_hash", "TEXT NOT NULL DEFAULT ''"),
            ("securities", "security_type", "TEXT NOT NULL DEFAULT 'a_share'"),
            ("securities", "list_date", "TEXT"),
            ("securities", "delist_date", "TEXT"),
            ("securities", "status", "TEXT NOT NULL DEFAULT 'active'"),
            ("securities", "is_st", "INTEGER NOT NULL DEFAULT 0"),
            ("securities", "source", "TEXT NOT NULL DEFAULT 'legacy'"),
            ("securities", "source_at", "TEXT NOT NULL DEFAULT ''"),
            ("securities", "source_content_hash", "TEXT NOT NULL DEFAULT ''"),
            ("market_daily", "source_at", "TEXT NOT NULL DEFAULT ''"),
            ("market_daily_requests", "claimed_by", "TEXT"),
            ("market_daily_run_items", "claimed_by", "TEXT"),
            ("market_daily_run_items", "claim_expires_at", "TEXT"),
            ("research_snapshots", "unavailable_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("research_invocations", "query_log_hash", "TEXT REFERENCES research_artifacts(content_hash)"),
            ("research_invocation_attempts", "policy_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("research_invocation_attempts", "cli_version", "TEXT"),
            ("research_invocation_attempts", "model", "TEXT"),
            ("research_invocation_attempts", "reasoning_effort", "TEXT"),
            ("research_invocation_attempts", "usage_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("research_stage_attempts", "policy_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("research_stage_attempts", "cli_version", "TEXT"),
            ("research_stage_attempts", "model", "TEXT"),
            ("research_stage_attempts", "reasoning_effort", "TEXT"),
            ("research_stage_attempts", "usage_json", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            _ensure_column(connection, table, column, definition)
        connection.commit()
    finally:
        connection.close()


def recreate_empty_market_daily_table(db_path: Path) -> None:
    """Replace only an empty legacy ``market_daily`` table with the current contract.

    This is intentionally not a row migration.  Operators may use it only
    after the explicit Market Daily reset has removed every old fact.
    """

    connection = sqlite3.connect(db_path)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'market_daily'"
        ).fetchone()
        if exists is None:
            raise RuntimeError("market_daily 表不存在")
        count = int(connection.execute("SELECT COUNT(*) FROM market_daily").fetchone()[0])
        if count != 0:
            raise RuntimeError("market_daily 非空，拒绝重建表结构")
        connection.execute("DROP TABLE market_daily")
        connection.commit()
    finally:
        connection.close()
    migrate_database(db_path)
    verified = sqlite3.connect(db_path)
    try:
        columns = {row[1]: str(row[2]).upper() for row in verified.execute("PRAGMA table_info(market_daily)")}
        if (
            columns.get("volume") != "INTEGER"
            or "source_at" not in columns
            or "adj_factor" in columns
        ):
            raise RuntimeError("market_daily 新表结构校验失败")
    finally:
        verified.close()


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
