from dataclasses import dataclass
from typing import Protocol


ANALYST_ROLES = (
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


@dataclass(frozen=True)
class AnalystOutput:
    role: str
    code: str
    summary: str
    payload: dict


class AnalystRunner(Protocol):
    def run(self, code: str, trade_date: str, evidence: list[dict]) -> list[AnalystOutput]:
        raise NotImplementedError


class ExternalTradingAgentsRunner:
    def run(self, code: str, trade_date: str, evidence: list[dict]) -> list[AnalystOutput]:
        raise RuntimeError(
            "Install and configure /Users/mac/Documents/TradingAgents-astock before live analyst runs"
        )


def run_analyst_flow(
    code: str,
    trade_date: str,
    evidence: list[dict],
    runner: AnalystRunner | None = None,
) -> list[AnalystOutput]:
    active_runner = runner or ExternalTradingAgentsRunner()
    outputs = active_runner.run(code, trade_date, evidence)
    missing = set(ANALYST_ROLES) - {output.role for output in outputs}
    if missing:
        raise ValueError(f"missing analyst outputs: {sorted(missing)}")
    return outputs
