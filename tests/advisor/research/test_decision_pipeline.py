from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time

import pytest

from advisor.research.catalog import ManifestCatalog
from advisor.research.codex.executor import CodexExecutionError, CodexResult, CodexAttempt
from advisor.research.contracts import (
    AgentManifest,
    DataProductManifest,
    ExecutionPolicy,
    EvidenceRef,
    FindingQuality,
    ResearchBoundary,
    ResearchFinding,
    ResearchSubject,
    ResearchTeam,
)
from advisor.research.data_products.engine import SnapshotResult
from advisor.research.decision.pipeline import TeamBlocked, DecisionPipeline


def _catalog() -> ManifestCatalog:
    return ManifestCatalog(
        products={"bars@1": DataProductManifest(product="bars@1", title="Bars", providers=("fixture",))},
        agents={
            "market@1": AgentManifest(
                agent="market@1",
                title="Market",
                instructions="inspect",
                required_products=("bars@1",),
            )
        },
        teams={"core@1": ResearchTeam(team="core@1", title="Core", agents=("market@1",))},
        pipelines={
            "decision@1": __import__("advisor.research.contracts", fromlist=["DecisionPipelineManifest"]).DecisionPipelineManifest(
                pipeline="decision@1",
                stages=(
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
                ),
                parallel_groups=(
                    ("bull_review", "bear_review"),
                    ("aggressive_risk", "neutral_risk", "conservative_risk"),
                ),
            )
        },
        execution_policies={},
    ).validate()


def _policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        policy="codex@1",
        model="gpt-test",
        reasoning_effort="medium",
        timeout_seconds=30,
        max_agent_concurrency=2,
        max_stage_concurrency=3,
    )


def _finding() -> ResearchFinding:
    subject = ResearchSubject(code="600519")
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))
    evidence = EvidenceRef(
        evidence_id="product:bars@1:fixture",
        source="fixture",
        locator="artifact://bars",
        observed_at=boundary.as_of,
        excerpt="close=10",
    )
    return ResearchFinding(
        agent={"id": "market", "version": 1},
        subject=subject,
        boundary=boundary,
        summary="Fixture finding",
        claims=(),
        evidence=(evidence,),
        risks=("fixture risk",),
        invalidation_conditions=("close below reference",),
        quality={"status": "passed", "checks": ("fixture",), "limitations": ()},
        details={},
    )


def _snapshot() -> SnapshotResult:
    from advisor.research.data_products.engine import ProductResult
    from advisor.research.contracts import VersionRef

    subject = ResearchSubject(code="600519")
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))
    product = ProductResult(VersionRef.parse("bars@1"), {"rows": [{"close": 10}]}, "a" * 64, "fixture", "passed", None, ())
    return SnapshotResult("snapshot-1", subject, boundary, {"bars@1": product}, "b" * 64)


class FakeExecutor:
    def __init__(self, fail_stage: str | None = None):
        self.fail_stage = fail_stage
        self.calls: list[tuple[str, object]] = []
        self.prompts: list[str] = []
        self.schemas: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def execute(self, capsule, policy, *, validator=None, **kwargs):
        manifest = json.loads(capsule.manifest_path.read_text(encoding="utf-8"))
        stage = manifest["label"].removeprefix("decision-")
        self.prompts.append(kwargs.get("prompt", ""))
        self.schemas[stage] = json.loads(capsule.root.joinpath("output-schema.json").read_text(encoding="utf-8"))
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append((stage, capsule))
        try:
            if stage == self.fail_stage:
                raise RuntimeError("fixture stage failed")
            if stage in {"bull_review", "bear_review", "research_manager", "aggressive_risk", "neutral_risk", "conservative_risk"}:
                if stage in {"bull_review", "bear_review", "aggressive_risk", "neutral_risk", "conservative_risk"}:
                    time.sleep(0.02)
                output = {
                    "stage": stage,
                    "summary": f"{stage} 阶段认为当前证据需要谨慎解读。",
                    "claims": [],
                    "evidence": [
                        {
                            "evidence_id": "product:bars@1:fixture",
                            "source": "fixture",
                            "locator": "artifact://bars",
                            "observed_at": "2026-08-06T08:30:00+00:00",
                            "excerpt": "最新收盘价为10元。",
                        }
                    ],
                    "risks": ["样例数据可能不完整。"],
                    "invalidation_conditions": ["后续证据与当前判断相反。"],
                    "quality": {"status": "passed", "checks": [], "limitations": []},
                    "details": {},
                }
            elif stage == "trader":
                output = {
                    "stage": "trader",
                    "stance": "hold",
                    "conviction": "medium",
                    "thesis": "当前证据支持继续观察，暂不采取行动。",
                    "evidence": [{"evidence_id": "product:bars@1:fixture", "source": "fixture", "observed_at": "2026-08-06T08:30:00+00:00"}],
                    "key_risks": ["样例数据可能不完整。"],
                    "invalidation_conditions": ["后续证据与当前判断相反。"],
                    "time_horizon": "未来一到十个交易日",
                    "price_range": None,
                    "position_limit": "仅保持较小的观察仓位。",
                    "quality": {"status": "passed", "checks": [], "limitations": []},
                }
            elif stage == "portfolio_manager":
                output = {
                    "team": {"id": "core", "version": 1},
                    "subject": {"code": "600519", "name": None},
                    "boundary": {"as_of": "2026-08-06T08:30:00+00:00"},
                    "stance": "hold",
                    "conviction": "medium",
                    "evidence_quality": {"status": "passed", "checks": [], "limitations": []},
                    "thesis": "当前证据支持继续观察，暂不采取行动。",
                    "evidence": [{"evidence_id": "product:bars@1:fixture", "source": "fixture", "observed_at": "2026-08-06T08:30:00+00:00"}],
                    "key_risks": ["样例数据可能不完整。"],
                    "invalidation_conditions": ["后续证据与当前判断相反。"],
                    "time_horizon": "未来一到十个交易日",
                    "price_range": None,
                    "position_limit": "仅保持较小的观察仓位。",
                }
            else:
                output = {}
            parsed = validator(output) if validator else output
            return CodexResult(parsed, "c" * 64, "fake-1", 1, ())
        finally:
            with self._lock:
                self.active -= 1


class FailingCodexExecutor(FakeExecutor):
    def execute(self, capsule, policy, *, validator=None, **kwargs):
        raise CodexExecutionError(
            "Codex execution timed out",
            kind="timeout",
            retryable=False,
            attempts=(
                CodexAttempt(
                    attempt_number=1,
                    status="timeout",
                    duration_ms=30,
                    error_class="timeout",
                    model=policy.model,
                    reasoning_effort=policy.reasoning_effort,
                ),
            ),
        )


def test_fixed_pipeline_keeps_peer_inputs_identical_and_separate():
    executor = FakeExecutor()
    pipeline = DecisionPipeline(_catalog(), executor)
    result = pipeline.run("core@1", _snapshot(), {"market@1": _finding()}, _policy())

    assert result.conclusion.stance == "hold"
    assert "confidence" not in result.conclusion.model_dump()
    peer_capsules = {stage: capsule for stage, capsule in executor.calls if stage in {"bull_review", "bear_review"}}
    assert peer_capsules["bull_review"].input_hash == peer_capsules["bear_review"].input_hash
    assert peer_capsules["bull_review"].declared_products == peer_capsules["bear_review"].declared_products == ("findings@1",)
    assert executor.max_active >= 2
    assert not (peer_capsules["bull_review"].root / "products" / "bear_review__1.json").exists()
    assert any('"stage" 字段必须原样返回 "bull_review"' in prompt for prompt in executor.prompts)
    assert all("所有面向读者的文本必须使用简体中文" in prompt for prompt in executor.prompts)
    assert all("使用短句和日常表达" in prompt for prompt in executor.prompts)
    assert executor.schemas["bull_review"]["properties"]["stage"]["enum"] == ["bull_review"]
    assert executor.schemas["portfolio_manager"]["properties"]["team"]["properties"]["id"]["enum"] == ["core"]
    price_range = executor.schemas["portfolio_manager"]["properties"]["price_range"]
    price_object = next(option for option in price_range["anyOf"] if option.get("type") == "object")
    assert price_object["required"] == ["high", "low"]
    assert price_object["additionalProperties"] is False


def test_pipeline_failure_blocks_only_current_team():
    executor = FakeExecutor(fail_stage="bear_review")
    pipeline = DecisionPipeline(_catalog(), executor)
    with pytest.raises(TeamBlocked, match="bear_review"):
        pipeline.run("core@1", _snapshot(), {"market@1": _finding()}, _policy())


def test_pipeline_failure_preserves_codex_attempts_and_error_kind():
    pipeline = DecisionPipeline(_catalog(), FailingCodexExecutor())

    with pytest.raises(TeamBlocked) as captured:
        pipeline.run("core@1", _snapshot(), {"market@1": _finding()}, _policy())

    error = captured.value
    assert error.stage in {"bull_review", "bear_review"}
    assert error.error_class == "timeout"
    assert len(error.attempts) == 1
    assert error.attempts[0].status == "timeout"


def test_stage_review_claims_cannot_hide_unlinked_evidence():
    with pytest.raises(ValueError, match="stage claim evidence"):
        from advisor.research.decision.contracts import StageReview

        StageReview.model_validate(
            {
                "stage": "bull_review",
                "summary": "fixture",
                "claims": [{"claim_id": "x", "statement": "fixture", "evidence_ids": ["missing"]}],
                "evidence": [],
                "quality": {"status": "passed"},
            }
        )
