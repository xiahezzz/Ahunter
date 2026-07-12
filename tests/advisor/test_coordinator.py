import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
import pytest

from advisor import paths as advisor_paths
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
    assert query_all(paths["db_path"], "SELECT advice_id, outcome FROM reviews") == [(advice_id, "reviewed")]
    payload = json.loads(result.report_paths.json_path.read_text(encoding="utf-8"))
    assert payload["linked_premarket"]["run_id"] == "initial"
    assert payload["reviews"][0]["advice_id"] == advice_id
    assert query_all(paths["db_path"], "SELECT report_type FROM report_archive ORDER BY created_at") == [("premarket",), ("review",)]
    assert query_all(paths["db_path"], "SELECT COUNT(*) FROM stock_profile_history")[0][0] >= 2


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
