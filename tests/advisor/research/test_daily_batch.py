from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from advisor.research.batch import CandidateSelectionError, DailyResearchBatch, select_candidates
from advisor.research.cli import run_daily_batch
from advisor.research.contracts import ExecutionPolicy, ResearchBoundary, ResearchSubject, RunStatus, VersionRef
from advisor.research.state_machine import ResearchCycleResult, TeamRunResult


def _policy() -> ExecutionPolicy:
    return ExecutionPolicy(policy="codex@1", model="gpt-test", reasoning_effort="medium", timeout_seconds=30, max_agent_concurrency=2, max_stage_concurrency=2)


def _cycle(code: str, team: str = "core@1"):
    subject = ResearchSubject(code=code)
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))
    return ResearchCycleResult(
        f"batch-1-{code}", subject, boundary, RunStatus.passed, "f" * 64, None, {}, {},
        {team: TeamRunResult(VersionRef.parse(team), RunStatus.passed, conclusion=SimpleNamespace(conclusion="private conclusion"))},
    )


class FakeCycleEngine:
    def __init__(self):
        self.calls = []

    def run_cycle(self, cycle_id, subject, boundary, team_refs, policy):
        self.calls.append((cycle_id, subject.code, tuple(team_refs), str(policy.policy), boundary.as_of))
        return _cycle(subject.code)


def test_candidate_selection_is_stable_deduplicated_and_records_sources():
    selection = select_candidates(explicit_codes=("600519", "000001"), mx_codes=("000001", "300750"), position_codes=("600519", "601318"), max_subjects=10)

    assert [item.subject.code for item in selection] == ["600519", "000001", "300750", "601318"]
    assert selection[0].sources == ("explicit", "position")
    assert selection[1].sources == ("explicit", "mx")


def test_candidate_limit_fails_closed():
    with pytest.raises(CandidateSelectionError, match="maximum"):
        select_candidates(explicit_codes=("600519", "000001"), max_subjects=1)


def test_daily_batch_uses_independent_cycles_with_shared_batch_inputs():
    engine = FakeCycleEngine()
    batch = DailyResearchBatch(engine, max_cycle_concurrency=2)
    selection = select_candidates(explicit_codes=("600519", "000001"), max_subjects=10)
    result = batch.run("batch-1", selection, ("core@1",), ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc)), _policy())

    assert result.status == RunStatus.passed
    assert set(result.cycles) == {"600519", "000001"}
    assert {call[2] for call in engine.calls} == {("core@1",)}
    assert {call[3] for call in engine.calls} == {"codex@1"}
    assert {call[4] for call in engine.calls} == {datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc)}


def test_empty_daily_team_set_skips_before_any_runtime_dependency_is_used():
    class NoCalls:
        def __getattr__(self, name):
            raise AssertionError(f"empty Daily Team Set must not use {name}")

    runtime = SimpleNamespace(
        config=SimpleNamespace(research=SimpleNamespace(default_teams=[], max_subjects=20)),
        policy=_policy(),
        repository=NoCalls(),
        cycle_engine=NoCalls(),
        artifact_store=NoCalls(),
        root=NoCalls(),
    )

    result = run_daily_batch(
        runtime,
        batch_id="batch-empty",
        codes=("600519",),
        as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc),
    )

    assert result.batch.status == "skipped"
    assert result.batch.team_refs == ()
    assert result.batch.cycles == {}
    assert result.publications == {}
    assert result.briefs == {}
