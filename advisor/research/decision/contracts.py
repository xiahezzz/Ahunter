from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from advisor.research.contracts import (
    ContractModel,
    DecisionReview,
    EvidenceRef,
    FindingClaim,
    FindingQuality,
    ResearchBoundary,
    ResearchSubject,
    TeamConclusion,
    VersionRef,
)


class StageReview(ContractModel):
    stage: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=12000)
    claims: tuple[FindingClaim, ...] = Field(default_factory=tuple, max_length=50)
    evidence: tuple[EvidenceRef, ...] = Field(default_factory=tuple, max_length=200)
    risks: tuple[str, ...] = Field(default_factory=tuple, max_length=30)
    invalidation_conditions: tuple[str, ...] = Field(default_factory=tuple, max_length=30)
    quality: FindingQuality
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_claim_evidence(self) -> StageReview:
        known = {item.evidence_id for item in self.evidence}
        if any(evidence_id not in known for claim in self.claims for evidence_id in claim.evidence_ids):
            raise ValueError("every stage claim evidence_id must refer to stage evidence")
        return self


class TraderProposal(ContractModel):
    stage: str = Field(min_length=1, max_length=80)
    stance: str = Field(min_length=1, max_length=40)
    conviction: str = Field(min_length=1, max_length=20)
    thesis: str = Field(min_length=1, max_length=16000)
    evidence: tuple[EvidenceRef, ...] = Field(default_factory=tuple, max_length=300)
    key_risks: tuple[str, ...] = Field(default_factory=tuple, max_length=50)
    invalidation_conditions: tuple[str, ...] = Field(default_factory=tuple, max_length=50)
    time_horizon: str = Field(min_length=1, max_length=160)
    price_range: dict[str, float] | None = None
    position_limit: str | None = Field(default=None, max_length=200)
    quality: FindingQuality


class ResearchCandidate(ContractModel):
    """A future-facing Market candidate, deliberately not a trade proposal.

    The generic type is kept separate from the fixed Overview Team contract.
    It can point to a security and disclose how it was selected, but cannot
    smuggle in stance, price, position, or a child-request instruction because
    every field outside this bounded shape is forbidden by ``ContractModel``.
    """

    code: str = Field(min_length=6, max_length=6)
    selection_method: str = Field(min_length=1, max_length=500, alias="method")
    rationale: str = Field(min_length=1, max_length=12000)
    evidence: tuple[EvidenceRef, ...] = Field(min_length=1, max_length=200)
    risks: tuple[str, ...] = Field(min_length=1, max_length=50)
    rank: int | None = Field(default=None, ge=1, le=1000000)

    @field_validator("code")
    @classmethod
    def validate_candidate_code(cls, value: str) -> str:
        # Reuse the single canonical A-share validator; this also rejects
        # synthetic codes and CDRs.
        return ResearchSubject(code=value).code  # type: ignore[return-value]

    @model_validator(mode="after")
    def validate_candidate_text(self) -> "ResearchCandidate":
        if any(not isinstance(item, str) or not item.strip() for item in self.risks):
            raise ValueError("Research Candidate risks must be non-empty")
        return self

    @property
    def method(self) -> str:
        """Read-compatible spelling for callers using the concise field name."""
        return self.selection_method


class MarketInsight(ContractModel):
    """One independently publishable Market insight or a bounded block reason."""

    insight_id: Literal["breadth_sentiment", "sector_rotation", "macro_policy"]
    agent: VersionRef
    status: Literal["passed", "blocked"]
    summary: str | None = Field(default=None, min_length=1, max_length=12000)
    claims: tuple[FindingClaim, ...] = Field(default_factory=tuple, max_length=50)
    evidence: tuple[EvidenceRef, ...] = Field(default_factory=tuple, max_length=200)
    risks: tuple[str, ...] = Field(default_factory=tuple, max_length=30)
    invalidation_conditions: tuple[str, ...] = Field(default_factory=tuple, max_length=30)
    method: dict[str, Any] | None = None
    reason_code: Literal[
        "taxonomy_stale", "information_unavailable", "snapshot_unavailable",
        "agent_timeout", "agent_invalid", "quality_blocked", "pipeline_blocked",
    ] | None = None

    @model_validator(mode="after")
    def validate_partial_shape(self) -> "MarketInsight":
        evidence_ids = {item.evidence_id for item in self.evidence}
        if any(item not in evidence_ids for claim in self.claims for item in claim.evidence_ids):
            raise ValueError("Market Insight claim evidence must be declared")
        if self.status == "passed":
            if self.summary is None or self.method is None or not self.evidence or self.reason_code is not None:
                raise ValueError("passed Market Insight requires summary, method, and evidence only")
        elif self.summary is not None or self.claims or self.evidence or self.method is not None or self.reason_code is None:
            raise ValueError("blocked Market Insight must contain only a bounded reason")
        return self


class MarketTeamReport(ContractModel):
    team: VersionRef
    subject: ResearchSubject
    boundary: ResearchBoundary
    status: Literal["passed", "partial"]
    insights: tuple[MarketInsight, ...] = Field(min_length=1, max_length=20)
    evidence_quality: FindingQuality
    time_horizon: str = Field(min_length=1, max_length=240)
    risks: tuple[str, ...] = Field(default_factory=tuple, max_length=50)
    invalidation_conditions: tuple[str, ...] = Field(default_factory=tuple, max_length=50)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_market_report(self) -> "MarketTeamReport":
        if not self.subject.is_market:
            raise ValueError("Market Team Report requires a Market Subject")
        ids = [item.insight_id for item in self.insights]
        if len(ids) != len(set(ids)):
            raise ValueError("Market Team Report Insights must be unique")
        passed = sum(item.status == "passed" for item in self.insights)
        blocked = len(self.insights) - passed
        if passed == 0:
            raise ValueError("blocked Market Team must not publish a report")
        if (self.status == "passed") != (blocked == 0):
            raise ValueError("Market Team Report status does not match Insights")
        return self


__all__ = [
    "DecisionReview", "MarketInsight", "MarketTeamReport", "ResearchCandidate", "StageReview", "TeamConclusion", "TraderProposal",
]
