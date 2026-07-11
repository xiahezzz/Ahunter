from dataclasses import dataclass

import pytest

from advisor.agents.astock_adapter import (
    ANALYST_ROLES,
    AnalystOutput,
    DataQualityBlockedError,
    ExternalTradingAgentsRunner,
    QualityOutcome,
    run_analyst_flow,
)


def quality_output(code: str, passed: bool = True) -> AnalystOutput:
    return AnalystOutput(
        role="quality_gate",
        code=code,
        summary="quality summary",
        payload={"quality_outcome": QualityOutcome(passed=passed, summary="quality summary")},
    )


def passing_outputs(code: str) -> list[AnalystOutput]:
    return [
        quality_output(code) if role == "quality_gate" else AnalystOutput(
            role=role,
            code=code,
            summary=f"{role} summary",
            payload={},
        )
        for role in ANALYST_ROLES
    ]


class FakeRunner:
    def run(self, code: str, trade_date: str, evidence: list[dict]) -> list[AnalystOutput]:
        return passing_outputs(code)


def test_includes_full_tradingagents_astock_role_set():
    assert ANALYST_ROLES == (
        "market",
        "social",
        "news",
        "fundamentals",
        "policy",
        "hot_money",
        "lockup",
        "quality_gate",
        "bull_researcher",
        "bear_researcher",
        "research_manager",
        "trader",
        "aggressive_risk",
        "neutral_risk",
        "conservative_risk",
        "portfolio_manager",
    )


def test_run_analyst_flow_uses_injected_runner():
    outputs = run_analyst_flow("600519", "2026-07-11", [{"evidence_id": "ev-1"}], runner=FakeRunner())
    assert len(outputs) == len(ANALYST_ROLES)
    assert outputs[-1].role == "portfolio_manager"


def test_input_quality_failure_prevents_runner_invocation():
    class TrackingRunner:
        called = False

        def run(self, code: str, trade_date: str, evidence: list[dict]) -> list[AnalystOutput]:
            self.called = True
            return passing_outputs(code)

    runner = TrackingRunner()

    with pytest.raises(DataQualityBlockedError):
        run_analyst_flow("600519", "2026-07-11", [{"blocking_failure": True}], runner=runner)

    assert runner.called is False


def test_injected_runner_quality_failure_blocks_recommendation_outputs():
    class FailedQualityRunner:
        def run(self, code: str, trade_date: str, evidence: list[dict]) -> list[AnalystOutput]:
            outputs = passing_outputs(code)
            outputs[ANALYST_ROLES.index("quality_gate")] = quality_output(code, passed=False)
            return outputs

    with pytest.raises(DataQualityBlockedError):
        run_analyst_flow("600519", "2026-07-11", [], runner=FailedQualityRunner())


@dataclass
class FakeGraph:
    snapshots: list[dict]
    finalized: bool = False
    closed: bool = False
    config: dict | None = None

    def __post_init__(self):
        self.config = self.config or {"checkpoint_enabled": True}
        self.graph = self

    def prepare_graph_run(self, code: str, trade_date: str):
        return {"company_of_interest": code}, {"stream_mode": "values"}, None

    def stream(self, initial_state: dict, **args):
        yield from self.snapshots

    def finalize_graph_run(self, code: str, trade_date: str, final_state: dict):
        self.finalized = True

    def close_graph_run(self):
        self.closed = True


def passing_state(code: str) -> dict:
    return {
        "market_report": "market",
        "sentiment_report": "social",
        "news_report": "news",
        "fundamentals_report": "fundamentals",
        "policy_report": "policy",
        "hot_money_report": "hot money",
        "lockup_report": "lockup",
        "data_quality_summary": quality_summary(),
        "investment_debate_state": {"bull_history": "bull", "bear_history": "bear", "judge_decision": "judge"},
        "investment_plan": "research plan",
        "trader_investment_plan": "trader plan",
        "risk_debate_state": {
            "aggressive_history": "aggressive",
            "neutral_history": "neutral",
            "conservative_history": "conservative",
        },
        "final_trade_decision": "advice only",
    }


def quality_summary(failing_grade: str | None = None) -> str:
    grades = [failing_grade or "A"] + ["A"] * 6
    return "### 硬检查结果\n" + "\n".join(
        f"- analyst {index} [{grade}] complete" for index, grade in enumerate(grades)
    ) + "\n\n### LLM 复审\ncomplete"


def test_external_runner_uses_full_analyst_set_and_maps_passing_staged_state():
    created_with: list[tuple[str, ...]] = []
    graph = FakeGraph([passing_state("600519")])

    def graph_factory(selected_analysts: list[str]):
        created_with.append(tuple(selected_analysts))
        return graph

    outputs = ExternalTradingAgentsRunner(graph_factory=graph_factory).run("600519", "2026-07-11", [])

    assert created_with == [ANALYST_ROLES[:7]]
    assert graph.config["checkpoint_enabled"] is False
    assert graph.finalized is True
    assert graph.closed is True
    assert [output.role for output in outputs] == list(ANALYST_ROLES)
    assert outputs[-1].summary == "advice only"


def test_external_runner_stops_at_failing_quality_gate_before_downstream_state():
    quality_failure = passing_state("600519")
    quality_failure["data_quality_summary"] = quality_summary("D")
    graph = FakeGraph([quality_failure, {"trader_investment_plan": "must not be accepted"}])

    with pytest.raises(DataQualityBlockedError):
        ExternalTradingAgentsRunner(graph_factory=lambda _: graph).run("600519", "2026-07-11", [])

    assert graph.finalized is False
    assert graph.closed is True


def test_falsey_injected_runner_is_used():
    class FalseyRunner:
        called = False

        def __bool__(self) -> bool:
            return False

        def run(self, code: str, trade_date: str, evidence: list[dict]) -> list[AnalystOutput]:
            self.called = True
            return passing_outputs(code)

    runner = FalseyRunner()
    run_analyst_flow("600519", "2026-07-11", [], runner=runner)
    assert runner.called is True


@pytest.mark.parametrize(
    "outputs",
    [
        lambda: passing_outputs("600519")[:-1],
        lambda: [
            AnalystOutput(role=role, code="600519", summary=role, payload={})
            for role in ANALYST_ROLES
        ],
    ],
)
def test_missing_roles_or_quality_outcome_fail_closed(outputs):
    class Runner:
        def run(self, code: str, trade_date: str, evidence: list[dict]) -> list[AnalystOutput]:
            return outputs()

    with pytest.raises(DataQualityBlockedError):
        run_analyst_flow("600519", "2026-07-11", [], runner=Runner())
