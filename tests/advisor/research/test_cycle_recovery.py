from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from advisor.research.cli import build_runtime, run_research_cycle
from tests.advisor.research.test_end_to_end import FixtureCodex, _registry


def test_stage_failure_is_persisted_as_blocked_without_a_team_conclusion(tmp_path: Path):
    class FailingStageCodex(FixtureCodex):
        def execute(self, capsule, policy, *, validator=None, **kwargs):
            manifest = json.loads(capsule.manifest_path.read_text(encoding="utf-8"))
            if manifest["label"] == "decision-bull_review":
                raise RuntimeError("fixture stage failure")
            return super().execute(capsule, policy, validator=validator, **kwargs)

    runtime = build_runtime(
        root=Path.cwd(),
        db_path=tmp_path / "research.sqlite",
        artifact_dir=tmp_path / "artifacts",
        provider_registry=_registry(),
        executor=FailingStageCodex(),
    )
    try:
        run = run_research_cycle(
            runtime,
            code="600519",
            as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc),
            reports_root=tmp_path / "reports",
            cycle_id="cycle-recovery-1",
        )
        assert run.cycle.status.value == "blocked"
        team_dir = run.publication.root / "teams" / "a_share_core@2"
        assert (team_dir / "status.json").is_file()
        assert not (team_dir / "conclusion.json").exists()
        row = runtime.repository.connection.execute(
            "SELECT status, message FROM research_stage_runs WHERE stage_name = 'bull_review'"
        ).fetchone()
        assert row[0] == "blocked"
        assert "Decision Stage" in row[1]
        finding_boundary = runtime.repository.connection.execute(
            "SELECT status FROM research_stage_runs WHERE stage_name = 'finding_quality'"
        ).fetchone()
        assert finding_boundary[0] == "passed"
        assert "finding_quality" in runtime.cycle_engine._load_stage_cache(
            "cycle-recovery-1", "a_share_core@2"
        )
        attempts = runtime.repository.connection.execute(
            "SELECT status, model, policy_json FROM research_stage_attempts WHERE stage_run_id LIKE '%bull_review'"
        ).fetchone()
        assert attempts[0] == "failed"
        assert attempts[1] == runtime.policy.model
        assert json.loads(attempts[2])["reasoning_effort"] == runtime.policy.reasoning_effort
    finally:
        runtime.close()
