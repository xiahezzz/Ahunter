import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from advisor.calendar import (
    ObservedTradingSessionsUnavailableError,
    is_trading_session,
    latest_expected_session,
)
from advisor.db.migrate import migrate_database
from advisor.evidence.mx_adapter import CollectorSnapshot, MediaMetadata, MxEvidence
from advisor.market_daily.contracts import (
    MarketAbsence,
    ObservedTradingSession,
    SessionObservationReceipt,
)
from advisor.quality import ANALYST_ROLES, QualityRequest, QualityResult, evaluate_run_quality


SHANGHAI = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 7, 12, 8, 30, tzinfo=SHANGHAI)
CODE = "600519"


def collector(*, passed: bool = True) -> CollectorSnapshot:
    return CollectorSnapshot(
        events=(),
        quality=QualityResult(
            "collector_state",
            "blocking",
            passed,
            "采集状态正常" if passed else "采集器未就绪",
        ),
        as_of=AS_OF,
        allowed_rids=(),
    )


def insert_session(connection: sqlite3.Connection, trade_date: date, observed_at: datetime) -> None:
    fact = ObservedTradingSession(
        trade_date=trade_date,
        primary_source="eastmoney",
        fallback_source="tdx",
        primary_observed_at=observed_at,
        fallback_observed_at=observed_at,
        fetched_at=observed_at,
    )
    connection.execute(
        """
        INSERT INTO trading_sessions (
          trade_date, primary_source, fallback_source, primary_observed_at,
          fallback_observed_at, fetched_at, content_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fact.trade_date.isoformat(),
            fact.primary_source,
            fact.fallback_source,
            fact.primary_observed_at.isoformat(),
            fact.fallback_observed_at.isoformat(),
            fact.fetched_at.isoformat(),
            fact.content_hash,
        ),
    )


def insert_observation(
    connection: sqlite3.Connection,
    observed_at: datetime,
    latest_session: date | None,
    *,
    session_set_hash: str | None = None,
) -> None:
    receipt = SessionObservationReceipt(
        observed_at=observed_at,
        primary_source="eastmoney",
        fallback_source="tdx",
        latest_session=latest_session,
        session_set_hash=session_set_hash or hashlib.sha256(b"quality-session-fixture").hexdigest(),
    )
    connection.execute(
        """
        INSERT INTO trading_session_observations (
          observation_id, observed_at, primary_source, fallback_source, latest_session,
          session_set_hash, content_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            receipt.content_hash,
            receipt.observed_at.isoformat(),
            receipt.primary_source,
            receipt.fallback_source,
            receipt.latest_session.isoformat() if receipt.latest_session else None,
            receipt.session_set_hash,
            receipt.content_hash,
        ),
    )


def insert_bar(connection: sqlite3.Connection, trade_date: date, *, content_hash: str) -> None:
    connection.execute(
        """
        INSERT INTO market_daily (
          code, trade_date, open, high, low, close, volume, amount, source,
          source_at, fetched_at, as_of_date, content_hash, quality_status
        ) VALUES (?, ?, 1, 1, 1, 1, 1, 1, 'eastmoney', ?, ?, ?, ?, 'passed')
        """,
        (CODE, trade_date.isoformat(), AS_OF.isoformat(), AS_OF.isoformat(), trade_date.isoformat(), content_hash),
    )


def quality_connection(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "advisor.sqlite"
    migrate_database(path)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) VALUES (?, ?, ?, 'running', ?)",
        ("run-15", "premarket", AS_OF.isoformat(), AS_OF.isoformat()),
    )
    insert_session(connection, date(2023, 7, 11), datetime(2023, 7, 11, 21, 0, tzinfo=SHANGHAI))
    insert_session(connection, date(2026, 7, 10), datetime(2026, 7, 10, 21, 0, tzinfo=SHANGHAI))
    # Saturday's no-op receipt explicitly proves that Friday remained the latest session.
    insert_observation(connection, datetime(2026, 7, 11, 21, 0, tzinfo=SHANGHAI), date(2026, 7, 10))
    insert_bar(connection, date(2023, 7, 11), content_hash="history")
    insert_bar(connection, date(2026, 7, 10), content_hash="latest")
    for role in ANALYST_ROLES:
        connection.execute(
            """
            INSERT INTO analyst_outputs (output_id, run_id, role, code, as_of, summary, payload_json)
            VALUES (?, 'run-15', ?, ?, ?, 'ready', '{}')
            """,
            (f"output-{role}", role, CODE, AS_OF.isoformat()),
        )
    connection.commit()
    return connection


def request(**changes: object) -> QualityRequest:
    values: dict[str, object] = {
        "run_id": "run-15",
        "run_type": "premarket",
        "as_of": AS_OF,
        "candidate_codes": (CODE,),
        "collector": collector(),
    }
    values.update(changes)
    return QualityRequest(**values)  # type: ignore[arg-type]


def check(result, name: str) -> QualityResult:
    return next(item for item in result.checks if item.check_name == name)


def test_calendar_facade_uses_only_explicit_observed_sessions_and_supports_future_years():
    sessions = (date(2026, 12, 31), date(2027, 1, 4))

    assert latest_expected_session(
        datetime(2027, 1, 4, 22, 0, tzinfo=SHANGHAI), sessions
    ) == date(2027, 1, 4)
    assert latest_expected_session(
        datetime(2027, 1, 4, 8, 30, tzinfo=SHANGHAI), sessions
    ) == date(2026, 12, 31)
    assert is_trading_session(date(2027, 1, 4), sessions)
    assert not is_trading_session(date(2027, 1, 1), sessions)
    with pytest.raises(ObservedTradingSessionsUnavailableError):
        latest_expected_session(datetime(2027, 1, 4, 22, 0, tzinfo=SHANGHAI))


def test_complete_quality_gate_uses_observed_sessions_and_persists_checks(tmp_path: Path):
    connection = quality_connection(tmp_path)

    result = evaluate_run_quality(connection, request())

    assert result.status == "passed"
    assert check(result, "trading_calendar").passed
    assert "2026-07-10" in check(result, "trading_calendar").details
    assert check(result, "market_staleness").passed
    persisted = connection.execute(
        "SELECT check_name, status FROM data_quality_checks WHERE run_id = 'run-15'"
    ).fetchall()
    assert len(persisted) == len(result.checks)
    assert all(status == "passed" for _, status in persisted)


def test_quality_gate_blocks_when_no_observed_session_facts_exist(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.execute("DELETE FROM trading_session_observations")
    connection.execute("DELETE FROM trading_sessions")
    connection.commit()

    result = evaluate_run_quality(connection, request())

    assert result.status == "blocked"
    assert check(result, "trading_calendar").blocking_failure
    assert "观测" in check(result, "trading_calendar").details


def test_quality_gate_does_not_mistake_a_missing_heartbeat_for_a_market_closure(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.execute("DELETE FROM trading_session_observations")
    connection.commit()

    result = evaluate_run_quality(connection, request())

    assert check(result, "trading_calendar").blocking_failure
    assert "观测" in check(result, "trading_calendar").details


def test_weekend_noop_receipt_keeps_quality_available_without_hardcoded_holidays(tmp_path: Path):
    connection = quality_connection(tmp_path)
    as_of = datetime(2026, 7, 12, 21, 30, tzinfo=SHANGHAI)
    insert_observation(connection, datetime(2026, 7, 12, 21, 0, tzinfo=SHANGHAI), date(2026, 7, 10))
    connection.commit()

    result = evaluate_run_quality(connection, request(as_of=as_of))

    assert check(result, "trading_calendar").passed
    assert check(result, "market_staleness").passed


def test_after_2100_requires_that_calendar_days_dual_source_receipt(tmp_path: Path):
    connection = quality_connection(tmp_path)
    as_of = datetime(2026, 7, 12, 21, 30, tzinfo=SHANGHAI)

    result = evaluate_run_quality(connection, request(as_of=as_of))

    assert check(result, "trading_calendar").blocking_failure
    assert "心跳" in check(result, "trading_calendar").details


def test_candidate_without_latest_observed_session_bar_is_blocked(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.execute("DELETE FROM market_daily WHERE trade_date = '2026-07-10'")
    connection.commit()

    result = evaluate_run_quality(connection, request())

    assert check(result, "trading_calendar").blocking_failure
    assert check(result, "market_staleness").blocking_failure


def test_evidenced_suspension_counts_as_latest_session_coverage(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.execute("DELETE FROM market_daily WHERE trade_date = '2026-07-10'")
    absence = MarketAbsence(
        code=CODE,
        trade_date=date(2026, 7, 10),
        reason="suspended",
        source="eastmoney",
        source_at=datetime(2026, 7, 10, 21, 0, tzinfo=SHANGHAI),
        fetched_at=datetime(2026, 7, 10, 21, 0, tzinfo=SHANGHAI),
    )
    connection.execute(
        """
        INSERT INTO market_daily_absences (
          code, trade_date, reason, source, source_at, fetched_at, content_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            absence.code,
            absence.trade_date.isoformat(),
            absence.reason,
            absence.source,
            absence.source_at.isoformat(),
            absence.fetched_at.isoformat(),
            absence.content_hash,
        ),
    )
    connection.commit()

    result = evaluate_run_quality(connection, request())

    assert check(result, "trading_calendar").passed
    assert check(result, "market_staleness").passed


def test_invalid_session_fact_or_receipt_hash_blocks_quality(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.execute("UPDATE trading_sessions SET content_hash = '0' || substr(content_hash, 2)")
    connection.commit()

    result = evaluate_run_quality(connection, request())

    assert check(result, "trading_calendar").blocking_failure
    assert "无效" in check(result, "trading_calendar").details


def test_future_session_observation_is_detected_as_future_data(tmp_path: Path):
    connection = quality_connection(tmp_path)
    insert_observation(connection, AS_OF + timedelta(seconds=1), date(2026, 7, 10))
    connection.commit()

    result = evaluate_run_quality(connection, request())

    assert check(result, "future_data_leakage").blocking_failure


def test_market_staleness_and_three_year_history_fail_independently(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.execute("DELETE FROM market_daily WHERE trade_date = '2023-07-11'")
    connection.execute("DELETE FROM market_daily WHERE trade_date = '2026-07-10'")
    connection.commit()

    result = evaluate_run_quality(connection, request())

    assert check(result, "market_staleness").blocking_failure
    assert check(result, "three_year_candidate_coverage").blocking_failure


def test_future_market_and_collector_records_block_quality(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.execute("UPDATE market_daily SET fetched_at = ?", ((AS_OF + timedelta(seconds=1)).isoformat(),))
    connection.commit()

    result = evaluate_run_quality(connection, request())

    assert check(result, "future_data_leakage").blocking_failure

    event = MxEvidence(
        evidence_id="e" * 64,
        source_type="mx",
        source_id="event-1",
        rid=1,
        content_hash="c" * 64,
        summary="关注 600519",
        received_at=AS_OF,
        source_created_at=AS_OF + timedelta(seconds=1),
        media=(
            MediaMetadata(
                content_hash="m" * 64,
                content_type="image/jpeg",
                local_path="data/events/media/a.jpg",
                downloaded_at=AS_OF + timedelta(seconds=1),
            ),
        ),
    )
    future_collector = replace(collector(), events=(event,), as_of=AS_OF + timedelta(seconds=1))
    result = evaluate_run_quality(connection, request(collector=future_collector))
    assert check(result, "future_data_leakage").blocking_failure


def test_invalid_ledger_and_missing_analyst_role_block(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.execute(
        "INSERT INTO ledger_accounts (account_id, name, created_at) VALUES ('default', 'default', ?)",
        (AS_OF.isoformat(),),
    )
    connection.execute(
        """
        INSERT INTO ledger_transactions (
          transaction_id, account_id, trade_date, transaction_type, code, quantity,
          price, amount, fees, source, created_at
        ) VALUES ('sell-first', 'default', '2026-07-10', 'sell', ?, 100, 1, 100, 0, 'manual', ?)
        """,
        (CODE, AS_OF.isoformat()),
    )
    connection.execute("DELETE FROM analyst_outputs WHERE role = 'market'")
    connection.commit()

    result = evaluate_run_quality(connection, request())

    assert check(result, "ledger_replay").blocking_failure
    assert check(result, "analyst_contract_readiness").blocking_failure


def test_optional_source_warning_requires_a_proven_alternate_market_source(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.executemany(
        """
        INSERT INTO market_sources (
          source_key, source, endpoint, params_hash, fetched_at, status, details_json
        ) VALUES (?, ?, 'bounded', ?, ?, ?, ?)
        """,
        [
            ("optional-failed", "optional_news", "p1", AS_OF.isoformat(), "failed", json.dumps({"optional": True})),
            (
                "selected-passed",
                "eastmoney",
                "p2",
                AS_OF.isoformat(),
                "passed",
                json.dumps(
                    {
                        "actual_latest_session": "2026-07-10",
                        "code": CODE,
                        "end": "2026-07-12",
                        "proof_type": "historical_market_fetch",
                        "start": "2023-07-11",
                    }
                ),
            ),
        ],
    )
    connection.commit()

    result = evaluate_run_quality(connection, request())

    warning = next(item for item in result.checks if item.check_name == "optional_source_degradation")
    assert warning.severity == "warning"
    assert result.status == "passed"


def test_invalid_request_blocks_and_valid_persistence_is_idempotent(tmp_path: Path):
    connection = quality_connection(tmp_path)

    invalid = evaluate_run_quality(connection, request(candidate_codes=()))
    assert invalid.status == "blocked"

    first = evaluate_run_quality(connection, request())
    second = evaluate_run_quality(connection, request())
    assert first == second
    assert connection.execute(
        "SELECT count(*) FROM data_quality_checks WHERE run_id = 'run-15'"
    ).fetchone()[0] == len(first.checks)
