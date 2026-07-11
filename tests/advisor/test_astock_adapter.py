from advisor.agents.astock_adapter import ANALYST_ROLES, AnalystOutput, run_analyst_flow


class FakeRunner:
    def run(self, code: str, trade_date: str, evidence: list[dict]) -> list[AnalystOutput]:
        return [
            AnalystOutput(role=role, code=code, summary=f"{role} summary", payload={"evidence_count": len(evidence)})
            for role in ANALYST_ROLES
        ]


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
