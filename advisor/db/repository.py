import hashlib
import json
import sqlite3
import uuid
from datetime import date, datetime
from pathlib import Path

from advisor.data_sources.contracts import DailyBar, MarketDataProvider


CALENDAR_PROOF_PRODUCER = "a-hunter-advisor-calendar-producer-v1"
CALENDAR_PROOF_VERSION = 1
TRUSTED_CALENDAR_SOURCES = frozenset(
    {"exchange_calendar", "local_calendar", "local_trading_calendar", "trading_calendar"}
)
_CALENDAR_PROOF_DOMAIN = b"a-hunter:trading-calendar-proof:v1\0"


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
            actual_latest_session=latest,
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


def record_trading_calendar_proof(
    connection: sqlite3.Connection,
    *,
    calendar_source: str,
    as_of: datetime,
    latest_expected_session: date,
    coverage_codes: tuple[str, ...] | None = None,
    scope: str | None = None,
) -> str:
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be sqlite3.Connection")
    if calendar_source not in TRUSTED_CALENDAR_SOURCES:
        raise ValueError("invalid calendar source")
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("calendar proof as_of must be timezone-aware")
    if not isinstance(latest_expected_session, date) or isinstance(latest_expected_session, datetime):
        raise ValueError("latest expected session must be a date")
    if latest_expected_session > as_of.date():
        raise ValueError("latest expected session cannot be future-dated")
    if (coverage_codes is None) == (scope is None):
        raise ValueError("calendar proof requires exactly one coverage contract")
    if coverage_codes is not None:
        if (
            not isinstance(coverage_codes, tuple)
            or not coverage_codes
            or len(coverage_codes) > 200
            or len(set(coverage_codes)) != len(coverage_codes)
            or any(not isinstance(code, str) or len(code) != 6 or not code.isdigit() for code in coverage_codes)
        ):
            raise ValueError("invalid calendar proof coverage")
        normalized_codes = tuple(sorted(coverage_codes))
        normalized_scope = "candidate_codes"
    else:
        if scope != "a_share":
            raise ValueError("invalid calendar proof scope")
        normalized_codes = ()
        normalized_scope = scope
    canonical = _calendar_proof_canonical_fields(
        calendar_source=calendar_source,
        as_of=as_of.isoformat(),
        latest_expected_session=latest_expected_session.isoformat(),
        scope=normalized_scope,
        coverage_codes=normalized_codes,
    )
    content_hash = calendar_proof_content_hash(
        calendar_source=calendar_source,
        as_of=as_of.isoformat(),
        latest_expected_session=latest_expected_session.isoformat(),
        scope=normalized_scope,
        coverage_codes=normalized_codes,
    )
    proof_id = f"advisor-calendar-proof:v1:{content_hash}"
    values = (
        proof_id,
        CALENDAR_PROOF_VERSION,
        CALENDAR_PROOF_PRODUCER,
        canonical["calendar_source"],
        canonical["as_of"],
        canonical["latest_expected_session"],
        canonical["scope"],
        json.dumps(canonical["coverage_codes"], separators=(",", ":")),
        content_hash,
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO trading_calendar_proofs (
          proof_id, contract_version, producer, calendar_source, as_of,
          latest_expected_session, scope, coverage_codes_json, content_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )
    persisted = connection.execute(
        """
        SELECT proof_id, contract_version, producer, calendar_source, as_of,
               latest_expected_session, scope, coverage_codes_json, content_hash
        FROM trading_calendar_proofs WHERE proof_id = ?
        """,
        (proof_id,),
    ).fetchone()
    if persisted is None or tuple(persisted) != values:
        raise ValueError("conflicting trading calendar proof")
    return proof_id


def calendar_proof_content_hash(
    *,
    calendar_source: str,
    as_of: str,
    latest_expected_session: str,
    scope: str,
    coverage_codes: tuple[str, ...],
) -> str:
    canonical = _calendar_proof_canonical_fields(
        calendar_source=calendar_source,
        as_of=as_of,
        latest_expected_session=latest_expected_session,
        scope=scope,
        coverage_codes=coverage_codes,
    )
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(_CALENDAR_PROOF_DOMAIN + encoded).hexdigest()


def _calendar_proof_canonical_fields(
    *,
    calendar_source: str,
    as_of: str,
    latest_expected_session: str,
    scope: str,
    coverage_codes: tuple[str, ...],
) -> dict[str, object]:
    return {
        "as_of": as_of,
        "calendar_source": calendar_source,
        "contract_version": CALENDAR_PROOF_VERSION,
        "coverage_codes": list(coverage_codes),
        "latest_expected_session": latest_expected_session,
        "producer": CALENDAR_PROOF_PRODUCER,
        "scope": scope,
    }
