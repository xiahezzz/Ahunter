from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any, Callable, Iterable

from advisor.research.artifacts import ArtifactStore
from advisor.research.capsules import RunCapsule, build_capsule
from advisor.research.catalog import ManifestCatalog
from advisor.research.codex.executor import CodexResult
from advisor.research.contracts import (
    DecisionPipelineManifest,
    ExecutionPolicy,
    EvidenceRef,
    FindingQuality,
    ResearchFinding,
    ResearchScope,
    ResearchSubject,
    TeamConclusion,
    VersionRef,
    content_hash,
)
from advisor.research.data_products.engine import SnapshotResult
from advisor.research.decision.contracts import StageReview, TraderProposal
from advisor.research.decision.market import MarketDecisionPipeline, MarketPipelineResult
from advisor.research.language import PLAIN_CHINESE_PROMPT, validate_decision_output_language


FIXED_STAGES = (
    "finding_quality",
    "bull_review",
    "bear_review",
    "research_manager",
    "trader",
    "aggressive_risk",
    "neutral_risk",
    "conservative_risk",
    "portfolio_manager",
    "publication_quality",
)
PEER_GROUPS = (
    ("bull_review", "bear_review"),
    ("aggressive_risk", "neutral_risk", "conservative_risk"),
)


class TeamBlocked(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
        attempts: tuple[Any, ...] = (),
        capsule_hash: str | None = None,
        error_class: str | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.attempts = attempts
        self.capsule_hash = capsule_hash
        self.error_class = error_class


class DecisionPipelineCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class StageResult:
    stage: str
    output: Any
    input_hash: str
    capsule_hash: str | None
    output_hash: str
    attempts: tuple[Any, ...] = ()
    cli_version: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    usage: dict[str, Any] | None = None


@dataclass(frozen=True)
class PipelineResult:
    team: VersionRef
    subject: ResearchSubject
    boundary: Any
    conclusion: TeamConclusion
    stages: dict[str, StageResult]
    pipeline_ref: VersionRef


class DecisionPipeline:
    def __init__(
        self,
        catalog: ManifestCatalog,
        executor,
        *,
        artifact_store: ArtifactStore | None = None,
        pipeline_ref: str | VersionRef = "decision@1",
    ) -> None:
        self.catalog = catalog
        self.executor = executor
        self.artifact_store = artifact_store
        self.pipeline = catalog.pipeline(pipeline_ref)
        self._validate_fixed_pipeline(self.pipeline)
        self.market_pipeline = MarketDecisionPipeline(catalog)

    def run(
        self,
        team_ref: str | VersionRef,
        snapshot: SnapshotResult,
        findings: dict[str | VersionRef, ResearchFinding],
        policy: ExecutionPolicy,
        *,
        completed_stages: dict[str, StageResult] | None = None,
        stage_callback: Callable[[StageResult], None] | None = None,
        agent_errors: dict[str, str] | None = None,
        cancel_event: Any | None = None,
    ) -> PipelineResult | MarketPipelineResult:
        _raise_if_cancelled(cancel_event)
        completed = completed_stages or {}

        def notify(stage: StageResult) -> StageResult:
            if stage_callback is not None:
                stage_callback(stage)
            return stage

        team = self.catalog.team(team_ref)
        normalized_findings = {str(VersionRef.parse(key)): value for key, value in findings.items()}
        if team.scope == ResearchScope.market:
            return self.market_pipeline.run(
                team.team,
                snapshot,
                normalized_findings,
                policy,
                agent_errors=agent_errors,
            )
        finding_stage, finding_evidence = self._finding_quality_gate(team, snapshot, normalized_findings)
        finding_stage = completed.get("finding_quality", finding_stage)
        stages: dict[str, StageResult] = {"finding_quality": finding_stage}
        if "finding_quality" not in completed:
            notify(finding_stage)
        finding_context = {"findings@1": {key: value.model_dump(mode="json") for key, value in sorted(normalized_findings.items())}}

        reviews = self._run_parallel(
            ("bull_review", "bear_review"),
            lambda stage: self._run_stage(
                stage,
                snapshot,
                policy,
                finding_context,
                StageReview,
                allowed_evidence=finding_evidence,
                cancel_event=cancel_event,
            ),
            max_workers=policy.max_stage_concurrency,
            completed_stages=completed,
            stage_callback=notify,
            cancel_event=cancel_event,
        )
        stages.update(reviews)
        review_context = {
            "findings@1": finding_context["findings@1"],
            "bull_review@1": reviews["bull_review"].output.model_dump(mode="json"),
            "bear_review@1": reviews["bear_review"].output.model_dump(mode="json"),
        }
        manager = completed.get("research_manager") or self._run_stage(
            "research_manager",
            snapshot,
            policy,
            review_context,
            StageReview,
            allowed_evidence=finding_evidence | _evidence_ids(reviews["bull_review"].output) | _evidence_ids(reviews["bear_review"].output),
            cancel_event=cancel_event,
        )
        if "research_manager" not in completed:
            notify(manager)
        stages["research_manager"] = manager
        trader = completed.get("trader") or self._run_stage(
            "trader",
            snapshot,
            policy,
            {"research_manager@1": manager.output.model_dump(mode="json")},
            TraderProposal,
            allowed_evidence=_evidence_ids(manager.output),
            cancel_event=cancel_event,
        )
        if "trader" not in completed:
            notify(trader)
        stages["trader"] = trader
        trader_context = {"trader@1": trader.output.model_dump(mode="json")}
        risk_results = self._run_parallel(
            ("aggressive_risk", "neutral_risk", "conservative_risk"),
            lambda stage: self._run_stage(
                stage,
                snapshot,
                policy,
                trader_context,
                StageReview,
                allowed_evidence=_evidence_ids(trader.output),
                cancel_event=cancel_event,
            ),
            max_workers=policy.max_stage_concurrency,
            completed_stages=completed,
            stage_callback=notify,
            cancel_event=cancel_event,
        )
        stages.update(risk_results)
        portfolio_inputs = {
            "trader@1": trader.output.model_dump(mode="json"),
            **{f"{stage}@1": result.output.model_dump(mode="json") for stage, result in sorted(risk_results.items())},
        }
        portfolio = completed.get("portfolio_manager") or self._run_stage(
            "portfolio_manager",
            snapshot,
            policy,
            portfolio_inputs,
            TeamConclusion,
            allowed_evidence=(
                _evidence_ids(trader.output)
                | {item for result in risk_results.values() for item in _evidence_ids(result.output)}
            ),
            team=team.team,
            cancel_event=cancel_event,
        )
        if "portfolio_manager" not in completed:
            notify(portfolio)
        stages["portfolio_manager"] = portfolio
        conclusion = portfolio.output
        self._publication_gate(
            conclusion,
            team.team,
            snapshot,
            _allowed_from_stages({key: value for key, value in stages.items() if key != "portfolio_manager"}),
        )
        stages["publication_quality"] = completed.get("publication_quality") or StageResult(
            "publication_quality",
            FindingQuality(status="passed", checks=("schema", "evidence", "boundary", "no-execution"), limitations=()),
            content_hash(conclusion),
            None,
            content_hash({"stage": "publication_quality", "conclusion": conclusion.model_dump(mode="json")}),
        )
        if "publication_quality" not in completed:
            notify(stages["publication_quality"])
        return PipelineResult(team.team, snapshot.subject, snapshot.boundary, conclusion, stages, self.pipeline.pipeline)

    def _run_parallel(
        self,
        stages: Iterable[str],
        runner,
        *,
        max_workers: int,
        completed_stages: dict[str, StageResult] | None = None,
        stage_callback: Callable[[StageResult], None] | None = None,
        cancel_event: Any | None = None,
    ) -> dict[str, StageResult]:
        stage_names = tuple(stages)
        completed = completed_stages or {}
        results: dict[str, StageResult] = {
            stage: completed[stage] for stage in stage_names if stage in completed
        }
        pending_stages = tuple(stage for stage in stage_names if stage not in results)
        if not pending_stages:
            return {stage: results[stage] for stage in stage_names}
        _raise_if_cancelled(cancel_event)
        max_workers = max(1, min(len(pending_stages), max_workers))
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="a-hunter-stage") as pool:
            futures = {pool.submit(runner, stage): stage for stage in pending_stages}
            try:
                for future in as_completed(futures):
                    _raise_if_cancelled(cancel_event)
                    stage = futures[future]
                    results[stage] = future.result()
                    if stage_callback is not None:
                        stage_callback(results[stage])
            except Exception as error:
                for future in futures:
                    future.cancel()
                if isinstance(error, DecisionPipelineCancelled):
                    raise
                if isinstance(error, TeamBlocked):
                    raise
                raise TeamBlocked(f"Decision Stage {futures.get(future)} failed", stage=futures.get(future)) from error
        return {stage: results[stage] for stage in stage_names}

    @staticmethod
    def _validate_fixed_pipeline(pipeline: DecisionPipelineManifest) -> None:
        if pipeline.stages != FIXED_STAGES or pipeline.parallel_groups != PEER_GROUPS:
            raise ValueError("Decision Pipeline must use the fixed A Hunter topology")

    def _finding_quality_gate(
        self,
        team,
        snapshot: SnapshotResult,
        findings: dict[str, ResearchFinding],
    ) -> tuple[StageResult, set[str]]:
        required = {str(ref) for ref in team.agents}
        missing = required - set(findings)
        if missing:
            raise TeamBlocked(f"missing required Findings: {', '.join(sorted(missing))}", stage="finding_quality")
        if set(findings) - required:
            raise TeamBlocked("Finding input contains an Agent outside the selected Team", stage="finding_quality")
        evidence_ids: set[str] = set()
        for agent_ref, finding in findings.items():
            if finding.agent != VersionRef.parse(agent_ref) or finding.subject != snapshot.subject or finding.boundary != snapshot.boundary:
                raise TeamBlocked(f"Finding quality failed for {agent_ref}", stage="finding_quality")
            if finding.quality.status == "blocked":
                raise TeamBlocked(f"Finding quality blocked for {agent_ref}", stage="finding_quality")
            evidence_ids.update(item.evidence_id for item in finding.evidence)
            if any(item.observed_at is not None and item.observed_at > snapshot.boundary.as_of for item in finding.evidence):
                raise TeamBlocked(f"Finding contains future evidence for {agent_ref}", stage="finding_quality")
        output = FindingQuality(status="passed", checks=("required-agents", "evidence", "boundary"), limitations=())
        finding_material = {key: value.model_dump(mode="json") for key, value in sorted(findings.items())}
        return StageResult("finding_quality", output, content_hash(finding_material), None, content_hash(output)), evidence_ids

    def _run_stage(
        self,
        stage: str,
        snapshot: SnapshotResult,
        policy: ExecutionPolicy,
        inputs: dict[str, Any],
        model,
        *,
        allowed_evidence: set[str],
        team: VersionRef | None = None,
        cancel_event: Any | None = None,
    ) -> StageResult:
        _raise_if_cancelled(cancel_event)
        output_schema = _stage_output_schema(model, stage, snapshot, team)
        capsule = build_capsule(
            label=f"decision-{stage}",
            instructions=f"{_stage_instructions(stage)}\n\n{PLAIN_CHINESE_PROMPT}",
            subject=snapshot.subject,
            boundary=snapshot.boundary,
            inputs=inputs,
            evidence_index={"allowed_evidence": sorted(allowed_evidence)},
            context={"team": str(team)} if team is not None else None,
            output_schema=output_schema,
            query_budget=0,
            max_result_rows=1,
            artifact_store=self.artifact_store,
        )
        codex_result: CodexResult[Any] | None = None
        capsule_hash = capsule.manifest_hash or capsule.input_hash
        identity_instruction = (
            f'"team" 字段必须原样返回 "{team}"，"subject.code" 字段必须原样返回 '
            f'"{snapshot.subject.code}"，"boundary.as_of" 字段必须原样返回 '
            f'"{snapshot.boundary.as_of.isoformat()}"。'
            if model is TeamConclusion
            else f'"stage" 字段必须原样返回 "{stage}"。'
        )
        try:
            codex_result = self.executor.execute(
                capsule,
                policy,
                prompt=(
                    f"执行固定决策阶段 {stage}。只能读取本 Capsule 明确提供的上游产物。"
                    "如果同级输出没有明确列在 Capsule 中，就不能读取或使用。"
                    + identity_instruction
                    + "只能使用这些上游产物中已经出现的 evidence_id。"
                    + PLAIN_CHINESE_PROMPT
                ),
                validator=lambda value: _validate_stage_model(model, value),
                cancel_event=cancel_event,
            )
            _raise_if_cancelled(cancel_event)
            output = codex_result.output if isinstance(codex_result.output, model) else model.model_validate(codex_result.output)
            self._validate_stage_output(stage, output, snapshot, allowed_evidence, team=team)
            output_hash = codex_result.output_hash
            if self.artifact_store is not None:
                output_hash = self.artifact_store.put_json(
                    output.model_dump(mode="json"),
                    media_type="application/vnd.a-hunter.decision-stage+json",
                ).content_hash
            return StageResult(
                stage,
                output,
                capsule.input_hash,
                capsule_hash,
                output_hash,
                codex_result.attempts,
                getattr(codex_result, "cli_version", None),
                getattr(codex_result, "model", None),
                getattr(codex_result, "reasoning_effort", None),
                getattr(codex_result, "usage", None),
            )
        except TeamBlocked as error:
            raise TeamBlocked(
                str(error),
                stage=error.stage or stage,
                attempts=tuple(getattr(error, "attempts", ())) or tuple(getattr(codex_result, "attempts", ())),
                capsule_hash=capsule_hash,
                error_class=getattr(error, "error_class", None) or type(error).__name__,
            ) from error
        except Exception as error:
            if _cancelled(cancel_event) or getattr(error, "kind", None) == "cancelled":
                raise DecisionPipelineCancelled() from error
            error_kind = getattr(error, "kind", None) or type(error).__name__
            raise TeamBlocked(
                f"Decision Stage {stage} failed: {str(error)[:200]}",
                stage=stage,
                attempts=tuple(getattr(error, "attempts", ())) or tuple(getattr(codex_result, "attempts", ())),
                capsule_hash=capsule_hash,
                error_class=error_kind,
            ) from error
        finally:
            capsule.cleanup()

    @staticmethod
    def _validate_stage_output(stage: str, output: Any, snapshot: SnapshotResult, allowed_evidence: set[str], *, team: VersionRef | None) -> None:
        try:
            validate_decision_output_language(output)
        except ValueError as error:
            raise TeamBlocked(str(error), stage=stage) from error
        if isinstance(output, StageReview):
            if output.stage != stage:
                raise TeamBlocked(f"Stage output identity mismatch for {stage}", stage=stage)
            if output.quality.status == "blocked":
                raise TeamBlocked(f"Stage quality blocked for {stage}", stage=stage)
            evidence = output.evidence
        elif isinstance(output, TraderProposal):
            if output.stage != stage or output.quality.status == "blocked":
                raise TeamBlocked(f"Trader output quality failed", stage=stage)
            evidence = output.evidence
        elif isinstance(output, TeamConclusion):
            if team is None or output.team != team or output.subject != snapshot.subject or output.boundary != snapshot.boundary:
                raise TeamBlocked("Portfolio Manager output boundary failed", stage=stage)
            evidence = output.evidence
            if output.evidence_quality.status == "blocked":
                raise TeamBlocked("Portfolio evidence quality is blocked", stage=stage)
            if _contains_key(output.model_dump(mode="json"), "confidence"):
                raise TeamBlocked("numeric confidence is forbidden", stage=stage)
        else:
            raise TeamBlocked(f"unsupported output for {stage}", stage=stage)
        unknown = {item.evidence_id for item in evidence} - allowed_evidence
        if unknown:
            raise TeamBlocked(f"Stage {stage} references unavailable evidence", stage=stage)
        if any(item.observed_at is not None and item.observed_at > snapshot.boundary.as_of for item in evidence):
            raise TeamBlocked(f"Stage {stage} contains future evidence", stage=stage)

    @staticmethod
    def _publication_gate(conclusion: TeamConclusion, team: VersionRef, snapshot: SnapshotResult, allowed_evidence: set[str]) -> None:
        DecisionPipeline._validate_stage_output("portfolio_manager", conclusion, snapshot, allowed_evidence, team=team)
        if not conclusion.thesis.strip() or not conclusion.evidence:
            raise TeamBlocked("publication quality requires an evidence-linked thesis", stage="publication_quality")


def _stage_instructions(stage: str) -> str:
    return {
        "bull_review": "在证据范围内形成最有力的看多观点。不要读取看空审查结果。",
        "bear_review": "在证据范围内形成最有力的看空观点。不要读取看多审查结果。",
        "research_manager": "独立权衡看多与看空观点，形成清晰、易懂的研究判断。",
        "trader": "把研究判断转成定性的观察方案，不生成或执行交易指令。",
        "aggressive_risk": "从进取型风险偏好出发，检查观察方案可能忽略的风险。",
        "neutral_risk": "从中性风险偏好出发，检查观察方案的收益与风险是否平衡。",
        "conservative_risk": "从保守型风险偏好出发，检查观察方案的下行风险。",
        "portfolio_manager": "综合观察方案和三份独立风险审查，形成该团队唯一的最终结论。",
    }.get(stage, stage)


def _cancelled(cancel_event: Any | None) -> bool:
    return bool(cancel_event is not None and callable(getattr(cancel_event, "is_set", None)) and cancel_event.is_set())


def _raise_if_cancelled(cancel_event: Any | None) -> None:
    if _cancelled(cancel_event):
        raise DecisionPipelineCancelled()


def _validate_stage_model(model: type[Any], value: Any) -> Any:
    output = model.model_validate(value)
    validate_decision_output_language(output)
    return output


def _stage_output_schema(model: type[Any], stage: str, snapshot: SnapshotResult, team: VersionRef | None) -> dict[str, Any]:
    schema = model.model_json_schema()
    properties = schema.setdefault("properties", {})
    if "price_range" in properties:
        properties["price_range"] = _price_range_schema()
    if model is not TeamConclusion:
        properties["stage"] = {"type": "string", "enum": [stage]}
        return schema
    if team is None:
        raise ValueError("portfolio_manager requires a Team")
    properties["team"] = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "enum": [team.id]},
            "version": {"type": "integer", "enum": [team.version]},
        },
        "required": ["id", "version"],
        "additionalProperties": False,
    }
    subject_name = snapshot.subject.name
    properties["subject"] = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "enum": [snapshot.subject.code]},
            "name": (
                {"type": "null", "enum": [None]}
                if subject_name is None
                else {"type": "string", "enum": [subject_name]}
            ),
        },
        "required": ["code", "name"],
        "additionalProperties": False,
    }
    properties["boundary"] = {
        "type": "object",
        "properties": {
            "as_of": {"type": "string", "enum": [snapshot.boundary.as_of.isoformat()]},
        },
        "required": ["as_of"],
        "additionalProperties": False,
    }
    return schema


def _price_range_schema() -> dict[str, Any]:
    return {
        "anyOf": [
            {
                "type": "object",
                "properties": {
                    "low": {"type": "number"},
                    "high": {"type": "number"},
                },
                "required": ["high", "low"],
                "additionalProperties": False,
            },
            {"type": "null"},
        ]
    }


def _evidence_ids(value: Any) -> set[str]:
    if isinstance(value, (StageReview, TraderProposal, TeamConclusion)):
        return {item.evidence_id for item in value.evidence}
    return set()


def _allowed_from_stages(stages: dict[str, StageResult]) -> set[str]:
    return {evidence_id for result in stages.values() for evidence_id in _evidence_ids(result.output)}


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(child, key) for child in value.values())
    if isinstance(value, list):
        return any(_contains_key(child, key) for child in value)
    return False
