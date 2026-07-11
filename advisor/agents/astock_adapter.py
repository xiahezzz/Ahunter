from dataclasses import dataclass
from importlib import import_module
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable, Protocol


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
_SAFE_EVIDENCE_FIELDS = (
    "evidence_id",
    "as_of",
    "code",
    "name",
    "source_type",
    "source_id",
    "summary",
    "confidence",
    "facts",
    "inferences",
    "conflicts",
    "quality_flags",
    "quality_status",
    "blocking_failure",
)
_COLLECTION_EVIDENCE_FIELDS = (
    "facts",
    "inferences",
    "conflicts",
    "quality_flags",
)
_SCALAR_EVIDENCE_FIELDS = tuple(
    field for field in _SAFE_EVIDENCE_FIELDS if field not in _COLLECTION_EVIDENCE_FIELDS
)
_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "password",
        "credential",
        "authorization",
        "cookie",
        "token",
        "session",
        "socket",
        "socket_id",
        "socketio",
        "socket_io",
        "chrome",
        "chrome_debugging_id",
        "debug",
        "debug_id",
        "debugger",
        "cdp",
        "raw",
        "payload",
        "raw_ref",
    }
)
_SENSITIVE_FIELD_PARTS = (
    "credential",
    "cookie",
    "token",
    "socket",
    "debug",
    "raw",
    "password",
    "authorization",
    "session",
    "api_key",
    "apikey",
    "secret",
    "payload",
)
_SENSITIVE_VALUE = re.compile(
    r"\b(?:api[_-]?key|apikey|secret|password|credential|authorization|cookie|token|session|socket(?:[_.-]?id)?|chrome(?:[ _.-]?debug(?:ging)?(?:[ _.-]?id)?)?|debug(?:ger|[ _.-]?id)?|cdp|raw(?:[ _.-]?ref)?|payload)\s*[:=]",
    re.IGNORECASE,
)
_SECRET_VALUE_PREFIX = re.compile(r"\b(?:sk|pk|sess)_[A-Za-z0-9_-]{8,}", re.IGNORECASE)
_MAX_EVIDENCE_ITEMS = 20
_MAX_EVIDENCE_CONTEXT_CHARS = 6_000
_MAX_EVIDENCE_VALUE_CHARS = 800


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
        graph_factory: Callable[[list[str], dict[str, Any]], Any] | None = None,
        upstream_config: dict[str, Any] | None = None,
    ) -> None:
        self.repository_path = Path(repository_path)
        self.graph_factory = graph_factory
        self.upstream_config = upstream_config

    def run(self, code: str, trade_date: str, evidence: list[dict]) -> list[AnalystOutput]:
        graph: Any | None = None

        try:
            graph = self._create_graph(self._graph_config())
            initial_state, args, _ = graph.prepare_graph_run(code, trade_date)
            if initial_state is None:
                raise DataQualityBlockedError("cannot inject current evidence into a resumed graph state")
            _inject_evidence_context(initial_state, evidence)

            stream_args = dict(args)
            stream_args["stream_mode"] = "values"
            final_state: dict[str, Any] | None = None
            quality_outcome: QualityOutcome | None = None
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
            if graph is not None:
                graph.close_graph_run()

    def _graph_config(self) -> dict[str, Any]:
        source_config = self.upstream_config
        if source_config is None:
            if self.graph_factory is not None:
                source_config = {}
            else:
                self._configure_repository_path()
                try:
                    source_config = import_module("tradingagents.default_config").DEFAULT_CONFIG
                except (ImportError, AttributeError) as error:
                    raise RuntimeError(
                        "TradingAgents-astock dependencies or graph configuration are unavailable; "
                        "install its declared dependencies and configure its LLM provider"
                    ) from error

        config = dict(source_config)
        config["checkpoint_enabled"] = False
        return config

    def _create_graph(self, config: dict[str, Any]) -> Any:
        if self.graph_factory is not None:
            return self.graph_factory(list(UPSTREAM_ANALYST_ROLES), config)

        self._configure_repository_path()
        try:
            graph_class = import_module("tradingagents.graph.trading_graph").TradingAgentsGraph
        except (ImportError, AttributeError) as error:
            raise RuntimeError(
                "TradingAgents-astock dependencies or graph configuration are unavailable; "
                "install its declared dependencies and configure its LLM provider"
            ) from error
        return graph_class(selected_analysts=list(UPSTREAM_ANALYST_ROLES), config=config)

    def _configure_repository_path(self) -> None:
        if not self.repository_path.is_dir():
            raise RuntimeError(
                f"TradingAgents-astock repository is unavailable at {self.repository_path}; "
                "configure ExternalTradingAgentsRunner(repository_path=...)"
            )

        repository = str(self.repository_path)
        if repository not in sys.path:
            sys.path.insert(0, repository)


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


def _inject_evidence_context(initial_state: dict[str, Any], evidence: list[dict]) -> None:
    context = _build_evidence_context(evidence)
    if not context:
        return

    messages = list(initial_state.get("messages", []))
    messages.append(("human", context))
    initial_state["messages"] = messages

    existing_context = initial_state.get("past_context", "")
    initial_state["past_context"] = f"{existing_context}\n\n{context}".strip()


def _build_evidence_context(evidence: list[dict]) -> str:
    records = []
    for item in evidence[:_MAX_EVIDENCE_ITEMS]:
        normalized = _normalize_evidence_item(item)
        if normalized:
            records.append(normalized)

    if not records:
        return ""
    serialized = json.dumps(records, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return (
        "Authorized normalized evidence context. Cite stable evidence_id values where feasible.\n"
        f"{serialized}"
    )[:_MAX_EVIDENCE_CONTEXT_CHARS]


def _normalize_evidence_item(item: dict[str, Any]) -> dict[str, Any]:
    normalized = {}
    for field in _SCALAR_EVIDENCE_FIELDS:
        if field not in item or _is_sensitive_field(field):
            continue
        value = _bounded_scalar(item[field])
        if value is not None:
            normalized[field] = value

    for field in _COLLECTION_EVIDENCE_FIELDS:
        if field not in item or _is_sensitive_field(field):
            continue
        value = _bounded_scalar_collection(item[field])
        if value:
            normalized[field] = value
    return normalized


def _bounded_scalar_collection(value: Any) -> list[bool | int | float | str]:
    if not isinstance(value, (list, tuple)):
        return []
    values = []
    for item in value[:20]:
        scalar = _bounded_scalar(item)
        if scalar is not None:
            values.append(scalar)
    return values


def _bounded_scalar(value: Any) -> bool | int | float | str | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        if _SENSITIVE_VALUE.search(value) or _SECRET_VALUE_PREFIX.search(value):
            return None
        return value[:_MAX_EVIDENCE_VALUE_CHARS]
    return None


def _is_sensitive_field(field: str) -> bool:
    lowered = field.lower().replace("-", "_")
    if lowered in _SENSITIVE_FIELD_NAMES:
        return True
    return any(part in lowered for part in _SENSITIVE_FIELD_PARTS)


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
