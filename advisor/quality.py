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

from advisor.agents.astock_adapter import ANALYST_ROLES
from advisor.evidence.mx_adapter import CollectorSnapshot
from advisor.ledger.model import LedgerTransaction, apply_transactions


_MAX_LEDGER_ROWS = 10_000
_MAX_SOURCE_ROWS = 1_000
_MAX_STALENESS_DAYS = 7
_VALID_RUN_TYPES = frozenset({"premarket", "review"})
_CODE_RE = re.compile(r"[03468]\d{5}\Z")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


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
    expected, error = _authoritative_expected_session(connection, request)
    if error is not None or expected is None:
        return _blocked_check("trading_calendar", error or "latest expected trading session is unavailable")
    missing = [
        code
        for code in request.candidate_codes
        if connection.execute(
            """
            SELECT 1 FROM market_daily
            WHERE code = ? AND trade_date = ? AND quality_status = 'passed'
            LIMIT 1
            """,
            (code, expected.isoformat()),
        ).fetchone()
        is None
    ]
    if missing:
        return _blocked_check(
            "trading_calendar",
            f"latest expected trading session {expected.isoformat()} is not covered by candidate rows",
        )
    return QualityResult(
        "trading_calendar", "blocking", True,
        f"latest expected trading session {expected.isoformat()} is covered",
    )


def _authoritative_expected_session(
    connection: sqlite3.Connection, request: QualityRequest
) -> tuple[dt.date | None, str | None]:
    rows = connection.execute(
        """
        SELECT source, details_json FROM market_sources
        WHERE status = 'passed' AND julianday(fetched_at) IS NOT NULL
          AND julianday(fetched_at) <= julianday(?)
        ORDER BY fetched_at DESC, source_key DESC
        LIMIT ?
        """,
        (request.as_of.isoformat(), _MAX_SOURCE_ROWS + 1),
    ).fetchall()
    if len(rows) > _MAX_SOURCE_ROWS:
        return None, "trading calendar proof scan limit exceeded"
    claims_by_code: dict[str, set[dt.date]] = defaultdict(set)
    saw_historical_proof = False
    for source, raw_details in rows:
        try:
            details = json.loads(raw_details)
            if not isinstance(details, dict) or details.get("proof_type") != "trading_calendar":
                continue
            proof_as_of = dt.datetime.fromisoformat(details.get("as_of"))
            if proof_as_of.tzinfo is None or proof_as_of.utcoffset() is None:
                raise ValueError("invalid calendar as_of")
            if proof_as_of > request.as_of:
                return None, "trading calendar proof is future-dated for this run"
            proof_date = proof_as_of.astimezone(request.as_of.tzinfo).date()
            if proof_date < request.as_of.date():
                saw_historical_proof = True
                continue
            if proof_date != request.as_of.date():
                return None, "trading calendar proof is stale or not current for this run"
            coverage = details.get("coverage_codes")
            scope = details.get("scope")
            if coverage is not None:
                if (
                    not isinstance(coverage, list)
                    or not coverage
                    or len(coverage) > 200
                    or len(set(coverage)) != len(coverage)
                    or any(not isinstance(code, str) or not _CODE_RE.fullmatch(code) for code in coverage)
                ):
                    raise ValueError("invalid calendar coverage")
                applicable_codes = set(request.candidate_codes).intersection(coverage)
            elif scope == "a_share":
                applicable_codes = set(request.candidate_codes)
            else:
                raise ValueError("invalid calendar coverage")
            if not applicable_codes:
                continue
            calendar_source = details.get("calendar_source")
            if (
                not isinstance(source, str)
                or not isinstance(calendar_source, str)
                or not _IDENTIFIER_RE.fullmatch(calendar_source)
                or source != calendar_source
            ):
                raise ValueError("invalid calendar source")
            claimed = dt.date.fromisoformat(details.get("latest_expected_session"))
            if claimed > request.as_of.date():
                return None, "latest expected trading session is future-dated"
            for code in applicable_codes:
                claims_by_code[code].add(claimed)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None, "trading calendar proof is invalid"

    if any(code not in claims_by_code for code in request.candidate_codes):
        if saw_historical_proof and not claims_by_code:
            return None, "trading calendar proof is stale or not current for this run"
        return None, "latest expected trading session is unavailable"
    if any(len(claims) != 1 for claims in claims_by_code.values()):
        return None, "conflicting trading calendar proof"
    expected_sessions = {next(iter(claims)) for claims in claims_by_code.values()}
    if len(expected_sessions) != 1:
        return None, "conflicting trading calendar proof"
    return next(iter(expected_sessions)), None


def _market_staleness_check(connection: sqlite3.Connection, request: QualityRequest) -> QualityResult:
    date = request.as_of.date()
    stale: list[str] = []
    for code in request.candidate_codes:
        row = connection.execute(
            "SELECT MAX(trade_date) FROM market_daily WHERE code = ? AND trade_date <= ? AND quality_status = 'passed'",
            (code, date.isoformat()),
        ).fetchone()
        try:
            latest = dt.date.fromisoformat(row[0]) if row and row[0] else None
        except (TypeError, ValueError):
            latest = None
        if latest is None or (date - latest).days > _MAX_STALENESS_DAYS:
            stale.append(code)
    return QualityResult(
        "market_staleness", "blocking", not stale,
        "candidate market data is current" if not stale else f"market data stale or missing for {len(stale)} candidates",
    )


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
    expected, calendar_error = _authoritative_expected_session(connection, request)
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
