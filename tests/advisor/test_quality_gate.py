import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from advisor.agents.astock_adapter import ANALYST_ROLES
from advisor.db.migrate import migrate_database
from advisor.evidence.mx_adapter import CollectorSnapshot, MediaMetadata, MxEvidence
from advisor.quality import QualityRequest, QualityResult, evaluate_run_quality


AS_OF = datetime(2026, 7, 12, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def collector(*, passed: bool = True) -> CollectorSnapshot:
    return CollectorSnapshot(
        events=(),
        quality=QualityResult(
            "collector_state",
            "blocking",
            passed,
            "collector snapshot is valid" if passed else "collector inactive",
        ),
        as_of=AS_OF,
    )


def quality_connection(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "advisor.sqlite"
    migrate_database(path)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) VALUES (?, ?, ?, 'running', ?)",
        ("run-15", "premarket", AS_OF.isoformat(), AS_OF.isoformat()),
    )
    rows = [
        ("600519", "2023-07-11"),
        ("600519", "2026-07-10"),
    ]
    for index, (code, trade_date) in enumerate(rows):
        connection.execute(
            """
            INSERT INTO market_daily (
              code, trade_date, open, high, low, close, volume, amount, source,
              fetched_at, as_of_date, content_hash, quality_status
            ) VALUES (?, ?, 1, 1, 1, 1, 1, 1, 'sina_http', ?, ?, ?, 'passed')
            """,
            (code, trade_date, AS_OF.isoformat(), trade_date, f"hash-{index}"),
        )
    for role in ANALYST_ROLES:
        connection.execute(
            """
            INSERT INTO analyst_outputs (output_id, run_id, role, code, as_of, summary, payload_json)
            VALUES (?, 'run-15', ?, '600519', ?, 'ready', '{}')
            """,
            (f"output-{role}", role, AS_OF.isoformat()),
        )
    connection.execute(
        """
        INSERT INTO market_sources (
          source_key, source, endpoint, params_hash, fetched_at, status, details_json
        ) VALUES ('calendar-proof', 'exchange_calendar', 'bounded', 'calendar', ?, 'passed', ?)
        """,
        (
            AS_OF.isoformat(),
            json.dumps(
                {
                    "as_of": AS_OF.isoformat(),
                    "calendar_source": "exchange_calendar",
                    "coverage_codes": ["600519"],
                    "latest_expected_session": "2026-07-10",
                    "proof_type": "trading_calendar",
                }
            ),
        ),
    )
    connection.execute(
        """
        INSERT INTO market_sources (
          source_key, source, endpoint, params_hash, fetched_at, status, details_json
        ) VALUES ('historical-fetch', 'sina_http', 'bounded', 'fetch', ?, 'passed', ?)
        """,
        (
            AS_OF.isoformat(),
            json.dumps(
                {
                    "actual_latest_session": "2026-07-10",
                    "code": "600519",
                    "end": "2026-07-12",
                    "proof_type": "historical_market_fetch",
                    "start": "2023-07-11",
                }
            ),
        ),
    )
    connection.commit()
    return connection


def request(**changes) -> QualityRequest:
    values = {
        "run_id": "run-15",
        "run_type": "premarket",
        "as_of": AS_OF,
        "candidate_codes": ("600519",),
        "collector": collector(),
    }
    values.update(changes)
    return QualityRequest(**values)


def test_complete_quality_gate_passes_and_persists_all_required_checks(tmp_path: Path):
    connection = quality_connection(tmp_path)

    result = evaluate_run_quality(connection, request())

    assert result.status == "passed"
    assert {check.check_name for check in result.checks} == {
        "collector_state",
        "trading_calendar",
        "market_staleness",
        "three_year_candidate_coverage",
        "future_data_leakage",
        "ledger_replay",
        "analyst_contract_readiness",
    }
    persisted = connection.execute(
        "SELECT check_name, status, details_json FROM data_quality_checks WHERE run_id = 'run-15'"
    ).fetchall()
    assert len(persisted) == 7
    assert all(status == "passed" for _, status, _ in persisted)
    assert all(isinstance(json.loads(details), dict) for _, _, details in persisted)


def test_missing_or_invalid_request_input_blocks_and_is_persisted(tmp_path: Path):
    connection = quality_connection(tmp_path)

    result = evaluate_run_quality(connection, request(candidate_codes=()))

    assert result.status == "blocked"
    assert any(check.blocking_failure for check in result.checks)
    assert connection.execute(
        "SELECT count(*) FROM data_quality_checks WHERE run_id = 'run-15' AND status = 'failed'"
    ).fetchone()[0] > 0


def test_collector_failure_blocks_run(tmp_path: Path):
    connection = quality_connection(tmp_path)

    result = evaluate_run_quality(connection, request(collector=collector(passed=False)))

    assert result.status == "blocked"
    assert next(check for check in result.checks if check.check_name == "collector_state").blocking_failure


def test_market_staleness_and_missing_three_year_history_block(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.execute("DELETE FROM market_daily WHERE trade_date = '2023-07-11'")
    connection.execute("UPDATE market_daily SET trade_date = '2026-06-01', as_of_date = '2026-06-01'")
    connection.commit()

    result = evaluate_run_quality(connection, request())

    assert result.status == "blocked"
    assert next(check for check in result.checks if check.check_name == "market_staleness").blocking_failure
    assert next(check for check in result.checks if check.check_name == "three_year_candidate_coverage").blocking_failure


def test_future_data_leakage_blocks(tmp_path: Path):
    connection = quality_connection(tmp_path)
    future = (AS_OF + timedelta(days=1)).date().isoformat()
    connection.execute(
        """
        INSERT INTO market_daily (
          code, trade_date, open, high, low, close, volume, amount, source,
          fetched_at, as_of_date, content_hash, quality_status
        ) VALUES ('600519', ?, 1, 1, 1, 1, 1, 1, 'sina_http', ?, ?, 'future', 'passed')
        """,
        (future, AS_OF.isoformat(), future),
    )
    connection.commit()

    result = evaluate_run_quality(connection, request())

    assert result.status == "blocked"
    assert next(check for check in result.checks if check.check_name == "future_data_leakage").blocking_failure


def test_future_market_fetch_timestamp_blocks(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.execute(
        "UPDATE market_daily SET fetched_at = ?",
        ((AS_OF + timedelta(seconds=1)).isoformat(),),
    )
    connection.commit()

    result = evaluate_run_quality(connection, request())

    assert result.status == "blocked"
    assert next(check for check in result.checks if check.check_name == "future_data_leakage").blocking_failure


def test_future_snapshot_source_and_media_timestamps_block_future_leakage(tmp_path: Path):
    connection = quality_connection(tmp_path)
    event = MxEvidence(
        evidence_id="e" * 64,
        source_type="mx",
        source_id="event-1",
        rid=123,
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
    future_collector = replace(
        collector(),
        events=(event,),
        as_of=AS_OF + timedelta(seconds=1),
    )

    result = evaluate_run_quality(connection, request(collector=future_collector))

    assert result.status == "blocked"
    assert next(
        check for check in result.checks if check.check_name == "future_data_leakage"
    ).blocking_failure


def test_trading_calendar_blocks_when_candidate_misses_latest_expected_session(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.execute(
        "UPDATE market_daily SET trade_date = '2026-07-09', as_of_date = '2026-07-09' "
        "WHERE trade_date = '2026-07-10'"
    )
    connection.commit()

    result = evaluate_run_quality(connection, request())

    check = next(check for check in result.checks if check.check_name == "trading_calendar")
    assert check.blocking_failure
    assert "2026-07-10" in check.details


def test_trading_calendar_passes_when_candidate_covers_latest_expected_session(tmp_path: Path):
    connection = quality_connection(tmp_path)

    result = evaluate_run_quality(connection, request())

    check = next(check for check in result.checks if check.check_name == "trading_calendar")
    assert check.passed
    assert "2026-07-10" in check.details


def test_incomplete_historical_fetch_cannot_self_authorize_calendar_freshness(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.execute("DELETE FROM market_sources")
    connection.execute(
        """
        INSERT INTO market_sources (
          source_key, source, endpoint, params_hash, fetched_at, status, details_json
        ) VALUES ('fetch-only', 'sina_http', 'bounded', 'fetch', ?, 'passed', ?)
        """,
        (
            AS_OF.isoformat(),
            json.dumps(
                {
                    "actual_latest_session": "2026-07-10",
                    "code": "600519",
                    "end": "2026-07-12",
                    "proof_type": "historical_market_fetch",
                    "start": "2023-07-11",
                }
            ),
        ),
    )
    connection.commit()

    result = evaluate_run_quality(connection, request())

    assert next(
        check for check in result.checks if check.check_name == "trading_calendar"
    ).blocking_failure


def test_unrelated_source_row_cannot_supply_calendar_proof(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.execute("DELETE FROM market_sources")
    connection.execute(
        """
        INSERT INTO market_sources (
          source_key, source, endpoint, params_hash, fetched_at, status, details_json
        ) VALUES ('unrelated', 'news_feed', 'bounded', 'other', ?, 'passed', ?)
        """,
        (AS_OF.isoformat(), json.dumps({"latest_expected_session": "2026-07-10"})),
    )
    connection.commit()

    result = evaluate_run_quality(connection, request())

    assert next(
        check for check in result.checks if check.check_name == "trading_calendar"
    ).blocking_failure


def test_authoritative_proof_for_unrelated_code_is_ignored(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.execute(
        """
        INSERT INTO market_sources (
          source_key, source, endpoint, params_hash, fetched_at, status, details_json
        ) VALUES ('other-code', 'sina_http', 'bounded', 'other-code', ?, 'passed', ?)
        """,
        (
            AS_OF.isoformat(),
            json.dumps(
                {
                    "as_of": AS_OF.isoformat(),
                    "calendar_source": "exchange_calendar",
                    "coverage_codes": ["000001"],
                    "latest_expected_session": "2026-07-10",
                    "proof_type": "trading_calendar",
                }
            ),
        ),
    )
    connection.commit()

    result = evaluate_run_quality(connection, request())

    assert next(
        check for check in result.checks if check.check_name == "trading_calendar"
    ).passed


def test_conflicting_current_calendar_claims_block(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.execute(
        """
        INSERT INTO market_daily (
          code, trade_date, open, high, low, close, volume, amount, source,
          fetched_at, as_of_date, content_hash, quality_status
        ) VALUES ('600519', '2026-07-09', 1, 1, 1, 1, 1, 1, 'other_source', ?,
                  '2026-07-09', 'other-hash', 'passed')
        """,
        (AS_OF.isoformat(),),
    )
    connection.execute(
        """
        INSERT INTO market_sources (
          source_key, source, endpoint, params_hash, fetched_at, status, details_json
        ) VALUES ('other-proof', 'other_calendar', 'bounded', 'other', ?, 'passed', ?)
        """,
        (
            AS_OF.isoformat(),
            json.dumps(
                {
                    "as_of": AS_OF.isoformat(),
                    "calendar_source": "other_calendar",
                    "coverage_codes": ["600519"],
                    "latest_expected_session": "2026-07-09",
                    "proof_type": "trading_calendar",
                }
            ),
        ),
    )
    connection.commit()

    result = evaluate_run_quality(connection, request())

    check = next(check for check in result.checks if check.check_name == "trading_calendar")
    assert check.blocking_failure
    assert "conflict" in check.details


def test_historical_calendar_proof_is_ignored_when_current_proof_is_available(tmp_path: Path):
    connection = quality_connection(tmp_path)
    historical_as_of = AS_OF - timedelta(days=1)
    connection.execute(
        """
        INSERT INTO market_sources (
          source_key, source, endpoint, params_hash, fetched_at, status, details_json
        ) VALUES ('historical-calendar-proof', 'historical_calendar', 'bounded', 'historical', ?, 'passed', ?)
        """,
        (
            historical_as_of.isoformat(),
            json.dumps(
                {
                    "as_of": historical_as_of.isoformat(),
                    "calendar_source": "historical_calendar",
                    "coverage_codes": ["600519"],
                    "latest_expected_session": "2026-07-09",
                    "proof_type": "trading_calendar",
                }
            ),
        ),
    )
    connection.commit()

    result = evaluate_run_quality(connection, request())

    assert next(check for check in result.checks if check.check_name == "trading_calendar").passed


@pytest.mark.parametrize("run_id", ("token=secret", "../escape", "run\nid"))
def test_unsafe_quality_request_run_id_blocks_without_persisting_checks(tmp_path: Path, run_id: str):
    connection = quality_connection(tmp_path)

    result = evaluate_run_quality(connection, request(run_id=run_id))

    assert result.status == "blocked"
    assert any(check.blocking_failure for check in result.checks)
    assert connection.execute("SELECT count(*) FROM data_quality_checks").fetchone()[0] == 0


def test_calendar_proof_newer_than_selected_source_data_blocks(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.execute(
        "UPDATE market_sources SET details_json = ? WHERE source_key = 'calendar-proof'",
        (
            json.dumps(
                {
                    "as_of": AS_OF.isoformat(),
                    "calendar_source": "exchange_calendar",
                    "coverage_codes": ["600519"],
                    "latest_expected_session": "2026-07-11",
                    "proof_type": "trading_calendar",
                }
            ),
        ),
    )
    connection.commit()

    result = evaluate_run_quality(connection, request())

    check = next(check for check in result.checks if check.check_name == "trading_calendar")
    assert check.blocking_failure
    assert "2026-07-11" in check.details


def test_stale_calendar_proof_blocks_current_run(tmp_path: Path):
    connection = quality_connection(tmp_path)
    details = json.loads(
        connection.execute(
            "SELECT details_json FROM market_sources WHERE source_key = 'calendar-proof'"
        ).fetchone()[0]
    )
    details["as_of"] = (AS_OF - timedelta(days=1)).isoformat()
    connection.execute(
        "UPDATE market_sources SET details_json = ? WHERE source_key = 'calendar-proof'",
        (json.dumps(details),),
    )
    connection.commit()

    result = evaluate_run_quality(connection, request())

    check = next(check for check in result.checks if check.check_name == "trading_calendar")
    assert check.blocking_failure
    assert "current" in check.details or "stale" in check.details


def test_invalid_ledger_replay_blocks(tmp_path: Path):
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
        ) VALUES ('sell-first', 'default', '2026-07-10', 'sell', '600519', 100, 1, 100, 0, 'manual', ?)
        """,
        (AS_OF.isoformat(),),
    )
    connection.commit()

    result = evaluate_run_quality(connection, request())

    assert result.status == "blocked"
    assert next(check for check in result.checks if check.check_name == "ledger_replay").blocking_failure


def test_malformed_ledger_values_block_replay(tmp_path: Path):
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
        ) VALUES ('bad-buy', 'default', '2026-07-10', 'buy', '600519', -1, 1, -1, 0, 'manual', ?)
        """,
        (AS_OF.isoformat(),),
    )
    connection.commit()

    result = evaluate_run_quality(connection, request())

    assert result.status == "blocked"
    assert next(check for check in result.checks if check.check_name == "ledger_replay").blocking_failure


def test_missing_required_analyst_role_blocks(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.execute("DELETE FROM analyst_outputs WHERE role = 'portfolio_manager'")
    connection.commit()

    result = evaluate_run_quality(connection, request())

    assert result.status == "blocked"
    assert next(check for check in result.checks if check.check_name == "analyst_contract_readiness").blocking_failure


def test_optional_source_failure_is_warning_only_when_selected_source_covers_request(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.executemany(
        """
        INSERT INTO market_sources (
          source_key, source, endpoint, params_hash, fetched_at, status, details_json
        ) VALUES (?, ?, 'bounded', ?, ?, ?, ?)
        """,
        [
            (
                "optional-failed",
                "optional_news",
                "p1",
                AS_OF.isoformat(),
                "failed",
                json.dumps({"optional": True, "codes": ["600519"]}),
            ),
            (
                "selected-passed",
                "sina_http",
                "p2",
                AS_OF.isoformat(),
                "passed",
                json.dumps(
                    {
                        "actual_latest_session": "2026-07-10",
                        "code": "600519",
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

    assert result.status == "passed"
    warnings = [check for check in result.checks if check.severity == "warning"]
    assert len(warnings) == 1
    assert warnings[0].passed is False


def test_optional_source_single_recent_row_does_not_prove_interval_coverage(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.execute("DELETE FROM market_daily WHERE trade_date = '2023-07-11'")
    connection.executemany(
        """
        INSERT INTO market_sources (
          source_key, source, endpoint, params_hash, fetched_at, status, details_json
        ) VALUES (?, ?, 'bounded', ?, ?, ?, ?)
        """,
        [
            ("optional-failed", "optional_news", "p1", AS_OF.isoformat(), "failed", '{"optional":true}'),
            (
                "selected-passed",
                "sina_http",
                "p2",
                AS_OF.isoformat(),
                "passed",
                json.dumps(
                    {
                        "actual_latest_session": "2026-07-10",
                        "code": "600519",
                        "end": "2026-07-12",
                        "proof_type": "historical_market_fetch",
                        "start": "2026-07-10",
                    }
                ),
            ),
        ],
    )
    connection.commit()

    result = evaluate_run_quality(connection, request())

    assert next(
        check for check in result.checks if check.check_name == "optional_source_coverage"
    ).blocking_failure


def test_optional_source_unrelated_code_fetch_is_ignored(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.execute("UPDATE market_daily SET source = 'other_source'")
    connection.executemany(
        """
        INSERT INTO market_sources (
          source_key, source, endpoint, params_hash, fetched_at, status, details_json
        ) VALUES (?, ?, 'bounded', ?, ?, ?, ?)
        """,
        [
            ("optional-failed", "optional_news", "p1", AS_OF.isoformat(), "failed", '{"optional":true}'),
            (
                "unrelated-passed",
                "other_source",
                "p2",
                AS_OF.isoformat(),
                "passed",
                json.dumps(
                    {
                        "actual_latest_session": "2026-07-10",
                        "code": "000001",
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

    assert next(
        check for check in result.checks if check.check_name == "optional_source_coverage"
    ).blocking_failure


def test_optional_source_failure_blocks_when_passing_source_does_not_supply_candidate_data(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.execute("UPDATE market_daily SET source = 'different_source'")
    connection.executemany(
        """
        INSERT INTO market_sources (
          source_key, source, endpoint, params_hash, fetched_at, status, details_json
        ) VALUES (?, ?, 'bounded', ?, ?, ?, ?)
        """,
        [
            ("optional-failed", "optional_news", "p1", AS_OF.isoformat(), "failed", '{"optional":true}'),
            ("selected-passed", "sina_http", "p2", AS_OF.isoformat(), "passed", '{}'),
        ],
    )
    connection.commit()

    result = evaluate_run_quality(connection, request())

    assert result.status == "blocked"
    assert next(check for check in result.checks if check.check_name == "optional_source_coverage").blocking_failure


def test_failed_required_market_source_blocks(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.execute(
        """
        INSERT INTO market_sources (
          source_key, source, endpoint, params_hash, fetched_at, status, details_json
        ) VALUES ('required-failed', 'sina_http', 'bounded', 'p1', ?, 'failed', '{}')
        """,
        (AS_OF.isoformat(),),
    )
    connection.commit()

    result = evaluate_run_quality(connection, request())

    assert result.status == "blocked"
    assert next(check for check in result.checks if check.check_name == "market_source_state").blocking_failure


def test_quality_persistence_is_idempotent(tmp_path: Path):
    connection = quality_connection(tmp_path)

    first = evaluate_run_quality(connection, request())
    second = evaluate_run_quality(connection, request())

    assert first == second
    assert connection.execute(
        "SELECT count(*) FROM data_quality_checks WHERE run_id = 'run-15'"
    ).fetchone()[0] == len(first.checks)


def test_quality_persistence_replaces_obsolete_optional_failure(tmp_path: Path):
    connection = quality_connection(tmp_path)
    connection.execute(
        """
        INSERT INTO market_sources (
          source_key, source, endpoint, params_hash, fetched_at, status, details_json
        ) VALUES ('optional-failed', 'optional_news', 'bounded', 'optional', ?, 'failed', ?)
        """,
        (AS_OF.isoformat(), json.dumps({"optional": True})),
    )
    connection.commit()

    first = evaluate_run_quality(connection, request())
    assert any(check.check_name == "optional_source_degradation" for check in first.checks)

    connection.execute("DELETE FROM market_sources WHERE source = 'optional_news'")
    connection.commit()
    second = evaluate_run_quality(connection, request())

    assert all(check.check_name != "optional_source_degradation" for check in second.checks)
    assert connection.execute(
        """
        SELECT count(*) FROM data_quality_checks
        WHERE run_id = 'run-15' AND check_name = 'optional_source_degradation'
        """
    ).fetchone()[0] == 0
