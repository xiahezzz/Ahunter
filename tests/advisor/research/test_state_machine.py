from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from advisor.research.agents.runner import AgentFindingError
from advisor.research.catalog import ManifestCatalog
from advisor.research.contracts import (
    AgentManifest,
    DataProductManifest,
    DecisionPipelineManifest,
    ExecutionPolicy,
    FindingQuality,
    ResearchBoundary,
    ResearchFinding,
    ResearchSubject,
    ResearchTeam,
    RunStatus,
)
from advisor.research.data_products.engine import ProductResult, SnapshotResult
from advisor.research.state_machine import InvalidTransition, ResearchCycleEngine, transition


def _catalog() -> ManifestCatalog:
    pipeline = DecisionPipelineManifest(
        pipeline="decision@1",
        stages=(
            "finding_quality", "bull_review", "bear_review", "research_manager", "trader",
            "aggressive_risk", "neutral_risk", "conservative_risk", "portfolio_manager", "publication_quality",
        ),
        parallel_groups=(("bull_review", "bear_review"), ("aggressive_risk", "neutral_risk", "conservative_risk")),
    )
    return ManifestCatalog(
        products={"bars@1": DataProductManifest(product="bars@1", title="Bars", providers=("fixture",))},
        agents={
            "market@1": AgentManifest(agent="market@1", title="Market", instructions="inspect", required_products=("bars@1",)),
            "social@1": AgentManifest(agent="social@1", title="Social", instructions="inspect", required_products=("bars@1",)),
        },
        teams={
            "core@1": ResearchTeam(team="core@1", title="Core", agents=("market@1",)),
            "social@1": ResearchTeam(team="social@1", title="Social", agents=("social@1",)),
        },
        pipelines={"decision@1": pipeline},
        execution_policies={},
    ).validate()


def _policy() -> ExecutionPolicy:
    return ExecutionPolicy(policy="codex@1", model="gpt-test", reasoning_effort="medium", timeout_seconds=30, max_agent_concurrency=2, max_stage_concurrency=2)


def _finding(agent: str, subject: ResearchSubject, boundary: ResearchBoundary) -> ResearchFinding:
    agent_ref = __import__("advisor.research.contracts", fromlist=["VersionRef"]).VersionRef.parse(agent)
    return ResearchFinding(
        agent=agent_ref,
        subject=subject,
        boundary=boundary,
        summary="fixture",
        claims=(),
        evidence=(),
        risks=(),
        invalidation_conditions=(),
        quality=FindingQuality(status="passed"),
        details={},
    )


class FakeProductEngine:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = 0

    def build_snapshot(self, **kwargs):
        self.calls += 1
        return self.snapshot


class FakeAgentRunner:
    def __init__(self, catalog, fail=None):
        self.catalog = catalog
        self.fail = fail
        self.calls = []

    def run(self, agent_ref, snapshot, policy):
        self.calls.append(str(agent_ref))
        if str(agent_ref) == self.fail:
            raise AgentFindingError("fixture failure")
        return SimpleNamespace(
            invocation_key=f"invocation-{agent_ref}",
            finding=_finding(str(agent_ref), snapshot.subject, snapshot.boundary),
        )


class FakeDecisionPipeline:
    def __init__(self):
        self.calls = []

    def run(self, team_ref, snapshot, findings, policy):
        self.calls.append((str(team_ref), tuple(sorted(findings))))
        return SimpleNamespace(conclusion=SimpleNamespace(team=team_ref))


def _snapshot() -> SnapshotResult:
    subject = ResearchSubject(code="600519")
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))
    product = ProductResult("bars@1", {"rows": []}, "a" * 64, "fixture", "passed", None, ())
    return SnapshotResult("snapshot-1", subject, boundary, {"bars@1": product}, "b" * 64)


def test_transition_rejects_rewind_and_duplicate_terminal_publish():
    assert transition(RunStatus.pending, RunStatus.running) == RunStatus.running
    assert transition(RunStatus.running, RunStatus.passed) == RunStatus.passed
    with pytest.raises(InvalidTransition):
        transition(RunStatus.passed, RunStatus.running)
    with pytest.raises(InvalidTransition):
        transition(RunStatus.passed, RunStatus.passed)


def test_cycle_builds_snapshot_once_and_shares_one_agent_finding_across_teams():
    catalog = _catalog()
    product_engine = FakeProductEngine(_snapshot())
    agents = FakeAgentRunner(catalog)
    decisions = FakeDecisionPipeline()
    engine = ResearchCycleEngine(catalog, product_engine, agents, decisions)

    result = engine.run_cycle("cycle-1", _snapshot().subject, _snapshot().boundary, ["core@1", "social@1"], _policy())
    replay = engine.run_cycle("cycle-1", _snapshot().subject, _snapshot().boundary, ["core@1", "social@1"], _policy())

    assert result.status == RunStatus.passed
    assert replay is result
    assert product_engine.calls == 1
    assert sorted(agents.calls) == ["market@1", "social@1"]
    assert len(decisions.calls) == 2


def test_failed_agent_blocks_only_dependent_team():
    catalog = _catalog()
    agents = FakeAgentRunner(catalog, fail="market@1")
    engine = ResearchCycleEngine(catalog, FakeProductEngine(_snapshot()), agents, FakeDecisionPipeline())

    result = engine.run_cycle("cycle-1", _snapshot().subject, _snapshot().boundary, ["core@1", "social@1"], _policy())

    assert result.team_results["core@1"].status == RunStatus.blocked
    assert result.team_results["social@1"].status == RunStatus.passed


def test_unavailable_product_blocks_one_team_without_crashing_other_teams():
    from advisor.research.contracts import DataProductManifest, ResearchTeam, AgentManifest

    catalog = _catalog()
    catalog.products["quote@1"] = DataProductManifest(
        product="quote@1", title="Quote", providers=("fixture",)
    )
    catalog.agents["quote@1"] = AgentManifest(
        agent="quote@1", title="Quote", instructions="inspect", required_products=("quote@1",)
    )
    catalog.teams["quote@1"] = ResearchTeam(team="quote@1", title="Quote", agents=("quote@1",))
    agents = FakeAgentRunner(catalog)
    engine = ResearchCycleEngine(catalog, FakeProductEngine(_snapshot()), agents, FakeDecisionPipeline())

    result = engine.run_cycle(
        "cycle-missing-product",
        _snapshot().subject,
        _snapshot().boundary,
        ["core@1", "quote@1"],
        _policy(),
    )

    assert result.team_results["core@1"].status == RunStatus.passed
    assert result.team_results["quote@1"].status == RunStatus.blocked
    assert result.team_results["quote@1"].reason_code == "snapshot_unavailable"
    assert "quote@1" not in agents.calls
    assert "quote@1" in result.agent_errors
    assert "Snapshot product quote@1" in result.agent_errors["quote@1"]


def test_unavailable_product_stops_a_security_team_before_any_of_its_agents_run():
    from advisor.research.contracts import DataProductManifest, ResearchTeam, AgentManifest

    catalog = _catalog()
    catalog.products["quote@1"] = DataProductManifest(
        product="quote@1", title="Quote", providers=("fixture",)
    )
    catalog.agents["quote@1"] = AgentManifest(
        agent="quote@1", title="Quote", instructions="inspect", required_products=("quote@1",)
    )
    catalog.teams["mixed@1"] = ResearchTeam(
        team="mixed@1", title="Mixed", agents=("market@1", "quote@1")
    )
    agents = FakeAgentRunner(catalog)
    decisions = FakeDecisionPipeline()
    engine = ResearchCycleEngine(catalog, FakeProductEngine(_snapshot()), agents, decisions)

    result = engine.run_cycle(
        "cycle-missing-product-gate",
        _snapshot().subject,
        _snapshot().boundary,
        ["mixed@1"],
        _policy(),
    )

    assert result.team_results["mixed@1"].status == RunStatus.blocked
    assert result.team_results["mixed@1"].reason_code == "snapshot_unavailable"
    assert agents.calls == []
    assert decisions.calls == []
