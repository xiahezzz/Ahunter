from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Protocol
import re
import sys


UPSTREAM_ANALYST_ROLES = (
    "market",
    "social",
    "news",
    "fundamentals",
    "policy",
    "hot_money",
    "lockup",
)

ANALYST_ROLES = UPSTREAM_ANALYST_ROLES + (
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

DEFAULT_TRADINGAGENTS_REPOSITORY = Path("/Users/mac/Documents/TradingAgents-astock")
_HARD_CHECKS_HEADER = "### 硬检查结果"
_HARD_CHECK_GRADE = re.compile(r"\[([ABCDF])\]")


class DataQualityBlockedError(RuntimeError):
    """Raised when evidence or the upstream quality gate blocks advisory output."""


@dataclass(frozen=True)
class QualityOutcome:
    passed: bool
    summary: str

    @classmethod
    def from_upstream_summary(cls, summary: str) -> "QualityOutcome":
        if not isinstance(summary, str) or _HARD_CHECKS_HEADER not in summary:
            raise DataQualityBlockedError("missing or ambiguous upstream quality outcome")

        hard_checks = summary.split(_HARD_CHECKS_HEADER, 1)[1].split("###", 1)[0]
        grades = _HARD_CHECK_GRADE.findall(hard_checks)
        if len(grades) != len(UPSTREAM_ANALYST_ROLES):
            raise DataQualityBlockedError("missing or ambiguous upstream quality outcome")

        return cls(passed=not any(grade in {"D", "F"} for grade in grades), summary=summary)


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
    def __init__(
        self,
        repository_path: Path | str = DEFAULT_TRADINGAGENTS_REPOSITORY,
        graph_factory: Callable[[list[str]], Any] | None = None,
    ) -> None:
        self.repository_path = Path(repository_path)
        self.graph_factory = graph_factory

    def run(self, code: str, trade_date: str, evidence: list[dict]) -> list[AnalystOutput]:
        graph = self._create_graph()
        graph.config["checkpoint_enabled"] = False
        initial_state, args, _ = graph.prepare_graph_run(code, trade_date)
        stream_args = dict(args)
        stream_args["stream_mode"] = "values"
        final_state: dict[str, Any] | None = None
        quality_outcome: QualityOutcome | None = None

        try:
            for state in graph.graph.stream(initial_state, **stream_args):
                final_state = state
                if "data_quality_summary" in state:
                    quality_outcome = QualityOutcome.from_upstream_summary(state["data_quality_summary"])
                    if not quality_outcome.passed:
                        raise DataQualityBlockedError("upstream quality gate failed mandatory hard checks")

            if final_state is None or quality_outcome is None:
                raise DataQualityBlockedError("missing or ambiguous upstream quality outcome")

            graph.finalize_graph_run(code, trade_date, final_state)
            return _outputs_from_state(code, final_state, quality_outcome)
        finally:
            graph.close_graph_run()

    def _create_graph(self) -> Any:
        if self.graph_factory is not None:
            return self.graph_factory(list(UPSTREAM_ANALYST_ROLES))

        if not self.repository_path.is_dir():
            raise RuntimeError(
                f"TradingAgents-astock repository is unavailable at {self.repository_path}; "
                "configure ExternalTradingAgentsRunner(repository_path=...)"
            )

        repository = str(self.repository_path)
        if repository not in sys.path:
            sys.path.insert(0, repository)
        try:
            graph_class = import_module("tradingagents.graph.trading_graph").TradingAgentsGraph
        except (ImportError, AttributeError) as error:
            raise RuntimeError(
                "TradingAgents-astock dependencies or graph configuration are unavailable; "
                "install its declared dependencies and configure its LLM provider"
            ) from error
        return graph_class(selected_analysts=list(UPSTREAM_ANALYST_ROLES))


def run_analyst_flow(
    code: str,
    trade_date: str,
    evidence: list[dict],
    runner: AnalystRunner | None = None,
) -> list[AnalystOutput]:
    if _has_blocking_evidence(evidence):
        raise DataQualityBlockedError("input evidence has a blocking quality failure")

    active_runner = runner if runner is not None else ExternalTradingAgentsRunner()
    outputs = active_runner.run(code, trade_date, evidence)
    _validate_outputs(outputs)
    return outputs


def _has_blocking_evidence(evidence: list[dict]) -> bool:
    for item in evidence:
        if item.get("blocking_failure") is True:
            return True
        if str(item.get("quality_status", "")).lower() in {"failed", "blocked"}:
            return True
    return False


def _validate_outputs(outputs: list[AnalystOutput]) -> None:
    output_roles = {output.role for output in outputs}
    missing = set(ANALYST_ROLES) - output_roles
    if missing:
        raise DataQualityBlockedError(f"missing analyst outputs: {sorted(missing)}")

    quality_outputs = [output for output in outputs if output.role == "quality_gate"]
    if len(quality_outputs) != 1:
        raise DataQualityBlockedError("missing or ambiguous quality outcome")

    outcome = quality_outputs[0].payload.get("quality_outcome")
    if not isinstance(outcome, QualityOutcome):
        raise DataQualityBlockedError("missing or ambiguous quality outcome")
    if not outcome.passed:
        raise DataQualityBlockedError("quality gate blocked advisory output")


def _outputs_from_state(
    code: str,
    state: dict[str, Any],
    quality_outcome: QualityOutcome,
) -> list[AnalystOutput]:
    investment_debate = state.get("investment_debate_state", {})
    risk_debate = state.get("risk_debate_state", {})
    summaries = {
        "market": state.get("market_report", ""),
        "social": state.get("sentiment_report", ""),
        "news": state.get("news_report", ""),
        "fundamentals": state.get("fundamentals_report", ""),
        "policy": state.get("policy_report", ""),
        "hot_money": state.get("hot_money_report", ""),
        "lockup": state.get("lockup_report", ""),
        "quality_gate": quality_outcome.summary,
        "bull_researcher": investment_debate.get("bull_history", ""),
        "bear_researcher": investment_debate.get("bear_history", ""),
        "research_manager": state.get("investment_plan") or investment_debate.get("judge_decision", ""),
        "trader": state.get("trader_investment_plan", ""),
        "aggressive_risk": risk_debate.get("aggressive_history", ""),
        "neutral_risk": risk_debate.get("neutral_history", ""),
        "conservative_risk": risk_debate.get("conservative_history", ""),
        "portfolio_manager": state.get("final_trade_decision", ""),
    }
    return [
        AnalystOutput(
            role=role,
            code=code,
            summary=summaries[role],
            payload={"quality_outcome": quality_outcome} if role == "quality_gate" else {},
        )
        for role in ANALYST_ROLES
    ]
