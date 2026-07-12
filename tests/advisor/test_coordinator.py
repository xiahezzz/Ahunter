import json
import sqlite3
from types import SimpleNamespace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
import pytest
from fastapi.testclient import TestClient

from advisor import paths as advisor_paths
from advisor import coordinator as coordinator_module
from advisor.agents.astock_adapter import (
    ANALYST_ROLES,
    AnalystOutput,
    DataQualityBlockedError,
    QualityOutcome,
)
from advisor.coordinator import run_premarket, run_review
from advisor.evidence.mx_adapter import CollectorSnapshot
from advisor.evidence.service import EvidenceRecord
from advisor.quality import QualityGateResult, QualityResult
from advisor.reporting import premarket as premarket_reporting
from advisor.reporting import review as review_reporting
from advisor.reporting.contracts import AdviceItem
from advisor.reporting.premarket import write_premarket_report
from advisor.web.api import create_app


AS_OF = datetime(2026, 7, 12, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
CODE = "600519"
EVIDENCE_ID = "e" * 64


@pytest.fixture(autouse=True)
def configured_reports_root(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: tmp_path / "reports")


def collector(*, passed: bool = True) -> CollectorSnapshot:
    return CollectorSnapshot(
        events=(),
        quality=QualityResult(
            "collector_state", "blocking", passed,
            "collector ready" if passed else "collector unavailable secret=hidden",
        ),
        as_of=AS_OF,
        allowed_rids=(),
    )


def passed_quality(*_args) -> QualityGateResult:
    return QualityGateResult(
        "passed",
        (QualityResult("coordinator_fixture", "blocking", True, "ready"),),
    )


def blocked_quality(*_args) -> QualityGateResult:
    return QualityGateResult(
        "blocked",
        (QualityResult("collector_state", "blocking", False, "secret=hidden"),),
    )


class PassingRunner:
    def run(self, code: str, trade_date: str, evidence: list[dict]) -> list[AnalystOutput]:
        assert trade_date == "2026-07-12"
        assert evidence[0]["evidence_id"] == EVIDENCE_ID
        return [
            AnalystOutput(
                role=role,
                code=code,
                summary=f"{role} summary",
                payload={
                    "quality_outcome": QualityOutcome(True, "hard checks passed")
                } if role == "quality_gate" else {"signal": role},
            )
            for role in ANALYST_ROLES
        ]


class BlockingRunner:
    def run(self, code: str, trade_date: str, evidence: list[dict]) -> list[AnalystOutput]:
        raise DataQualityBlockedError("provider token=must-not-archive")


def persist_fixture_evidence(connection, run_id, snapshot, *, as_of):
    record = EvidenceRecord(
        EVIDENCE_ID, run_id, CODE, as_of.isoformat(), "mx", "event-1", "关注 600519",
    )
    connection.execute(
        """
        INSERT INTO evidence (
          evidence_id, run_id, code, as_of, source_type, source_id, summary,
          confidence, facts_json, inferences_json, conflicts_json, quality_flags_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0.5, '[]', '[]', '[]', '[]')
        """,
        (record.evidence_id, run_id, record.code, record.as_of,
         record.source_type, record.source_id, record.summary),
    )
    connection.commit()
    return [record]


def seed_market(db_path: Path) -> None:
    connection = sqlite3.connect(db_path)
    connection.executemany(
        """
        INSERT INTO market_daily (
          code, trade_date, open, high, low, close, volume, amount, source,
          fetched_at, as_of_date, content_hash, quality_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 10000, 'fixture', ?, ?, ?, 'passed')
        """,
        [
            (CODE, "2026-07-10", 10, 11, 9, 10.5, 1000, AS_OF.isoformat(), "2026-07-10", "h1"),
            (CODE, "2026-07-11", 10.5, 12, 10, 11.5, 1200, AS_OF.isoformat(), "2026-07-11", "h2"),
        ],
    )
    connection.commit()
    connection.close()


def coordinator_paths(tmp_path: Path) -> dict:
    return {
        "db_path": tmp_path / "state" / "advisor.sqlite",
        "chart_dir": tmp_path / "charts",
        "profile_dir": tmp_path / "profiles",
        "output_dir": tmp_path / "reports",
    }


def query_all(db_path: Path, sql: str):
    connection = sqlite3.connect(db_path)
    rows = connection.execute(sql).fetchall()
    connection.close()
    return rows


def seed_premarket_report(
    paths: dict,
    *,
    database_run_id: str,
    report_run_id: str,
    advice: list[AdviceItem],
    supersedes: str | None = None,
) -> None:
    connection = sqlite3.connect(paths["db_path"])
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at, finished_at) "
        "VALUES (?, 'premarket', ?, 'passed', ?, ?)",
        (database_run_id, AS_OF.isoformat(), AS_OF.isoformat(), AS_OF.isoformat()),
    )
    for item in advice:
        connection.execute(
            "INSERT OR IGNORE INTO securities (code, name, exchange, created_at, updated_at) "
            "VALUES (?, ?, 'SSE', ?, ?)",
            (item.code, item.code, AS_OF.isoformat(), AS_OF.isoformat()),
        )
        connection.execute(
            "INSERT INTO advice (advice_id, run_id, code, action, confidence, rationale, "
            "evidence_ids_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.advice_id, database_run_id, item.code, item.action, item.confidence,
                item.rationale, json.dumps(item.evidence_ids), AS_OF.isoformat(),
            ),
        )
    connection.commit()
    connection.close()
    write_premarket_report(
        "2026-07-12",
        advice,
        paths["output_dir"],
        quality_results=(QualityResult("fixture", "blocking", True, "ready"),),
        run_id=None if report_run_id == "initial" else report_run_id,
        rerun_reason=None if report_run_id == "initial" else "updated candidates",
        supersedes=supersedes,
    )


def test_premarket_happy_path_persists_complete_projection(tmp_path: Path):
    paths = coordinator_paths(tmp_path)
    from advisor.db.migrate import migrate_database
    migrate_database(paths["db_path"])
    seed_market(paths["db_path"])

    result = run_premarket(
        collector_snapshot=collector(), analyst_runner=PassingRunner(), as_of=AS_OF,
        report_date="2026-07-12", candidate_codes=(CODE,),
        quality_evaluator=passed_quality, evidence_persister=persist_fixture_evidence,
        **paths,
    )

    assert result.status == "passed"
    assert query_all(paths["db_path"], "SELECT run_type, status FROM advisor_runs") == [("premarket", "passed")]
    assert query_all(paths["db_path"], "SELECT status FROM data_quality_checks") == [("passed",)]
    assert query_all(paths["db_path"], "SELECT evidence_id FROM evidence") == [(EVIDENCE_ID,)]
    outputs = query_all(paths["db_path"], "SELECT role, payload_json FROM analyst_outputs ORDER BY role")
    assert {row[0] for row in outputs} == set(ANALYST_ROLES)
    assert all(isinstance(json.loads(row[1]), dict) for row in outputs)
    advice = query_all(paths["db_path"], "SELECT action, evidence_ids_json FROM advice")
    assert advice == [("watch", json.dumps([EVIDENCE_ID], separators=(",", ":")))]
    assert query_all(paths["db_path"], "SELECT report_type FROM report_archive") == [("premarket",)]
    assert len(query_all(paths["db_path"], "SELECT path FROM chart_assets")) == 1
    assert query_all(paths["db_path"], "SELECT code FROM stock_profiles") == [(CODE,)]
    assert query_all(paths["db_path"], "SELECT run_id FROM stock_profile_history") == [(result.run_id,)]
    assert (paths["profile_dir"] / f"{CODE}.md").exists()
    assert next(paths["chart_dir"].rglob("*.png")).stat().st_size > 1000


def test_quality_blocked_premarket_publishes_only_sanitized_failure(tmp_path: Path):
    paths = coordinator_paths(tmp_path)
    result = run_premarket(
        collector_snapshot=collector(passed=False), analyst_runner=PassingRunner(), as_of=AS_OF,
        report_date="2026-07-12", candidate_codes=(CODE,),
        quality_evaluator=blocked_quality, evidence_persister=persist_fixture_evidence,
        **paths,
    )

    assert result.status == "blocked"
    assert query_all(paths["db_path"], "SELECT COUNT(*) FROM advice")[0][0] == 0
    assert query_all(paths["db_path"], "SELECT COUNT(*) FROM analyst_outputs")[0][0] == 0
    archive = json.loads(result.report_paths.json_path.read_text(encoding="utf-8"))
    assert archive["report_type"] == "failure"
    assert "hidden" not in json.dumps(archive)


def test_analyst_quality_error_publishes_no_partial_advice(tmp_path: Path):
    paths = coordinator_paths(tmp_path)
    result = run_premarket(
        collector_snapshot=collector(), analyst_runner=BlockingRunner(), as_of=AS_OF,
        report_date="2026-07-12", candidate_codes=(CODE,),
        quality_evaluator=passed_quality, evidence_persister=persist_fixture_evidence,
        **paths,
    )

    assert result.status == "blocked"
    assert query_all(paths["db_path"], "SELECT COUNT(*) FROM advice")[0][0] == 0
    assert query_all(paths["db_path"], "SELECT COUNT(*) FROM analyst_outputs")[0][0] == 0
    assert "must-not-archive" not in result.report_paths.json_path.read_text(encoding="utf-8")


def test_review_links_morning_advice_and_persists_review(tmp_path: Path):
    paths = coordinator_paths(tmp_path)
    from advisor.db.migrate import migrate_database
    migrate_database(paths["db_path"])
    seed_market(paths["db_path"])
    morning = run_premarket(
        collector_snapshot=collector(), analyst_runner=PassingRunner(), as_of=AS_OF,
        report_date="2026-07-12", candidate_codes=(CODE,),
        quality_evaluator=passed_quality, evidence_persister=persist_fixture_evidence,
        **paths,
    )

    result = run_review(
        collector_snapshot=collector(), as_of=AS_OF.replace(hour=22, minute=30),
        report_date="2026-07-12", candidate_codes=(CODE,),
        quality_evaluator=passed_quality, **paths,
    )

    assert morning.status == result.status == "passed"
    advice_id = query_all(paths["db_path"], "SELECT advice_id FROM advice")[0][0]
    assert query_all(paths["db_path"], "SELECT advice_id, outcome FROM reviews") == [(advice_id, "followed_strength")]
    payload = json.loads(result.report_paths.json_path.read_text(encoding="utf-8"))
    assert payload["linked_premarket"]["run_id"] == "initial"
    assert payload["reviews"][0]["advice_id"] == advice_id
    assert "11.5000" in payload["reviews"][0]["review_text"]
    assert "10.5000" in payload["reviews"][0]["review_text"]
    assert "no ledger transactions" in payload["reviews"][0]["review_text"]
    assert query_all(paths["db_path"], "SELECT report_type FROM report_archive ORDER BY created_at") == [("premarket",), ("review",)]
    assert query_all(paths["db_path"], "SELECT COUNT(*) FROM stock_profile_history")[0][0] >= 2


@pytest.mark.parametrize("module", [premarket_reporting, review_reporting])
def test_declared_report_cli_entrypoint_has_minimal_argparse_path(module):
    assert hasattr(module, "main")
    with pytest.raises(SystemExit) as exit_info:
        module.main(["--help"])
    assert exit_info.value.code == 0


def test_premarket_cli_invokes_injected_coordinator(tmp_path: Path):
    calls = {}
    snapshot = collector()

    def snapshot_reader(events_db, allowed_rids_path, *, as_of):
        calls["snapshot"] = (events_db, allowed_rids_path, as_of)
        return snapshot

    def coordinator(**kwargs):
        calls["coordinator"] = kwargs
        return SimpleNamespace(
            run_id="cli-run", status="passed", warnings=(),
            report_paths=SimpleNamespace(
                markdown_path=tmp_path / "premarket.md",
                json_path=tmp_path / "premarket.json",
            ),
        )

    premarket_reporting.main(
        [
            "--as-of", AS_OF.isoformat(), "--report-date", "2026-07-12",
            "--codes", CODE, "--events-db", str(tmp_path / "events.sqlite"),
            "--allowed-rids", str(tmp_path / "allowed.yaml"),
            "--output-dir", str(tmp_path / "reports"),
        ],
        coordinator=coordinator,
        snapshot_reader=snapshot_reader,
    )

    assert calls["snapshot"][2] == AS_OF
    assert calls["coordinator"]["collector_snapshot"] is snapshot
    assert calls["coordinator"]["candidate_codes"] == (CODE,)


def test_review_uses_exact_selected_premarket_archive(tmp_path: Path):
    paths = coordinator_paths(tmp_path)
    from advisor.db.migrate import migrate_database
    migrate_database(paths["db_path"])
    seed_market(paths["db_path"])
    initial = AdviceItem("initial-advice", CODE, "watch", 0.5, "initial", [])
    rerun = AdviceItem("rerun-advice", "000001", "watch", 0.5, "rerun", [])
    seed_premarket_report(
        paths, database_run_id="morning-initial", report_run_id="initial", advice=[initial]
    )
    seed_premarket_report(
        paths, database_run_id="morning-rerun", report_run_id="rerun1", advice=[rerun],
        supersedes="initial",
    )

    result = run_review(
        collector_snapshot=collector(), as_of=AS_OF.replace(hour=22, minute=30),
        report_date="2026-07-12", candidate_codes=(CODE,), premarket_run_id="initial",
        quality_evaluator=passed_quality, **paths,
    )

    assert result.status == "passed"
    assert query_all(paths["db_path"], "SELECT advice_id FROM reviews") == [("initial-advice",)]


def test_review_rolls_back_rows_when_selected_archive_linkage_fails(tmp_path: Path):
    paths = coordinator_paths(tmp_path)
    from advisor.db.migrate import migrate_database
    migrate_database(paths["db_path"])
    seed_market(paths["db_path"])
    advice = AdviceItem("initial-advice", CODE, "watch", 0.5, "initial", [])
    seed_premarket_report(
        paths, database_run_id="morning-initial", report_run_id="initial", advice=[advice]
    )
    history_before = query_all(paths["db_path"], "SELECT COUNT(*) FROM stock_profile_history")
    profile_before = query_all(paths["db_path"], "SELECT * FROM stock_profiles")

    with pytest.raises(ValueError, match="premarket archive not found"):
        run_review(
            collector_snapshot=collector(), as_of=AS_OF.replace(hour=22, minute=30),
            report_date="2026-07-12", candidate_codes=(CODE,),
            premarket_run_id="missing", quality_evaluator=passed_quality, **paths,
        )

    assert query_all(paths["db_path"], "SELECT COUNT(*) FROM reviews") == [(0,)]
    assert query_all(paths["db_path"], "SELECT COUNT(*) FROM stock_profile_history") == history_before
    assert query_all(paths["db_path"], "SELECT * FROM stock_profiles") == profile_before


def test_review_rolls_back_all_projection_rows_when_report_write_fails(
    tmp_path: Path, monkeypatch
):
    paths = coordinator_paths(tmp_path)
    from advisor.db.migrate import migrate_database
    migrate_database(paths["db_path"])
    seed_market(paths["db_path"])
    advice = AdviceItem("initial-advice", CODE, "watch", 0.5, "initial", [])
    seed_premarket_report(
        paths, database_run_id="morning-initial", report_run_id="initial", advice=[advice]
    )

    def fail_report_write(*_args, **_kwargs):
        raise OSError("fixture report write failure")

    monkeypatch.setattr(coordinator_module, "write_review_report", fail_report_write)
    with pytest.raises(OSError, match="fixture report write failure"):
        run_review(
            collector_snapshot=collector(), as_of=AS_OF.replace(hour=22, minute=30),
            report_date="2026-07-12", candidate_codes=(CODE,),
            quality_evaluator=passed_quality, **paths,
        )

    assert query_all(paths["db_path"], "SELECT COUNT(*) FROM reviews") == [(0,)]
    assert query_all(paths["db_path"], "SELECT COUNT(*) FROM stock_profiles") == [(0,)]
    assert query_all(paths["db_path"], "SELECT COUNT(*) FROM stock_profile_history") == [(0,)]
    assert query_all(paths["db_path"], "SELECT COUNT(*) FROM chart_assets") == [(0,)]
    assert query_all(
        paths["db_path"],
        "SELECT status FROM advisor_runs WHERE run_type = 'review'",
    ) == [("failed",)]


def test_review_archive_insert_failure_rolls_back_and_hides_orphan_archive(
    tmp_path: Path, monkeypatch
):
    paths = coordinator_paths(tmp_path)
    from advisor.db.migrate import migrate_database
    migrate_database(paths["db_path"])
    seed_market(paths["db_path"])
    advice = AdviceItem("initial-advice", CODE, "watch", 0.5, "initial", [])
    seed_premarket_report(
        paths, database_run_id="morning-initial", report_run_id="initial", advice=[advice]
    )
    real_archive_report = coordinator_module._archive_report

    def fail_review_archive(connection, run_id, report_type, report_date, report_paths, as_of):
        if report_type == "review":
            raise sqlite3.IntegrityError("fixture archive insertion failure")
        return real_archive_report(
            connection, run_id, report_type, report_date, report_paths, as_of
        )

    monkeypatch.setattr(coordinator_module, "_archive_report", fail_review_archive)
    with pytest.raises(sqlite3.IntegrityError, match="fixture archive insertion failure"):
        run_review(
            collector_snapshot=collector(), as_of=AS_OF.replace(hour=22, minute=30),
            report_date="2026-07-12", candidate_codes=(CODE,), run_id="review-orphan",
            quality_evaluator=passed_quality, **paths,
        )

    assert (paths["output_dir"] / "2026-07-12" / "review.complete.json").is_file()
    assert query_all(paths["db_path"], "SELECT COUNT(*) FROM reviews") == [(0,)]
    assert query_all(
        paths["db_path"], "SELECT COUNT(*) FROM report_archive WHERE report_type = 'review'"
    ) == [(0,)]
    assert query_all(
        paths["db_path"],
        "SELECT status FROM advisor_runs WHERE run_id = 'review-orphan'",
    ) == [("failed",)]

    monkeypatch.setattr(coordinator_module, "_archive_report", real_archive_report)
    with pytest.raises(FileExistsError, match="report archive already exists"):
        run_review(
            collector_snapshot=collector(), as_of=AS_OF.replace(hour=22, minute=31),
            report_date="2026-07-12", candidate_codes=(CODE,), run_id="review-retry",
            quality_evaluator=passed_quality, **paths,
        )
    assert query_all(
        paths["db_path"],
        "SELECT status FROM advisor_runs WHERE run_id = 'review-retry'",
    ) == [("failed",)]
    assert query_all(
        paths["db_path"], "SELECT COUNT(*) FROM report_archive WHERE report_type = 'review'"
    ) == [(0,)]

    client = TestClient(create_app(paths["db_path"].parent, db_path=paths["db_path"]))
    listing = client.get(
        "/api/reports?start_date=2026-07-12&end_date=2026-07-12"
    )

    assert listing.status_code == 200
    assert not any(item["report_type"] == "review" for item in listing.json()["reports"])
    assert client.get(
        "/api/reports/2026-07-12/review?run_id=initial"
    ).status_code == 503


def test_review_blocks_when_archive_advice_scope_differs_from_quality_scope(tmp_path: Path):
    paths = coordinator_paths(tmp_path)
    from advisor.db.migrate import migrate_database
    migrate_database(paths["db_path"])
    advice = [
        AdviceItem("advice-1", CODE, "watch", 0.5, "first", []),
        AdviceItem("advice-2", "000001", "watch", 0.5, "second", []),
    ]
    seed_premarket_report(
        paths, database_run_id="morning-initial", report_run_id="initial", advice=advice
    )

    result = run_review(
        collector_snapshot=collector(), as_of=AS_OF.replace(hour=22, minute=30),
        report_date="2026-07-12", candidate_codes=(CODE,),
        quality_evaluator=passed_quality, **paths,
    )

    assert result.status == "blocked"
    assert query_all(paths["db_path"], "SELECT COUNT(*) FROM reviews") == [(0,)]
    assert query_all(paths["db_path"], "SELECT COUNT(*) FROM stock_profile_history") == [(0,)]


def test_review_outcome_uses_decline_and_same_day_ledger_activity(tmp_path: Path):
    paths = coordinator_paths(tmp_path)
    from advisor.db.migrate import migrate_database
    migrate_database(paths["db_path"])
    advice = AdviceItem("initial-advice", CODE, "watch", 0.5, "initial", [])
    seed_premarket_report(
        paths, database_run_id="morning-initial", report_run_id="initial", advice=[advice]
    )
    connection = sqlite3.connect(paths["db_path"])
    connection.executemany(
        "INSERT INTO market_daily (code, trade_date, open, high, low, close, volume, amount, "
        "source, fetched_at, as_of_date, content_hash, quality_status) "
        "VALUES (?, ?, 10, 10, 9, ?, 100, 1000, 'fixture', ?, ?, ?, 'passed')",
        [
            (CODE, "2026-07-11", 10.0, AS_OF.isoformat(), "2026-07-11", "prior"),
            (CODE, "2026-07-12", 9.0, AS_OF.isoformat(), "2026-07-12", "latest"),
        ],
    )
    connection.execute(
        "INSERT INTO ledger_accounts (account_id, name, created_at) VALUES ('a1', 'fixture', ?)",
        (AS_OF.isoformat(),),
    )
    connection.execute(
        "INSERT INTO ledger_transactions (transaction_id, account_id, trade_date, "
        "transaction_type, code, quantity, price, amount, fees, source, created_at) "
        "VALUES ('t1', 'a1', '2026-07-12', 'buy', ?, 1, 9, 9, 0, 'fixture', ?)",
        (CODE, AS_OF.isoformat()),
    )
    connection.commit()
    connection.close()

    result = run_review(
        collector_snapshot=collector(), as_of=AS_OF.replace(hour=22, minute=30),
        report_date="2026-07-12", candidate_codes=(CODE,),
        quality_evaluator=passed_quality, **paths,
    )

    payload = json.loads(result.report_paths.json_path.read_text(encoding="utf-8"))
    assert payload["reviews"][0]["outcome"] == "risk_review"
    assert "9.0000" in payload["reviews"][0]["review_text"]
    assert "10.0000" in payload["reviews"][0]["review_text"]
    assert "1 ledger transaction" in payload["reviews"][0]["review_text"]


def test_coordinator_resolves_configured_storage_without_tushare(tmp_path: Path):
    config_path = tmp_path / "config" / "advisor.yaml"
    config_path.parent.mkdir()
    config_path.write_text(yaml.safe_dump({
        "market": {"primary": "A股"},
        "schedule": {"premarket_time": "08:30", "review_time": "22:30"},
        "storage": {
            "database": "custom/state.sqlite",
            "chart_dir": "custom/charts",
            "profile_dir": "custom/profiles",
        },
        "data_sources": {"allow_tushare": False, "free_sources": ["sina"]},
    }), encoding="utf-8")
    report_dir = tmp_path / "reports"

    result = run_premarket(
        collector_snapshot=collector(), analyst_runner=PassingRunner(), as_of=AS_OF,
        report_date="2026-07-12", candidate_codes=(CODE,), output_dir=report_dir,
        config_path=config_path, root=tmp_path, quality_evaluator=passed_quality,
        evidence_persister=persist_fixture_evidence,
    )

    assert result.status == "passed"
    assert (tmp_path / "custom" / "state.sqlite").exists()
    assert (tmp_path / "custom" / "profiles" / f"{CODE}.md").exists()
    assert not list(tmp_path.rglob("*tushare*"))
