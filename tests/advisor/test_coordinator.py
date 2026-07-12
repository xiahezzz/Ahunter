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
                } if role == "quality_gate"
                else {"decision": {"action": "hold", "confidence": 0.5, "confidence_basis": "rating_strength"}}
                if role == "portfolio_manager"
                else {"signal": role},
            )
            for role in ANALYST_ROLES
        ]


class BlockingRunner:
    def run(self, code: str, trade_date: str, evidence: list[dict]) -> list[AnalystOutput]:
        raise DataQualityBlockedError("provider token=must-not-archive")


class DecisionRunner(PassingRunner):
    def run(self, code: str, trade_date: str, evidence: list[dict]) -> list[AnalystOutput]:
        outputs = super().run(code, trade_date, evidence)
        summaries = {
            "portfolio_manager": "Portfolio favors staged research exposure.",
            "trader": "Trader confirms liquidity is adequate.",
            "research_manager": "Research case is evidence-backed.",
        }
        return [
            AnalystOutput(
                item.role,
                item.code,
                summaries.get(item.role, item.summary),
                {"decision": {"action": "watch_buy", "confidence": 0.8, "confidence_basis": "rating_strength"}}
                if item.role == "portfolio_manager"
                else item.payload,
            )
            for item in outputs
        ]


class EmptyPortfolioRunner(PassingRunner):
    def run(self, code: str, trade_date: str, evidence: list[dict]) -> list[AnalystOutput]:
        return [
            AnalystOutput(item.role, item.code, "" if item.role == "portfolio_manager" else item.summary, item.payload)
            for item in super().run(code, trade_date, evidence)
        ]


class CandidateRunner:
    def run(self, code: str, trade_date: str, evidence: list[dict]) -> list[AnalystOutput]:
        return [
            AnalystOutput(
                role,
                code,
                f"{role} summary",
                {"quality_outcome": QualityOutcome(True, "hard checks passed")}
                if role == "quality_gate"
                else {"decision": {"action": "hold", "confidence": 0.5, "confidence_basis": "rating_strength"}}
                if role == "portfolio_manager"
                else {"signal": role},
            )
            for role in ANALYST_ROLES
        ]


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
    report_paths = write_premarket_report(
        "2026-07-12",
        advice,
        paths["output_dir"],
        quality_results=(QualityResult("fixture", "blocking", True, "ready"),),
        run_id=None if report_run_id == "initial" else report_run_id,
        rerun_reason=None if report_run_id == "initial" else "updated candidates",
        supersedes=supersedes,
    )
    connection.execute(
        "INSERT INTO report_archive (report_id, run_id, report_type, report_date, "
        "markdown_path, json_path, created_at) VALUES (?, ?, 'premarket', ?, ?, ?, ?)",
        (
            f"report-{database_run_id}", database_run_id, "2026-07-12",
            str(report_paths.markdown_path), str(report_paths.json_path), AS_OF.isoformat(),
        ),
    )
    connection.commit()
    connection.close()


def test_premarket_happy_path_persists_complete_projection(tmp_path: Path):
    paths = coordinator_paths(tmp_path)
    from advisor.db.migrate import migrate_database
    migrate_database(paths["db_path"])
    seed_market(paths["db_path"])
    connection = sqlite3.connect(paths["db_path"])
    connection.execute(
        "INSERT INTO ledger_accounts (account_id, name, created_at) VALUES ('a1', 'fixture', ?)",
        (AS_OF.isoformat(),),
    )
    connection.execute(
        "INSERT INTO positions (account_id, code, quantity, cost_basis, updated_at) "
        "VALUES ('a1', ?, 100, 1000, ?)",
        (CODE, AS_OF.isoformat()),
    )
    connection.commit()
    connection.close()

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
    assert advice == [("hold", json.dumps([EVIDENCE_ID], separators=(",", ":")))]
    assert query_all(paths["db_path"], "SELECT report_type FROM report_archive") == [("premarket",)]
    assert len(query_all(paths["db_path"], "SELECT path FROM chart_assets")) == 1
    assert query_all(paths["db_path"], "SELECT code FROM stock_profiles") == [(CODE,)]
    ledger_exposure = json.loads(
        query_all(paths["db_path"], "SELECT ledger_exposure_json FROM stock_profiles")[0][0]
    )
    assert ledger_exposure == {
        "cost_basis": 1000.0,
        "market_price": 11.5,
        "market_value": 1150.0,
        "pricing_status": "passed",
        "quantity": 100,
        "unrealized_pnl": 150.0,
    }
    assert query_all(paths["db_path"], "SELECT run_id FROM stock_profile_history") == [(result.run_id,)]
    assert (paths["profile_dir"] / f"{CODE}.md").exists()
    assert next(paths["chart_dir"].rglob("*.png")).stat().st_size > 1000


def test_premarket_uses_bounded_analyst_decision_and_role_rationale(tmp_path: Path):
    paths = coordinator_paths(tmp_path)
    from advisor.db.migrate import migrate_database
    migrate_database(paths["db_path"])
    seed_market(paths["db_path"])

    result = run_premarket(
        collector_snapshot=collector(), analyst_runner=DecisionRunner(), as_of=AS_OF,
        report_date="2026-07-12", candidate_codes=(CODE,),
        quality_evaluator=passed_quality, evidence_persister=persist_fixture_evidence,
        **paths,
    )

    payload = json.loads(result.report_paths.json_path.read_text(encoding="utf-8"))
    item = payload["advice"][0]
    assert item["action"] == "watch_buy"
    assert item["confidence"] == pytest.approx(0.8)
    assert "Portfolio favors" in item["rationale"]
    assert "Trader confirms" in item["rationale"]
    assert "Research case" in item["rationale"]
    assert payload["context"]["chart_markers"] == [f"{CODE}: advice=watch_buy at 2026-07-12"]


def test_premarket_expands_candidates_from_mx_summary_and_positions(tmp_path: Path):
    paths = coordinator_paths(tmp_path)
    from advisor.db.migrate import migrate_database
    migrate_database(paths["db_path"])
    connection = sqlite3.connect(paths["db_path"])
    connection.execute(
        "INSERT INTO ledger_accounts (account_id, name, created_at) VALUES ('a1', 'fixture', ?)",
        (AS_OF.isoformat(),),
    )
    connection.execute(
        "INSERT INTO positions (account_id, code, quantity, cost_basis, updated_at) "
        "VALUES ('a1', '000001', 10, 10, ?)",
        (AS_OF.isoformat(),),
    )
    connection.commit()
    connection.close()
    event = SimpleNamespace(summary="MX accepted discussion of 600519")
    snapshot = CollectorSnapshot((event,), collector().quality, AS_OF, ())
    checked = []

    def capture_quality(_connection, request):
        checked.append(request.candidate_codes)
        return passed_quality()

    result = run_premarket(
        collector_snapshot=snapshot, analyst_runner=CandidateRunner(), as_of=AS_OF,
        report_date="2026-07-12", candidate_codes=(),
        quality_evaluator=capture_quality, evidence_persister=persist_fixture_evidence,
        **paths,
    )

    assert result.status == "passed"
    assert checked == [(CODE, "000001"), (CODE, "000001")]
    assert {row[0] for row in query_all(paths["db_path"], "SELECT code FROM advice")} == {CODE, "000001"}


def test_premarket_projection_preserves_non_empty_profile_history(tmp_path: Path):
    paths = coordinator_paths(tmp_path)
    from advisor.db.migrate import migrate_database
    migrate_database(paths["db_path"])
    seed_market(paths["db_path"])
    connection = sqlite3.connect(paths["db_path"])
    connection.execute(
        "INSERT INTO securities (code, name, exchange, created_at, updated_at) VALUES (?, ?, 'SSE', ?, ?)",
        (CODE, CODE, AS_OF.isoformat(), AS_OF.isoformat()),
    )
    connection.execute(
        "INSERT INTO stock_profiles (code, thesis_json, information_flow_json, capital_flow_json, "
        "fundamentals_json, analyst_flow_json, ledger_exposure_json, assets_json, updated_at) "
        "VALUES (?, '{\"name\":\"Prior Name\",\"industry\":\"Prior Industry\","
        "\"thesis\":\"Prior thesis\"}', '[\"prior information\"]', "
        "'[\"prior capital\"]', '{}', '[\"prior analyst\"]', '{}', "
        "'[\"prior-chart.png\"]', ?)",
        (CODE, AS_OF.isoformat()),
    )
    connection.commit()
    connection.close()

    run_premarket(
        collector_snapshot=collector(), analyst_runner=EmptyPortfolioRunner(), as_of=AS_OF,
        report_date="2026-07-12", candidate_codes=(CODE,),
        quality_evaluator=passed_quality, evidence_persister=persist_fixture_evidence,
        **paths,
    )

    row = query_all(
        paths["db_path"],
        f"SELECT thesis_json, information_flow_json, capital_flow_json, analyst_flow_json, "
        f"assets_json FROM stock_profiles WHERE code = '{CODE}'",
    )[0]
    assert json.loads(row[0]) == {
        "name": "Prior Name",
        "industry": "Prior Industry",
        "thesis": "Prior thesis",
    }
    assert "prior information" in json.loads(row[1])
    assert "prior capital" in json.loads(row[2])
    assert "prior analyst" in json.loads(row[3])
    assert "prior-chart.png" in json.loads(row[4])


def test_final_premarket_quality_failure_survives_projection_rollback(tmp_path: Path):
    paths = coordinator_paths(tmp_path)
    evaluations = iter(
        (
            QualityGateResult(
                "passed",
                (QualityResult("preflight_fixture", "blocking", True, "ready"),),
            ),
            QualityGateResult(
                "blocked",
                (QualityResult("final_fixture", "blocking", False, "stale"),),
            ),
        )
    )

    result = run_premarket(
        collector_snapshot=collector(), analyst_runner=PassingRunner(), as_of=AS_OF,
        report_date="2026-07-12", candidate_codes=(CODE,),
        quality_evaluator=lambda *_args: next(evaluations),
        evidence_persister=persist_fixture_evidence,
        **paths,
    )

    assert result.status == "blocked"
    assert query_all(
        paths["db_path"],
        "SELECT check_name, status FROM data_quality_checks ORDER BY check_name",
    ) == [("final_fixture", "failed")]


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


@pytest.mark.parametrize(
    "portfolio_payload",
    [
        {},
        {"decision": {"action": "watch"}},
        {"decision": {"action": "hold", "confidence": 1.5, "confidence_basis": "rating_strength"}},
        {"decision": {"action": "watch_buy", "rating": "hold", "confidence": 0.8, "confidence_basis": "rating_strength"}},
    ],
)
def test_missing_malformed_or_ambiguous_portfolio_decision_blocks_publication(
    tmp_path: Path,
    portfolio_payload: dict,
):
    class BadDecisionRunner(PassingRunner):
        def run(self, code: str, trade_date: str, evidence: list[dict]) -> list[AnalystOutput]:
            return [
                AnalystOutput(item.role, item.code, item.summary, portfolio_payload)
                if item.role == "portfolio_manager" else item
                for item in super().run(code, trade_date, evidence)
            ]

    paths = coordinator_paths(tmp_path)

    result = run_premarket(
        collector_snapshot=collector(), analyst_runner=BadDecisionRunner(), as_of=AS_OF,
        report_date="2026-07-12", candidate_codes=(CODE,),
        quality_evaluator=passed_quality, evidence_persister=persist_fixture_evidence,
        **paths,
    )

    assert result.status == "blocked"
    assert query_all(paths["db_path"], "SELECT COUNT(*) FROM advice") == [(0,)]
    payload = json.loads(result.report_paths.json_path.read_text(encoding="utf-8"))
    assert payload["report_type"] == "failure"
    assert payload["quality_status"] == "blocked"


def test_ambiguous_duplicate_portfolio_decisions_block_publication(tmp_path: Path):
    class DuplicateDecisionRunner(PassingRunner):
        def run(self, code: str, trade_date: str, evidence: list[dict]) -> list[AnalystOutput]:
            outputs = super().run(code, trade_date, evidence)
            outputs.append(
                AnalystOutput(
                    "portfolio_manager",
                    code,
                    "duplicate",
                    {"decision": {"action": "watch_buy", "confidence": 0.8, "confidence_basis": "rating_strength"}},
                )
            )
            return outputs

    paths = coordinator_paths(tmp_path)

    result = run_premarket(
        collector_snapshot=collector(), analyst_runner=DuplicateDecisionRunner(), as_of=AS_OF,
        report_date="2026-07-12", candidate_codes=(CODE,),
        quality_evaluator=passed_quality, evidence_persister=persist_fixture_evidence,
        **paths,
    )

    assert result.status == "blocked"
    assert query_all(paths["db_path"], "SELECT COUNT(*) FROM advice") == [(0,)]


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


def test_review_versions_snapshots_for_every_active_ledger_account(tmp_path: Path):
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
    advice_id = query_all(paths["db_path"], "SELECT advice_id FROM advice")[0][0]
    connection = sqlite3.connect(paths["db_path"])
    connection.executemany(
        "INSERT INTO ledger_accounts (account_id, name, created_at) VALUES (?, ?, ?)",
        [
            ("review-account", "Review", AS_OF.isoformat()),
            ("cash-only", "Cash", AS_OF.isoformat()),
            ("unrelated", "Unrelated", AS_OF.isoformat()),
        ],
    )
    connection.executemany(
        "INSERT INTO ledger_transactions (transaction_id, account_id, trade_date, transaction_type, code, quantity, price, amount, fees, source, created_at) "
        "VALUES (?, 'review-account', ?, ?, ?, ?, ?, ?, 0, 'fixture', ?)",
        [
            ("review-deposit", "2026-07-11", "cash_deposit", None, 0, 0, 20000, AS_OF.isoformat()),
            ("review-buy", "2026-07-12", "buy", CODE, 100, 11.5, -1150, AS_OF.isoformat()),
        ],
    )
    connection.executemany(
        "INSERT INTO ledger_transactions (transaction_id, account_id, trade_date, transaction_type, code, quantity, price, amount, fees, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'fixture', ?)",
        [
            ("cash-deposit", "cash-only", "2026-07-11", "cash_deposit", None, 0, 0, 5000, AS_OF.isoformat()),
            ("other-deposit", "unrelated", "2026-07-11", "cash_deposit", None, 0, 0, 5000, AS_OF.isoformat()),
            ("other-fee", "unrelated", "2026-07-12", "fee", None, 0, 0, -10, AS_OF.isoformat()),
        ],
    )
    connection.commit()
    connection.close()

    result = run_review(
        collector_snapshot=collector(), as_of=AS_OF.replace(hour=22, minute=30),
        report_date="2026-07-12", candidate_codes=(CODE,),
        run_id="review-first", quality_evaluator=passed_quality, **paths,
    )

    assert morning.status == result.status == "passed"
    snapshots = query_all(
        paths["db_path"],
        "SELECT snapshot_id, account_id, cash, market_value, realized_pnl, unrealized_pnl, exposure_json FROM portfolio_snapshots",
    )
    assert len(snapshots) == 3
    review_snapshot = next(row for row in snapshots if row[1] == "review-account")
    assert review_snapshot[1:6] == ("review-account", 18850.0, 1150.0, 0.0, 0.0)
    context = json.loads(result.report_paths.json_path.read_text(encoding="utf-8"))["context"]
    impacts = [json.loads(item) for item in context["ledger_impact"]]
    assert {item["account_id"] for item in impacts} == {
        "cash-only", "review-account", "unrelated"
    }
    assert all("pricing_status" in item and "quality_flags" in item for item in impacts)
    impact = next(item for item in impacts if item["account_id"] == "review-account")
    assert impact["advice_id"] == advice_id
    assert impact["snapshot_id"] == review_snapshot[0]
    assert impact["transactions"] == [
        {"transaction_id": "review-buy", "transaction_type": "buy"}
    ]
    assert impact["exposure"][CODE]["quantity"] == 100
    assert query_all(
        paths["db_path"],
        "SELECT run_id, advice_id, transaction_id, account_id, code, trade_date "
        "FROM advice_trade_matches",
    ) == [("review-first", advice_id, "review-buy", "review-account", CODE, "2026-07-12")]

    connection = sqlite3.connect(paths["db_path"])
    connection.row_factory = sqlite3.Row
    coordinator_module.materialize_ledger_snapshots(
        connection,
        ("cash-only", "review-account", "unrelated"),
        as_of=AS_OF.replace(hour=22, minute=30),
        snapshot_source="review-second",
    )
    connection.commit()
    connection.close()
    versioned = query_all(
        paths["db_path"],
        "SELECT account_id, COUNT(DISTINCT snapshot_id) FROM portfolio_snapshots "
        "GROUP BY account_id ORDER BY account_id",
    )
    assert versioned == [("cash-only", 2), ("review-account", 2), ("unrelated", 2)]


def test_review_rejects_excessive_ledger_accounts_before_rendering_context(
    tmp_path: Path,
    monkeypatch,
):
    from advisor.db.migrate import migrate_database
    from advisor.ledger import importer as ledger_importer

    monkeypatch.setattr(ledger_importer, "MAX_LEDGER_SNAPSHOT_ACCOUNTS", 2, raising=False)
    paths = coordinator_paths(tmp_path)
    migrate_database(paths["db_path"])
    seed_market(paths["db_path"])
    seed_premarket_report(
        paths,
        database_run_id="initial",
        report_run_id="initial",
        advice=[
            AdviceItem(
                "advice-1",
                CODE,
                "watch",
                0.7,
                "fixture rationale",
                (),
            )
        ],
    )
    connection = sqlite3.connect(paths["db_path"])
    connection.executemany(
        "INSERT INTO ledger_accounts (account_id, name, created_at) VALUES (?, ?, ?)",
        [
            ("account-1", "Account 1", AS_OF.isoformat()),
            ("account-2", "Account 2", AS_OF.isoformat()),
            ("account-3", "Account 3", AS_OF.isoformat()),
        ],
    )
    connection.executemany(
        "INSERT INTO ledger_transactions (transaction_id, account_id, trade_date, "
        "transaction_type, code, quantity, price, amount, fees, source, created_at) "
        "VALUES (?, ?, '2026-07-11', 'cash_deposit', NULL, 0, 0, 1000, 0, 'fixture', ?)",
        [
            ("deposit-1", "account-1", AS_OF.isoformat()),
            ("deposit-2", "account-2", AS_OF.isoformat()),
            ("deposit-3", "account-3", AS_OF.isoformat()),
        ],
    )
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="ledger snapshot account limit"):
        run_review(
            collector_snapshot=collector(), as_of=AS_OF.replace(hour=22, minute=30),
            report_date="2026-07-12", candidate_codes=(CODE,),
            run_id="review-too-many-accounts", quality_evaluator=passed_quality, **paths,
        )

    assert not (paths["output_dir"] / "2026-07-12" / "review.json").exists()
    assert query_all(paths["db_path"], "SELECT COUNT(*) FROM portfolio_snapshots") == [(0,)]


def test_review_bounds_ledger_account_discovery_before_snapshot_materialization(
    tmp_path: Path,
    monkeypatch,
):
    from advisor.db.migrate import migrate_database
    from advisor.ledger import importer as ledger_importer

    monkeypatch.setattr(ledger_importer, "MAX_LEDGER_SNAPSHOT_ACCOUNTS", 2, raising=False)
    paths = coordinator_paths(tmp_path)
    migrate_database(paths["db_path"])
    seed_premarket_report(
        paths,
        database_run_id="initial",
        report_run_id="initial",
        advice=[AdviceItem("advice-1", CODE, "watch", 0.7, "fixture rationale", ())],
    )
    connection = sqlite3.connect(paths["db_path"])
    account_rows = [
        (f"account-{index}", f"Account {index}", AS_OF.isoformat())
        for index in range(1, 6)
    ]
    connection.executemany(
        "INSERT INTO ledger_accounts (account_id, name, created_at) VALUES (?, ?, ?)",
        account_rows,
    )
    connection.executemany(
        "INSERT INTO ledger_transactions (transaction_id, account_id, trade_date, "
        "transaction_type, code, quantity, price, amount, fees, source, created_at) "
        "VALUES (?, ?, '2026-07-11', 'cash_deposit', NULL, 0, 0, 1000, 0, 'fixture', ?)",
        [
            (f"deposit-{index}", f"account-{index}", AS_OF.isoformat())
            for index in range(1, 6)
        ],
    )
    connection.commit()
    connection.close()
    account_queries: list[str] = []

    class RecordingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if "SELECT DISTINCT account_id FROM ledger_transactions" in sql:
                account_queries.append(sql)
            return super().execute(sql, parameters)

    def recording_connect(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path, factory=RecordingConnection)
        connection.row_factory = sqlite3.Row
        return connection

    def fail_if_unbounded_materialized(_connection, account_ids, **_kwargs):
        assert len(account_ids) <= 3
        raise AssertionError("snapshot materialization should not run after cap overflow")

    monkeypatch.setattr(coordinator_module, "connect", recording_connect)
    monkeypatch.setattr(
        coordinator_module, "materialize_ledger_snapshots", fail_if_unbounded_materialized
    )

    with pytest.raises(ValueError, match="ledger snapshot account limit"):
        run_review(
            collector_snapshot=collector(), as_of=AS_OF.replace(hour=22, minute=30),
            report_date="2026-07-12", candidate_codes=(CODE,),
            run_id="review-bounded-account-discovery",
            quality_evaluator=passed_quality,
            **paths,
        )

    assert account_queries
    assert "LIMIT" in account_queries[0].upper()
    assert query_all(paths["db_path"], "SELECT COUNT(*) FROM portfolio_snapshots") == [(0,)]


def test_review_rejects_excessive_ledger_context_items_before_rendering(
    tmp_path: Path,
    monkeypatch,
):
    from advisor.db.migrate import migrate_database

    monkeypatch.setattr(coordinator_module, "MAX_REVIEW_LEDGER_CONTEXT_ITEMS", 3, raising=False)
    paths = coordinator_paths(tmp_path)
    migrate_database(paths["db_path"])
    seed_premarket_report(
        paths,
        database_run_id="initial",
        report_run_id="initial",
        advice=[
            AdviceItem("advice-1", CODE, "watch", 0.7, "fixture rationale", ()),
            AdviceItem("advice-2", "000001", "watch", 0.7, "fixture rationale", ()),
        ],
    )
    connection = sqlite3.connect(paths["db_path"])
    connection.executemany(
        "INSERT INTO ledger_accounts (account_id, name, created_at) VALUES (?, ?, ?)",
        [
            ("account-1", "Account 1", AS_OF.isoformat()),
            ("account-2", "Account 2", AS_OF.isoformat()),
        ],
    )
    connection.executemany(
        "INSERT INTO ledger_transactions (transaction_id, account_id, trade_date, "
        "transaction_type, code, quantity, price, amount, fees, source, created_at) "
        "VALUES (?, ?, '2026-07-11', 'cash_deposit', NULL, 0, 0, 1000, 0, 'fixture', ?)",
        [
            ("deposit-1", "account-1", AS_OF.isoformat()),
            ("deposit-2", "account-2", AS_OF.isoformat()),
        ],
    )
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="review ledger context limit"):
        run_review(
            collector_snapshot=collector(), as_of=AS_OF.replace(hour=22, minute=30),
            report_date="2026-07-12", candidate_codes=(CODE, "000001"),
            run_id="review-too-many-context-items", quality_evaluator=passed_quality, **paths,
        )

    assert not (paths["output_dir"] / "2026-07-12" / "review.json").exists()


def test_review_truncates_excessive_same_day_trade_matches_per_context_item(
    tmp_path: Path,
    monkeypatch,
):
    from advisor.db.migrate import migrate_database

    monkeypatch.setattr(
        coordinator_module, "MAX_REVIEW_LEDGER_MATCHES_PER_CONTEXT_ITEM", 2, raising=False
    )
    paths = coordinator_paths(tmp_path)
    migrate_database(paths["db_path"])
    seed_market(paths["db_path"])
    seed_premarket_report(
        paths,
        database_run_id="initial",
        report_run_id="initial",
        advice=[AdviceItem("advice-1", CODE, "watch", 0.7, "fixture rationale", ())],
    )
    connection = sqlite3.connect(paths["db_path"])
    connection.execute(
        "INSERT INTO ledger_accounts (account_id, name, created_at) VALUES ('account-1', 'Account', ?)",
        (AS_OF.isoformat(),),
    )
    connection.executemany(
        "INSERT INTO ledger_transactions (transaction_id, account_id, trade_date, "
        "transaction_type, code, quantity, price, amount, fees, source, created_at) "
        "VALUES (?, 'account-1', '2026-07-12', 'buy', ?, 1, 10, -10, 0, 'fixture', ?)",
        [
            (f"match-{index}", CODE, AS_OF.isoformat())
            for index in range(1, 5)
        ],
    )
    connection.commit()
    connection.close()

    result = run_review(
        collector_snapshot=collector(), as_of=AS_OF.replace(hour=22, minute=30),
        report_date="2026-07-12", candidate_codes=(CODE,),
        run_id="review-bounded-matches",
        quality_evaluator=passed_quality,
        **paths,
    )

    context = json.loads(result.report_paths.json_path.read_text(encoding="utf-8"))["context"]
    impact = json.loads(context["ledger_impact"][0])
    assert impact["transactions"] == [
        {"transaction_id": "match-1", "transaction_type": "buy"},
        {"transaction_id": "match-2", "transaction_type": "buy"},
    ]
    assert impact["transactions_truncated"] is True
    assert impact["transaction_match_count"] == 4
    assert query_all(
        paths["db_path"],
        "SELECT transaction_id FROM advice_trade_matches ORDER BY transaction_id",
    ) == [("match-1",), ("match-2",)]


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


@pytest.mark.parametrize(
    ("module", "expected_hour"),
    [(premarket_reporting, 8), (review_reporting, 22)],
)
def test_report_cli_date_defaults_as_of_and_allows_candidate_expansion(
    module, expected_hour, tmp_path: Path, capsys
):
    calls = {}

    def snapshot_reader(_events_db, _allowed_rids_path, *, as_of):
        calls["snapshot_as_of"] = as_of
        return collector()

    def coordinator(**kwargs):
        calls["coordinator"] = kwargs
        return SimpleNamespace(
            run_id="cli-run", status="blocked", warnings=(),
            report_paths=SimpleNamespace(
                markdown_path=tmp_path / "failure.md",
                json_path=tmp_path / "failure.json",
            ),
        )

    exit_code = module.main(
        ["--date", "2026-07-12", "--output-dir", str(tmp_path / "reports")],
        coordinator=coordinator,
        snapshot_reader=snapshot_reader,
    )

    assert calls["snapshot_as_of"].date().isoformat() == "2026-07-12"
    assert (calls["snapshot_as_of"].hour, calls["snapshot_as_of"].minute) == (expected_hour, 30)
    assert calls["coordinator"]["candidate_codes"] == ()
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"


@pytest.mark.parametrize(
    ("module", "fallback"),
    [
        (premarket_reporting, "premarket-cli-failed-20260712"),
        (review_reporting, "review-cli-failed-20260712"),
    ],
)
def test_report_cli_failure_never_echoes_invalid_run_id(
    module, fallback, tmp_path: Path, capsys
):
    invalid_run_id = "token=secret"

    def failed_snapshot(*_args, **_kwargs):
        raise RuntimeError("fixture failure")

    exit_code = module.main(
        [
            "--date", "2026-07-12",
            "--run-id", invalid_run_id,
            "--output-dir", str(tmp_path / "reports"),
        ],
        snapshot_reader=failed_snapshot,
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert exit_code == 1
    assert payload["run_id"] == fallback
    assert invalid_run_id not in output
    assert payload["json_path"].endswith(f"failure.{fallback}.json")


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


def test_review_rejects_selected_premarket_archive_without_archive_row(tmp_path: Path):
    paths = coordinator_paths(tmp_path)
    from advisor.db.migrate import migrate_database
    migrate_database(paths["db_path"])
    seed_market(paths["db_path"])
    advice = AdviceItem("initial-advice", CODE, "watch", 0.5, "initial", [])
    seed_premarket_report(
        paths, database_run_id="morning-initial", report_run_id="initial", advice=[advice]
    )
    connection = sqlite3.connect(paths["db_path"])
    connection.execute("DELETE FROM report_archive WHERE run_id = 'morning-initial'")
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="premarket archive not found"):
        run_review(
            collector_snapshot=collector(), as_of=AS_OF.replace(hour=22, minute=30),
            report_date="2026-07-12", candidate_codes=(CODE,),
            quality_evaluator=passed_quality, **paths,
        )

    assert query_all(paths["db_path"], "SELECT COUNT(*) FROM reviews") == [(0,)]


def test_review_rejects_archive_with_advice_from_multiple_premarket_runs(tmp_path: Path):
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
    connection = sqlite3.connect(paths["db_path"])
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at, finished_at) "
        "VALUES ('morning-other', 'premarket', ?, 'passed', ?, ?)",
        (AS_OF.isoformat(), AS_OF.isoformat(), AS_OF.isoformat()),
    )
    connection.execute(
        "UPDATE advice SET run_id = 'morning-other' WHERE advice_id = 'advice-2'"
    )
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="morning advice does not match premarket archive"):
        run_review(
            collector_snapshot=collector(), as_of=AS_OF.replace(hour=22, minute=30),
            report_date="2026-07-12", candidate_codes=(CODE, "000001"),
            quality_evaluator=passed_quality, **paths,
        )

    assert query_all(paths["db_path"], "SELECT COUNT(*) FROM reviews") == [(0,)]


def test_review_rejects_archive_missing_advice_from_linked_premarket_run(tmp_path: Path):
    paths = coordinator_paths(tmp_path)
    from advisor.db.migrate import migrate_database
    migrate_database(paths["db_path"])
    archived = AdviceItem("advice-1", CODE, "watch", 0.5, "archived", [])
    seed_premarket_report(
        paths,
        database_run_id="morning-initial",
        report_run_id="initial",
        advice=[archived],
    )
    connection = sqlite3.connect(paths["db_path"])
    connection.execute(
        "INSERT INTO securities (code, name, exchange, created_at, updated_at) "
        "VALUES ('000001', '000001', 'SZSE', ?, ?)",
        (AS_OF.isoformat(), AS_OF.isoformat()),
    )
    connection.execute(
        "INSERT INTO advice (advice_id, run_id, code, action, confidence, rationale, "
        "evidence_ids_json, created_at) VALUES "
        "('advice-2', 'morning-initial', '000001', 'watch', 0.5, 'omitted', '[]', ?)",
        (AS_OF.isoformat(),),
    )
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="morning advice does not match premarket archive"):
        run_review(
            collector_snapshot=collector(), as_of=AS_OF.replace(hour=22, minute=30),
            report_date="2026-07-12", candidate_codes=(CODE,),
            quality_evaluator=passed_quality, **paths,
        )

    assert query_all(paths["db_path"], "SELECT COUNT(*) FROM reviews") == [(0,)]


def test_review_linkage_cannot_change_between_validation_and_publication(
    tmp_path: Path, monkeypatch
):
    paths = coordinator_paths(tmp_path)
    from advisor.db.migrate import migrate_database
    migrate_database(paths["db_path"])
    advice = AdviceItem("initial-advice", CODE, "watch", 0.5, "initial", [])
    seed_premarket_report(
        paths, database_run_id="morning-initial", report_run_id="initial", advice=[advice]
    )
    real_load = coordinator_module._load_morning_advice

    def load_then_mutate_linkage(connection, *args):
        items = real_load(connection, *args)
        racer = sqlite3.connect(paths["db_path"], timeout=0.01)
        try:
            racer.execute("DELETE FROM report_archive WHERE run_id = 'morning-initial'")
            racer.commit()
        finally:
            racer.close()
        return items

    monkeypatch.setattr(coordinator_module, "_load_morning_advice", load_then_mutate_linkage)

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        run_review(
            collector_snapshot=collector(), as_of=AS_OF.replace(hour=22, minute=30),
            report_date="2026-07-12", candidate_codes=(CODE,),
            quality_evaluator=passed_quality, **paths,
        )

    assert query_all(paths["db_path"], "SELECT COUNT(*) FROM reviews") == [(0,)]
    assert query_all(
        paths["db_path"], "SELECT COUNT(*) FROM report_archive WHERE run_id = 'morning-initial'"
    ) == [(1,)]


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


def test_premarket_archive_failure_rolls_back_all_projection_rows(
    tmp_path: Path, monkeypatch
):
    paths = coordinator_paths(tmp_path)
    from advisor.db.migrate import migrate_database
    migrate_database(paths["db_path"])
    seed_market(paths["db_path"])

    def fail_archive(*_args, **_kwargs):
        raise sqlite3.IntegrityError("fixture premarket archive failure")

    monkeypatch.setattr(coordinator_module, "_archive_report", fail_archive)
    with pytest.raises(sqlite3.IntegrityError, match="fixture premarket archive failure"):
        run_premarket(
            collector_snapshot=collector(), analyst_runner=PassingRunner(), as_of=AS_OF,
            report_date="2026-07-12", candidate_codes=(CODE,), run_id="premarket-failed",
            quality_evaluator=passed_quality, evidence_persister=persist_fixture_evidence,
            **paths,
        )

    for table in (
        "advice",
        "chart_assets",
        "stock_profiles",
        "stock_profile_history",
        "report_archive",
    ):
        assert query_all(paths["db_path"], f"SELECT COUNT(*) FROM {table}") == [(0,)]
    assert query_all(
        paths["db_path"],
        "SELECT status FROM advisor_runs WHERE run_id = 'premarket-failed'",
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
        "VALUES ('t1', 'a1', '2026-07-12', 'buy', ?, 1, 9, -9, 0, 'fixture', ?)",
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


def test_review_treats_decline_as_favorable_for_reduce_and_lists_ledger_types(tmp_path: Path):
    paths = coordinator_paths(tmp_path)
    from advisor.db.migrate import migrate_database
    migrate_database(paths["db_path"])
    advice = AdviceItem("initial-advice", CODE, "reduce", 0.75, "risk reduction", [])
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
    connection.executemany(
        "INSERT INTO ledger_transactions (transaction_id, account_id, trade_date, "
        "transaction_type, code, quantity, price, amount, fees, source, created_at) "
        "VALUES (?, 'a1', '2026-07-12', ?, ?, 100, 9, ?, 0, 'fixture', ?)",
        [
            ("t1", "buy", CODE, -900, AS_OF.isoformat()),
            ("t2", "sell", CODE, 900, AS_OF.isoformat()),
        ],
    )
    connection.commit()
    connection.close()

    result = run_review(
        collector_snapshot=collector(), as_of=AS_OF.replace(hour=22, minute=30),
        report_date="2026-07-12", candidate_codes=(CODE,),
        quality_evaluator=passed_quality, **paths,
    )

    review = json.loads(result.report_paths.json_path.read_text(encoding="utf-8"))["reviews"][0]
    assert review["outcome"] == "followed_strength"
    assert "2 ledger transactions" in review["review_text"]
    assert "buy=1" in review["review_text"]
    assert "sell=1" in review["review_text"]


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


def test_premarket_uses_configured_tradingagents_runtime_when_runner_is_not_injected(
    tmp_path: Path,
    monkeypatch,
):
    created = []

    class ConfiguredRunner(PassingRunner):
        def __init__(self, *, repository_path, upstream_config):
            created.append((str(repository_path), upstream_config))

    monkeypatch.setattr(coordinator_module, "ExternalTradingAgentsRunner", ConfiguredRunner)
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
        "trading_agents": {
            "repository_path": "/opt/tradingagents-astock",
            "upstream_provider": "dashscope",
            "upstream_model": "qwen-plus",
        },
    }), encoding="utf-8")

    result = run_premarket(
        collector_snapshot=collector(), as_of=AS_OF,
        report_date="2026-07-12", candidate_codes=(CODE,),
        output_dir=tmp_path / "reports", config_path=config_path, root=tmp_path,
        quality_evaluator=passed_quality, evidence_persister=persist_fixture_evidence,
    )

    assert result.status == "passed"
    assert created == [
        (
            "/opt/tradingagents-astock",
            {"llm_provider": "dashscope", "deep_think_llm": "qwen-plus", "quick_think_llm": "qwen-plus"},
        )
    ]
