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
    initial_state: dict | None = None
    prepare_error: Exception | None = None

    def __post_init__(self):
        self.config = self.config or {"checkpoint_enabled": True}
        self.graph = self

    def prepare_graph_run(self, code: str, trade_date: str):
        if self.prepare_error is not None:
            raise self.prepare_error
        return self.initial_state or {"company_of_interest": code}, {"stream_mode": "values"}, None

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

    def graph_factory(selected_analysts: list[str], config: dict):
        created_with.append(tuple(selected_analysts))
        assert config["checkpoint_enabled"] is False
        return graph

    outputs = ExternalTradingAgentsRunner(graph_factory=graph_factory).run("600519", "2026-07-11", [])

    assert created_with == [ANALYST_ROLES[:7]]
    assert graph.config["checkpoint_enabled"] is True
    assert graph.finalized is True
    assert graph.closed is True
    assert [output.role for output in outputs] == list(ANALYST_ROLES)
    assert outputs[-1].summary == "advice only"


def test_external_runner_stops_at_failing_quality_gate_before_downstream_state():
    quality_failure = passing_state("600519")
    quality_failure["data_quality_summary"] = quality_summary("D")
    graph = FakeGraph([quality_failure, {"trader_investment_plan": "must not be accepted"}])

    with pytest.raises(DataQualityBlockedError):
        ExternalTradingAgentsRunner(graph_factory=lambda _, __: graph).run("600519", "2026-07-11", [])

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


def test_external_runner_bridges_only_safe_evidence_fields_into_messages_and_past_context():
    graph = FakeGraph(
        [passing_state("600519")],
        initial_state={"messages": [("human", "600519")], "past_context": "existing portfolio memory"},
    )
    evidence = [
        {
            "evidence_id": "ev-1",
            "as_of": "2026-07-11T09:30:00+08:00",
            "code": "600519",
            "name": "Kweichow Moutai",
            "source_type": "mx",
            "source_id": "source-1",
            "summary": "Volume increased.",
            "confidence": 0.8,
            "facts": ["volume up"],
            "inferences": ["momentum"],
            "conflicts": [],
            "quality_flags": ["verified"],
            "quality_status": "passed",
            "raw_payload": "do-not-forward",
            "raw_ref": "do-not-forward",
            "cookie": "do-not-forward",
            "token": "do-not-forward",
            "socket_io_id": "do-not-forward",
            "chrome_debugging_id": "do-not-forward",
            "unknown_field": "do-not-forward",
        }
    ]

    ExternalTradingAgentsRunner(graph_factory=lambda _, __: graph).run("600519", "2026-07-11", evidence)

    context = graph.initial_state["messages"][-1][1]
    assert graph.initial_state["messages"][-1][0] == "human"
    assert "ev-1" in context
    assert "Volume increased." in context
    assert "do-not-forward" not in context
    assert "existing portfolio memory" in graph.initial_state["past_context"]
    assert "ev-1" in graph.initial_state["past_context"]


def test_external_runner_drops_nested_and_secret_compound_evidence_values():
    graph = FakeGraph(
        [passing_state("600519")],
        initial_state={"messages": [("human", "600519")], "past_context": "existing memory"},
    )
    leaked_values = ("LEAK", "LEAK2", "LEAK3", "LEAK4", "LEAK5", "LEAK6")
    evidence = [
        {
            "evidence_id": "ev-safe",
            "summary": "Safe summary.",
            "facts": ["safe fact", {"api_key": "LEAK", "unknown": "LEAK2"}, ["LEAK3"], b"LEAK4", "token=LEAK5"],
            "inferences": [{"secret": "LEAK"}, ["LEAK2"], b"LEAK3", "safe inference"],
            "conflicts": [{"password": "LEAK4"}, ["LEAK5"], b"LEAK6", "safe conflict"],
            "quality_flags": [{"raw_ref": "LEAK"}, ["LEAK2"], b"LEAK3", "authorization=LEAK4", "safe flag"],
        }
    ]

    ExternalTradingAgentsRunner(graph_factory=lambda _, __: graph).run("600519", "2026-07-11", evidence)

    message_context = graph.initial_state["messages"][-1][1]
    past_context = graph.initial_state["past_context"]
    for forbidden in (*leaked_values, "api_key", "unknown", "secret", "password", "raw_ref", "token=", "authorization="):
        assert forbidden not in message_context
        assert forbidden not in past_context
    for expected in ("ev-safe", "Safe summary.", "safe fact", "safe inference", "safe conflict", "safe flag"):
        assert expected in message_context


def test_external_runner_fails_closed_and_closes_when_initial_state_is_missing():
    graph = FakeGraph([passing_state("600519")], initial_state=None)
    graph.prepare_graph_run = lambda code, trade_date: (None, {"stream_mode": "values"}, 1)

    with pytest.raises(DataQualityBlockedError):
        ExternalTradingAgentsRunner(graph_factory=lambda _, __: graph).run("600519", "2026-07-11", [])

    assert graph.closed is True


def test_external_runner_closes_when_preparation_raises():
    graph = FakeGraph([], prepare_error=RuntimeError("prepare failed"))

    with pytest.raises(RuntimeError, match="prepare failed"):
        ExternalTradingAgentsRunner(graph_factory=lambda _, __: graph).run("600519", "2026-07-11", [])

    assert graph.closed is True


def test_graph_factory_receives_fresh_checkpoint_disabled_config_without_mutating_source():
    source_config = {"checkpoint_enabled": True, "provider": "test"}
    received: list[dict] = []
    graph = FakeGraph([passing_state("600519")])

    def graph_factory(selected_analysts: list[str], config: dict):
        received.append(config)
        return graph

    ExternalTradingAgentsRunner(
        graph_factory=graph_factory,
        upstream_config=source_config,
    ).run("600519", "2026-07-11", [])

    assert source_config == {"checkpoint_enabled": True, "provider": "test"}
    assert received[0] is not source_config
    assert received[0] == {"checkpoint_enabled": False, "provider": "test"}
