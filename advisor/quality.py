from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Literal
from zoneinfo import ZoneInfo

from advisor.calendar import ObservedTradingSessionsUnavailableError, latest_expected_session
from advisor.evidence.mx_adapter import CollectorSnapshot
from advisor.ledger.model import LedgerTransaction, apply_transactions
from advisor.market_daily.contracts import ObservedTradingSession, SessionObservationReceipt


_MAX_LEDGER_ROWS = 10_000
_MAX_SOURCE_ROWS = 1_000
_MAX_SESSION_ROWS = 3_000
_VALID_RUN_TYPES = frozenset({"premarket", "review"})
_CODE_RE = re.compile(r"[03468]\d{5}\Z")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SESSION_READY_AT = dt.time(21, 0)

# The legacy operational quality tables are retained for existing local data
# reads, but their contract now names only A Hunter's seven Research Agents.
ANALYST_ROLES = (
    "market",
    "social",
    "news",
    "fundamentals",
    "policy",
    "hot_money",
    "lockup",
)


@dataclass(frozen=True)
class QualityResult:
    check_name: str
    severity: str
    passed: bool
    details: str

    @property
    def blocking_failure(self) -> bool:
        return self.severity == "blocking" and not self.passed


@dataclass(frozen=True)
class QualityRequest:
    run_id: str
    run_type: str
    as_of: dt.datetime
    candidate_codes: tuple[str, ...]
    collector: CollectorSnapshot


@dataclass(frozen=True)
class QualityGateResult:
    status: Literal["passed", "blocked"]
    checks: tuple[QualityResult, ...]


def evaluate_run_quality(
    connection: sqlite3.Connection,
    request: QualityRequest,
) -> QualityGateResult:
    valid, reason = _validate_request(request)
    collector_check = _collector_check(request.collector if isinstance(request, QualityRequest) else None)
    if not valid:
        checks = (
            collector_check,
            *tuple(_blocked_check(name, reason) for name in (
                "trading_calendar",
                "market_staleness",
                "three_year_candidate_coverage",
                "future_data_leakage",
                "ledger_replay",
                "analyst_contract_readiness",
            )),
        )
    else:
        checks = (
            collector_check,
            _trading_calendar_check(connection, request),
            _market_staleness_check(connection, request),
            _coverage_check(connection, request),
            _future_data_check(connection, request),
            _ledger_check(connection),
            _analyst_check(connection, request),
            *_optional_source_checks(connection, request),
        )
    result = QualityGateResult(
        status="blocked" if any(check.blocking_failure for check in checks) else "passed",
        checks=tuple(checks),
    )
    if isinstance(request, QualityRequest) and _valid_run_id(request.run_id):
        _persist_checks(connection, request, result.checks)
    return result


def persist_quality_results(
    connection: sqlite3.Connection,
    request: QualityRequest,
    results: tuple[QualityResult, ...],
) -> None:
    """Persist an already evaluated or coordinator-adjusted set of checks."""
    if not isinstance(request, QualityRequest) or not _valid_run_id(request.run_id):
        raise ValueError("invalid quality request")
    if not isinstance(results, tuple) or not results or not all(
        isinstance(result, QualityResult) for result in results
    ):
        raise ValueError("quality results are missing or invalid")
    _persist_checks(connection, request, results)


def _validate_request(request: object) -> tuple[bool, str]:
    if not isinstance(request, QualityRequest):
        return False, "quality request is missing or invalid"
    if not _valid_run_id(request.run_id):
        return False, "quality request run ID is invalid"
    if request.run_type not in _VALID_RUN_TYPES:
        return False, "quality request run type is invalid"
    if not isinstance(request.as_of, dt.datetime) or request.as_of.tzinfo is None or request.as_of.utcoffset() is None:
        return False, "quality request as_of is invalid"
    codes = request.candidate_codes
    if (
        not isinstance(codes, tuple)
        or not codes
        or len(codes) > 200
        or len(set(codes)) != len(codes)
        or any(not isinstance(code, str) or len(code) != 6 or not code.isdigit() for code in codes)
    ):
        return False, "candidate codes are missing or invalid"
    if not isinstance(request.collector, CollectorSnapshot):
        return False, "collector snapshot is missing or invalid"
    return True, "quality request is valid"


def _valid_run_id(value: object) -> bool:
    return isinstance(value, str) and _RUN_ID_RE.fullmatch(value) is not None


def _collector_check(snapshot: CollectorSnapshot | None) -> QualityResult:
    quality = getattr(snapshot, "quality", None)
    if (
        quality is None
        or getattr(quality, "check_name", None) != "collector_state"
        or getattr(quality, "severity", None) != "blocking"
        or type(getattr(quality, "passed", None)) is not bool
        or not isinstance(getattr(quality, "details", None), str)
    ):
        return _blocked_check("collector_state", "collector snapshot quality is missing or invalid")
    return QualityResult("collector_state", "blocking", quality.passed, quality.details[:800])


def _trading_calendar_check(connection: sqlite3.Connection, request: QualityRequest) -> QualityResult:
    expected, error = _expected_observed_session(connection, request)
    if error is not None or expected is None:
        return _blocked_check("trading_calendar", error or "最新已观测交易日不可用")
    missing = [
        code
        for code in request.candidate_codes
        if not _has_security_coverage(connection, code, expected)
    ]
    if missing:
        return _blocked_check(
            "trading_calendar",
            f"最新已观测交易日 {expected.isoformat()} 缺少候选证券覆盖",
        )
    return QualityResult(
        "trading_calendar", "blocking", True,
        f"最新已观测交易日 {expected.isoformat()} 已覆盖",
    )


def _expected_observed_session(
    connection: sqlite3.Connection, request: QualityRequest
) -> tuple[dt.date | None, str | None]:
    """Read the local dual-source session facts without inferring any closure."""
    session_rows = connection.execute(
        """
        SELECT trade_date, primary_source, fallback_source, primary_observed_at,
               fallback_observed_at, fetched_at, content_hash
        FROM trading_sessions
        WHERE trade_date <= ?
        ORDER BY trade_date
        LIMIT ?
        """,
        (request.as_of.date().isoformat(), _MAX_SESSION_ROWS + 1),
    ).fetchall()
    if len(session_rows) > _MAX_SESSION_ROWS:
        return None, "已观测交易日扫描超出上限"
    try:
        sessions: list[dt.date] = []
        for row in session_rows:
            fact = ObservedTradingSession(
                trade_date=dt.date.fromisoformat(row[0]),
                primary_source=row[1],
                fallback_source=row[2],
                primary_observed_at=dt.datetime.fromisoformat(row[3]),
                fallback_observed_at=dt.datetime.fromisoformat(row[4]),
                fetched_at=dt.datetime.fromisoformat(row[5]),
            )
            if fact.content_hash != row[6]:
                raise ValueError("交易日事实哈希不匹配")
            sessions.append(fact.trade_date)
        expected = latest_expected_session(request.as_of, tuple(sessions))
    except (KeyError, TypeError, ValueError, ObservedTradingSessionsUnavailableError):
        return None, "本地已观测交易日不可用或无效"

    receipt_row = connection.execute(
        """
        SELECT observed_at, primary_source, fallback_source, latest_session,
               session_set_hash, content_hash
        FROM trading_session_observations
        WHERE julianday(observed_at) IS NOT NULL AND julianday(observed_at) <= julianday(?)
        ORDER BY julianday(observed_at) DESC, rowid DESC
        LIMIT 1
        """,
        (request.as_of.isoformat(),),
    ).fetchone()
    if receipt_row is None:
        return None, "缺少双源交易日观测记录"
    try:
        receipt = SessionObservationReceipt(
            observed_at=dt.datetime.fromisoformat(receipt_row[0]),
            primary_source=receipt_row[1],
            fallback_source=receipt_row[2],
            latest_session=(
                dt.date.fromisoformat(receipt_row[3])
                if receipt_row[3] else None
            ),
            session_set_hash=receipt_row[4],
        )
        if receipt.content_hash != receipt_row[5]:
            raise ValueError("交易日观测记录哈希不匹配")
        local_as_of = request.as_of.astimezone(_SHANGHAI)
        local_observed_at = receipt.observed_at.astimezone(_SHANGHAI)
        required_observation_date = (
            local_as_of.date()
            if local_as_of.time() >= _SESSION_READY_AT
            else local_as_of.date() - dt.timedelta(days=1)
        )
        if local_observed_at.time() < _SESSION_READY_AT or local_observed_at.date() < required_observation_date:
            return None, "双源交易日观测心跳已过期"
        if receipt.latest_session != expected:
            return None, "双源交易日观测与本地交易日事实不一致"
    except (TypeError, ValueError, OverflowError):
        return None, "双源交易日观测记录无效"
    return expected, None


def _market_staleness_check(connection: sqlite3.Connection, request: QualityRequest) -> QualityResult:
    expected, error = _expected_observed_session(connection, request)
    if error is not None or expected is None:
        return _blocked_check("market_staleness", error or "最新已观测交易日不可用")
    stale = [code for code in request.candidate_codes if not _has_security_coverage(connection, code, expected)]
    return QualityResult(
        "market_staleness", "blocking", not stale,
        "候选证券行情数据已更新" if not stale else f"{len(stale)} 只候选证券未覆盖最新已观测交易日",
    )


def _has_security_coverage(connection: sqlite3.Connection, code: str, session: dt.date) -> bool:
    row = connection.execute(
        """
        SELECT
          EXISTS(
            SELECT 1 FROM market_daily
            WHERE code = ? AND trade_date = ? AND quality_status = 'passed'
          )
          OR EXISTS(
            SELECT 1 FROM market_daily_absences
            WHERE code = ? AND trade_date = ? AND reason = 'suspended'
          )
        """,
        (code, session.isoformat(), code, session.isoformat()),
    ).fetchone()
    return bool(row and row[0])


def _coverage_check(connection: sqlite3.Connection, request: QualityRequest) -> QualityResult:
    cutoff = _three_year_cutoff(request.as_of.date())
    missing: list[str] = []
    for code in request.candidate_codes:
        row = connection.execute(
            "SELECT MIN(trade_date), MAX(trade_date) FROM market_daily WHERE code = ? AND trade_date <= ? AND quality_status = 'passed'",
            (code, request.as_of.date().isoformat()),
        ).fetchone()
        try:
            earliest = dt.date.fromisoformat(row[0]) if row and row[0] else None
        except (TypeError, ValueError):
            earliest = None
        if earliest is None or earliest > cutoff:
            missing.append(code)
    return QualityResult(
        "three_year_candidate_coverage", "blocking", not missing,
        "three-year candidate coverage is available" if not missing else f"three-year coverage missing for {len(missing)} candidates",
    )


def _future_data_check(connection: sqlite3.Connection, request: QualityRequest) -> QualityResult:
    date = request.as_of.date().isoformat()
    as_of = request.as_of.isoformat()
    queries = (
        (
            """
            SELECT 1 FROM market_daily
            WHERE date(trade_date) IS NULL OR date(as_of_date) IS NULL
               OR date(trade_date) > date(?) OR date(as_of_date) > date(?)
               OR julianday(fetched_at) IS NULL OR julianday(fetched_at) > julianday(?)
            LIMIT 1
            """,
            (date, date, as_of),
        ),
        ("SELECT 1 FROM events_normalized WHERE julianday(as_of) IS NULL OR julianday(as_of) > julianday(?) LIMIT 1", (as_of,)),
        ("SELECT 1 FROM evidence WHERE julianday(as_of) IS NULL OR julianday(as_of) > julianday(?) LIMIT 1", (as_of,)),
        (
            "SELECT 1 FROM analyst_outputs WHERE run_id = ? AND (julianday(as_of) IS NULL OR julianday(as_of) > julianday(?)) LIMIT 1",
            (request.run_id, as_of),
        ),
        (
            "SELECT 1 FROM ledger_transactions WHERE date(trade_date) IS NULL OR date(trade_date) > date(?) OR julianday(created_at) IS NULL OR julianday(created_at) > julianday(?) LIMIT 1",
            (date, as_of),
        ),
        (
            "SELECT 1 FROM market_sources WHERE julianday(fetched_at) IS NULL OR julianday(fetched_at) > julianday(?) LIMIT 1",
            (as_of,),
        ),
        (
            "SELECT 1 FROM trading_sessions WHERE julianday(fetched_at) IS NULL OR julianday(fetched_at) > julianday(?) LIMIT 1",
            (as_of,),
        ),
        (
            "SELECT 1 FROM trading_session_observations WHERE julianday(observed_at) IS NULL OR julianday(observed_at) > julianday(?) LIMIT 1",
            (as_of,),
        ),
    )
    leaked = _collector_has_future_data(request.collector, request.as_of) or any(
        connection.execute(sql, params).fetchone() is not None for sql, params in queries
    )
    return QualityResult(
        "future_data_leakage", "blocking", not leaked,
        "no future-dated records detected" if not leaked else "future-dated records detected",
    )


def _ledger_check(connection: sqlite3.Connection) -> QualityResult:
    rows = connection.execute(
        """
        SELECT transaction_id, account_id, trade_date, transaction_type, code,
               quantity, price, amount, fees
        FROM ledger_transactions
        ORDER BY account_id, trade_date, transaction_id
        LIMIT ?
        """,
        (_MAX_LEDGER_ROWS + 1,),
    ).fetchall()
    if len(rows) > _MAX_LEDGER_ROWS:
        return _blocked_check("ledger_replay", "ledger replay limit exceeded")
    grouped: dict[str, list[LedgerTransaction]] = defaultdict(list)
    try:
        for row in rows:
            transaction = LedgerTransaction(
                transaction_id=row[0], trade_date=row[2], transaction_type=row[3],
                code=row[4], quantity=row[5], price=row[6], amount=row[7], fees=row[8],
            )
            _validate_ledger_row(row[1], transaction)
            grouped[row[1]].append(transaction)
        for transactions in grouped.values():
            apply_transactions(transactions)
    except (TypeError, ValueError, OverflowError):
        return _blocked_check("ledger_replay", "ledger replay is invalid")
    return QualityResult("ledger_replay", "blocking", True, "ledger replay is valid")


def _analyst_check(connection: sqlite3.Connection, request: QualityRequest) -> QualityResult:
    rows = connection.execute(
        "SELECT role, code, as_of, summary FROM analyst_outputs WHERE run_id = ? LIMIT ?",
        (request.run_id, len(request.candidate_codes) * len(ANALYST_ROLES) + 1),
    ).fetchall()
    expected = {(code, role) for code in request.candidate_codes for role in ANALYST_ROLES}
    actual: set[tuple[str, str]] = set()
    invalid = len(rows) > len(expected)
    for role, code, output_as_of, summary in rows:
        key = (code, role)
        if (
            key in actual or key not in expected or not isinstance(summary, str) or not summary.strip()
            or not isinstance(output_as_of, str) or output_as_of > request.as_of.isoformat()
        ):
            invalid = True
        actual.add(key)
    missing = expected - actual
    passed = not invalid and not missing
    return QualityResult(
        "analyst_contract_readiness", "blocking", passed,
        "required analyst contracts are ready" if passed else "required analyst contracts are missing or invalid",
    )


def _validate_ledger_row(account_id: object, transaction: LedgerTransaction) -> None:
    if not isinstance(account_id, str) or not _IDENTIFIER_RE.fullmatch(account_id):
        raise ValueError("invalid account")
    if not isinstance(transaction.transaction_id, str) or not _IDENTIFIER_RE.fullmatch(transaction.transaction_id):
        raise ValueError("invalid transaction ID")
    try:
        if dt.date.fromisoformat(transaction.trade_date).isoformat() != transaction.trade_date:
            raise ValueError("invalid trade date")
    except (TypeError, ValueError) as error:
        raise ValueError("invalid trade date") from error
    if transaction.transaction_type not in {"cash_deposit", "cash_withdrawal", "buy", "sell", "fee", "tax"}:
        raise ValueError("invalid transaction type")
    if type(transaction.quantity) is not int or transaction.quantity < 0:
        raise ValueError("invalid quantity")
    if not all(type(value) in {int, float} and math.isfinite(value) for value in (transaction.price, transaction.amount, transaction.fees)):
        raise ValueError("invalid ledger amount")
    if transaction.transaction_type in {"buy", "sell"}:
        if (
            not isinstance(transaction.code, str) or not _CODE_RE.fullmatch(transaction.code)
            or transaction.quantity <= 0 or transaction.price <= 0 or transaction.fees < 0
        ):
            raise ValueError("invalid trade")
        if transaction.transaction_type == "buy" and transaction.amount >= 0:
            raise ValueError("invalid buy amount")
        if transaction.transaction_type == "sell" and transaction.amount <= 0:
            raise ValueError("invalid sell amount")
        if not math.isclose(abs(transaction.amount), transaction.quantity * transaction.price, rel_tol=1e-9, abs_tol=1e-6):
            raise ValueError("trade amount mismatch")
    elif (
        transaction.code is not None or transaction.quantity != 0 or transaction.price != 0
        or transaction.fees != 0
    ):
        raise ValueError("invalid cash transaction")
    elif transaction.transaction_type == "cash_deposit" and transaction.amount <= 0:
        raise ValueError("invalid cash deposit")
    elif transaction.transaction_type in {"cash_withdrawal", "fee", "tax"} and transaction.amount >= 0:
        raise ValueError("invalid cash outflow")


def _optional_source_checks(connection: sqlite3.Connection, request: QualityRequest) -> tuple[QualityResult, ...]:
    rows = connection.execute(
        """
        SELECT source, status, details_json FROM market_sources
        WHERE julianday(fetched_at) IS NOT NULL AND julianday(fetched_at) <= julianday(?)
        ORDER BY source, fetched_at DESC, source_key DESC LIMIT ?
        """,
        (request.as_of.isoformat(), _MAX_SOURCE_ROWS + 1),
    ).fetchall()
    if len(rows) > _MAX_SOURCE_ROWS:
        return (_blocked_check("optional_source_coverage", "market source scan limit exceeded"),)
    latest: dict[str, tuple[str, object]] = {}
    parsed_rows: list[tuple[str, str, object]] = []
    for source, status, raw_details in rows:
        try:
            details = json.loads(raw_details)
        except (TypeError, json.JSONDecodeError):
            return (_blocked_check("optional_source_coverage", "market source details are invalid"),)
        parsed_rows.append((source, status, details))
        if source not in latest:
            latest[source] = (status, details)

    passed_sources: set[str] = set()
    optional_failures: list[str] = []
    required_failures: list[str] = []
    for source, (status, details) in latest.items():
        if status == "passed":
            passed_sources.add(source)
        elif status == "failed" and isinstance(details, dict) and details.get("optional") is True:
            optional_failures.append(source)
        elif status == "failed":
            required_failures.append(source)
        else:
            return (_blocked_check("market_source_state", "market source status is invalid"),)
    if required_failures:
        return (_blocked_check("market_source_state", f"{len(required_failures)} required market sources failed"),)
    if not optional_failures:
        return ()
    expected, calendar_error = _expected_observed_session(connection, request)
    if calendar_error is not None or expected is None:
        return (_blocked_check("optional_source_coverage", "authoritative calendar coverage is unavailable"),)
    cutoff = _three_year_cutoff(request.as_of.date())
    covered = all(
        _alternate_source_covers(
            connection, code, cutoff, expected, passed_sources, parsed_rows
        )
        for code in request.candidate_codes
    )
    if not covered:
        return (_blocked_check("optional_source_coverage", "optional source failed without proven alternate coverage"),)
    return (
        QualityResult(
            "optional_source_degradation", "warning", False,
            f"{len(optional_failures)} optional source failures covered by selected market source",
        ),
    )


def _alternate_source_covers(
    connection: sqlite3.Connection,
    code: str,
    cutoff: dt.date,
    expected: dt.date,
    passed_sources: set[str],
    source_rows: list[tuple[str, str, object]],
) -> bool:
    for source, status, details in source_rows:
        if (
            status != "passed"
            or source not in passed_sources
            or not isinstance(details, dict)
            or details.get("proof_type") != "historical_market_fetch"
            or details.get("code") != code
        ):
            continue
        try:
            start = dt.date.fromisoformat(details.get("start"))
            end = dt.date.fromisoformat(details.get("end"))
            actual_latest = dt.date.fromisoformat(details.get("actual_latest_session"))
        except (TypeError, ValueError):
            continue
        if start > cutoff or end < expected or actual_latest != expected:
            continue
        row = connection.execute(
            """
            SELECT MIN(trade_date), MAX(trade_date) FROM market_daily
            WHERE code = ? AND source = ? AND trade_date <= ? AND quality_status = 'passed'
            """,
            (code, source, expected.isoformat()),
        ).fetchone()
        try:
            earliest = dt.date.fromisoformat(row[0]) if row and row[0] else None
            latest = dt.date.fromisoformat(row[1]) if row and row[1] else None
        except (TypeError, ValueError):
            continue
        if earliest is not None and earliest <= cutoff and latest == expected:
            return True
    return False


def _persist_checks(
    connection: sqlite3.Connection,
    request: QualityRequest,
    checks: tuple[QualityResult, ...],
) -> None:
    started = not connection.in_transaction
    savepoint = "quality_check_replacement"
    try:
        connection.execute("BEGIN IMMEDIATE" if started else f"SAVEPOINT {savepoint}")
        connection.execute("DELETE FROM data_quality_checks WHERE run_id = ?", (request.run_id,))
        for check in checks:
            check_id = hashlib.sha256(
                f"a-hunter:quality:v1\0{request.run_id}\0{check.check_name}".encode("utf-8")
            ).hexdigest()
            connection.execute(
                """
                INSERT INTO data_quality_checks (
                  check_id, run_id, check_name, severity, status, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(check_id) DO UPDATE SET
                  severity = excluded.severity,
                  status = excluded.status,
                  details_json = excluded.details_json,
                  created_at = excluded.created_at
                """,
                (
                    check_id, request.run_id, check.check_name, check.severity,
                    "passed" if check.passed else "failed",
                    json.dumps({"details": check.details}, sort_keys=True, separators=(",", ":")),
                    request.as_of.isoformat(),
                ),
            )
        if started:
            connection.commit()
        else:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
    except BaseException:
        if started:
            connection.rollback()
        else:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def _collector_has_future_data(snapshot: CollectorSnapshot, as_of: dt.datetime) -> bool:
    try:
        if (
            not isinstance(snapshot.as_of, dt.datetime)
            or snapshot.as_of.tzinfo is None
            or snapshot.as_of.utcoffset() is None
            or snapshot.as_of > as_of
        ):
            return True
        for event in snapshot.events:
            timestamps = (event.received_at, event.source_created_at)
            if any(
                value is not None
                and (
                    not isinstance(value, dt.datetime)
                    or value.tzinfo is None
                    or value.utcoffset() is None
                    or value > as_of
                )
                for value in timestamps
            ):
                return True
            if any(
                not isinstance(media.downloaded_at, dt.datetime)
                or media.downloaded_at.tzinfo is None
                or media.downloaded_at.utcoffset() is None
                or media.downloaded_at > as_of
                for media in event.media
            ):
                return True
    except (AttributeError, TypeError, ValueError, OverflowError):
        return True
    return False


def _blocked_check(name: str, details: str) -> QualityResult:
    return QualityResult(name, "blocking", False, details)


def _three_year_cutoff(as_of_date: dt.date) -> dt.date:
    try:
        return as_of_date.replace(year=as_of_date.year - 3)
    except ValueError:
        return as_of_date.replace(year=as_of_date.year - 3, day=28)


def evaluate_quality(
    connection: sqlite3.Connection,
    required_codes: list[str],
    as_of: str,
) -> list[QualityResult]:
    as_of_date = dt.datetime.fromisoformat(as_of).date()
    cutoff = _three_year_cutoff(as_of_date)
    results = []
    for code in required_codes:
        row = connection.execute(
            "SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM market_daily WHERE code = ? AND trade_date <= ?",
            (code, as_of_date.isoformat()),
        ).fetchone()
        earliest = dt.date.fromisoformat(row[0]) if row and row[0] else None
        passed = earliest is not None and earliest <= cutoff and bool(row[1])
        results.append(QualityResult(
            f"three_year_history:{code}", "blocking", passed,
            f"{row[2] if row else 0} market_daily rows; cutoff {cutoff.isoformat()} satisfied={passed}",
        ))
    return results


def has_blocking_failure(
    connection: sqlite3.Connection,
    required_codes: list[str],
    as_of: str,
) -> bool:
    return any(result.blocking_failure for result in evaluate_quality(connection, required_codes, as_of))
