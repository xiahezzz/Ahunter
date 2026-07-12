import hashlib
import json
import sqlite3
import uuid
from datetime import date
from pathlib import Path

from advisor.data_sources.contracts import DailyBar, MarketDataProvider


def connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def upsert_daily_bar(connection: sqlite3.Connection, bar: DailyBar) -> int:
    cursor = connection.execute(
        """
        INSERT INTO market_daily (
          code, trade_date, open, high, low, close, volume, amount,
          adj_factor, limit_up, limit_down, source, fetched_at,
          as_of_date, schema_version, content_hash, quality_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'passed')
        ON CONFLICT(code, trade_date) DO UPDATE SET
          open = excluded.open,
          high = excluded.high,
          low = excluded.low,
          close = excluded.close,
          volume = excluded.volume,
          amount = excluded.amount,
          adj_factor = excluded.adj_factor,
          limit_up = excluded.limit_up,
          limit_down = excluded.limit_down,
          source = excluded.source,
          fetched_at = excluded.fetched_at,
          as_of_date = excluded.as_of_date,
          schema_version = excluded.schema_version,
          content_hash = excluded.content_hash,
          quality_status = excluded.quality_status
        WHERE market_daily.content_hash <> excluded.content_hash
        """,
        (
            bar.code,
            bar.trade_date.isoformat(),
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
            bar.amount,
            bar.adj_factor,
            bar.limit_up,
            bar.limit_down,
            bar.source,
            bar.fetched_at,
            bar.as_of_date.isoformat(),
            bar.content_hash,
        ),
    )
    return cursor.rowcount


def record_market_source_attempt(
    connection: sqlite3.Connection,
    provider: MarketDataProvider,
    code: str,
    start: date,
    end: date,
    *,
    status: str,
    fetched_at: str,
    error: str | None = None,
) -> None:
    params = {"code": code, "start": start.isoformat(), "end": end.isoformat()}
    params_json = json.dumps(params, sort_keys=True, separators=(",", ":"))
    details = {**params, "error": error}
    if status == "passed":
        latest = connection.execute(
            """
            SELECT MAX(trade_date) FROM market_daily
            WHERE code = ? AND source = ? AND quality_status = 'passed'
              AND trade_date BETWEEN ? AND ?
            """,
            (code, provider.source, start.isoformat(), end.isoformat()),
        ).fetchone()[0]
        if not isinstance(latest, str):
            raise ValueError("successful market source attempt requires persisted bars")
        details.update(
            proof_type="historical_market_fetch",
            latest_expected_session=latest,
        )
    connection.execute(
        """
        INSERT INTO market_sources (
          source_key, source, endpoint, params_hash, fetched_at, status, details_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid.uuid4().hex,
            provider.source,
            provider.endpoint,
            hashlib.sha256(params_json.encode("utf-8")).hexdigest(),
            fetched_at,
            status,
            json.dumps(details, sort_keys=True, separators=(",", ":")),
        ),
    )
