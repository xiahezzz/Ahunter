from __future__ import annotations

import json
from pathlib import Path

import pytest

from advisor.research.artifacts import ArtifactStore
from advisor.research.contracts import ResearchScope, ResearchSubject
from advisor.research.repository import ResearchRepository


def test_research_repository_is_idempotent_and_preserves_accepted_attempt(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    capsule = store.put_text("capsule")
    output = store.put_json({"finding": "accepted"})
    conclusion = store.put_json({"conclusion": "hold"})
    query_log = store.put_text(
        '{"product":"bars@1","result_hash":"' + "c" * 64 + '"}\n',
        media_type="application/vnd.a-hunter.query-log+jsonl",
    )
    repo = ResearchRepository.open(tmp_path / "research.sqlite")
    try:
        with repo.transaction():
            repo.create_cycle(
                cycle_id="cycle-1", subject_code="600519", subject_name="Fixture",
                as_of="2026-08-06T08:30:00+00:00", fingerprint="a" * 64,
            )
            repo.create_cycle(
                cycle_id="cycle-1", subject_code="600519", subject_name="Fixture",
                as_of="2026-08-06T08:30:00+00:00", fingerprint="a" * 64,
            )
            repo.create_run(run_id="cycle-1:core@1", cycle_id="cycle-1", team_ref="core@1")
            repo.create_run(run_id="cycle-1:core@1", cycle_id="cycle-1", team_ref="core@1")
            repo.record_artifact(capsule, relative_path="63/" + capsule.content_hash[2:])
            repo.record_artifact(output, relative_path="" + output.content_hash[:2] + "/" + output.content_hash[2:])
            repo.record_artifact(conclusion, relative_path=conclusion.content_hash[:2] + "/" + conclusion.content_hash[2:])
            repo.record_artifact(query_log, relative_path=query_log.content_hash[:2] + "/" + query_log.content_hash[2:])
            repo.create_invocation(
                invocation_key="invocation-1", cycle_id="cycle-1", agent_ref="market@1",
                subject_code="600519", as_of="2026-08-06T08:30:00+00:00",
                input_hashes=["b" * 64], policy_ref="codex@1",
            )
            repo.set_status("research_invocations", "invocation_key", "invocation-1", "running")
            attempt = repo.create_attempt(
                invocation_key="invocation-1",
                attempt_number=1,
                capsule_hash=capsule.content_hash,
                policy_json={"policy": "codex@1", "model": "gpt-test", "reasoning_effort": "medium"},
                cli_version="codex-cli fixture",
                model="gpt-test",
                reasoning_effort="medium",
                usage_json={"input_tokens": 1},
            )
            repo.finish_attempt(attempt_id=attempt, status="passed", output_hash=output.content_hash, duration_ms=10)
            with pytest.raises(ValueError, match="immutable"):
                repo.finish_attempt(attempt_id=attempt, status="failed", output_hash=None, duration_ms=20)
            repo.record_finding("invocation-1", output_hash=output.content_hash, query_log_hash=query_log.content_hash)
            repo.record_finding("invocation-1", output_hash=output.content_hash, query_log_hash=query_log.content_hash)
            with pytest.raises(ValueError, match="Finding is immutable"):
                repo.record_finding(
                    "invocation-1",
                    output_hash=output.content_hash,
                    query_log_hash=conclusion.content_hash,
                )
            with pytest.raises(ValueError, match="Finding is immutable"):
                repo.record_finding("invocation-1", output_hash=conclusion.content_hash)
            repo.record_stage_run(
                stage_run_id="stage-1", research_run_id="cycle-1:core@1", stage_name="trader",
                stage_order=4, input_hashes=["b" * 64], output_hash=output.content_hash,
            )
            repo.record_team_conclusion(
                conclusion_hash=conclusion.content_hash, research_run_id="cycle-1:core@1",
                team_ref="core@1", subject_code="600519", as_of="2026-08-06T08:30:00+00:00",
                stance="hold", conviction="medium", quality_status="passed",
            )
            repo.record_report(
                report_id="report-1", cycle_id="cycle-1", team_ref="core@1", report_kind="conclusion",
                json_hash=conclusion.content_hash, markdown_hash=None, status="complete",
            )
        attempts = repo.connection.execute(
            "SELECT status, output_hash, policy_json, cli_version, model, reasoning_effort, usage_json FROM research_invocation_attempts WHERE attempt_id = ?", (attempt,)
        ).fetchone()
        assert tuple(attempts) == (
            "passed",
            output.content_hash,
            '{"model":"gpt-test","policy":"codex@1","reasoning_effort":"medium"}',
            "codex-cli fixture",
            "gpt-test",
            "medium",
            '{"input_tokens":1}',
        )
        assert repo.connection.execute("SELECT count(*) FROM research_cycles").fetchone()[0] == 1
        assert repo.connection.execute("SELECT count(*) FROM research_runs").fetchone()[0] == 1
        assert repo.connection.execute("SELECT count(*) FROM research_reports").fetchone()[0] == 1
        assert repo.connection.execute(
            "SELECT query_log_hash FROM research_invocations WHERE invocation_key = ?", ("invocation-1",)
        ).fetchone()[0] == query_log.content_hash
    finally:
        repo.close()


def test_terminal_failure_query_audit_hash_cannot_be_backfilled(tmp_path: Path):
    repo = ResearchRepository.open(tmp_path / "research.sqlite")
    try:
        with repo.transaction():
            repo.create_cycle(
                cycle_id="cycle-terminal-audit",
                subject_code="600519",
                subject_name="Fixture",
                as_of="2026-08-06T08:30:00+00:00",
                fingerprint="a" * 64,
            )
            repo.create_invocation(
                invocation_key="security-failure",
                cycle_id="cycle-terminal-audit",
                agent_ref="market@1",
                subject_code="600519",
                as_of="2026-08-06T08:30:00+00:00",
                input_hashes=["b" * 64],
                policy_ref="codex@1",
            )
            repo.set_status(
                "research_invocations",
                "invocation_key",
                "security-failure",
                "running",
            )
            repo.record_invocation_failure(
                "security-failure",
                status="failed",
                message="fixture failure",
            )
            repo.record_invocation_failure(
                "security-failure",
                status="failed",
                message="fixture failure",
            )
            with pytest.raises(ValueError, match="terminal research status is immutable"):
                repo.record_invocation_failure(
                    "security-failure",
                    status="failed",
                    message="fixture failure",
                    query_log_hash="c" * 64,
                )
            repo.create_scope_invocation(
                invocation_key="market-failure",
                cycle_id="cycle-terminal-audit",
                subject=ResearchSubject(scope=ResearchScope.market),
                agent_ref="market_breadth@1",
                as_of="2026-08-06T08:30:00+00:00",
                input_hashes=["b" * 64],
                policy_ref="codex@1",
            )
            repo.start_scope_invocation("market-failure")
            repo.record_scope_invocation_failure(
                "market-failure",
                status="failed",
                message="fixture failure",
            )
            repo.record_scope_invocation_failure(
                "market-failure",
                status="failed",
                message="fixture failure",
            )
            with pytest.raises(ValueError, match="terminal scope invocation is immutable"):
                repo.record_scope_invocation_failure(
                    "market-failure",
                    status="failed",
                    message="fixture failure",
                    query_log_hash="c" * 64,
                )
    finally:
        repo.close()


def test_research_repository_enforces_persisted_status_transitions(tmp_path: Path):
    repo = ResearchRepository.open(tmp_path / "research.sqlite")
    try:
        with repo.transaction():
            repo.create_cycle(
                cycle_id="cycle-status",
                subject_code="600519",
                subject_name="Fixture",
                as_of="2026-08-06T08:30:00+00:00",
                fingerprint="a" * 64,
            )
            with pytest.raises(ValueError, match="invalid status transition"):
                repo.set_status("research_cycles", "cycle_id", "cycle-status", "passed")
            repo.set_status("research_cycles", "cycle_id", "cycle-status", "running")
            repo.set_status("research_cycles", "cycle_id", "cycle-status", "running")
            repo.set_status("research_cycles", "cycle_id", "cycle-status", "passed", finished=True)
            with pytest.raises(ValueError, match="terminal research status"):
                repo.set_status("research_cycles", "cycle_id", "cycle-status", "running")
    finally:
        repo.close()
