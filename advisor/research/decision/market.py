"""Fixed typed Market Pipeline with explicit partial-report semantics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from advisor.research.catalog import ManifestCatalog
from advisor.research.contracts import ExecutionPolicy, FindingQuality, ResearchFinding, ResearchScope, VersionRef, content_hash
from advisor.research.data_products.engine import SnapshotResult
from advisor.research.decision.contracts import MarketInsight, MarketTeamReport
from advisor.research.market_safety import (
    contains_unsafe_market_output,
    snapshot_security_names,
)


_OVERVIEW_AGENTS = (
    "market_breadth@1",
    "sector_rotation@1",
    "market_macro_policy@1",
)
_INSIGHT_IDS = {
    "market_breadth@1": "breadth_sentiment",
    "sector_rotation@1": "sector_rotation",
    "market_macro_policy@1": "macro_policy",
}
_REQUIRED_PRODUCTS = {
    "market_breadth@1": ("whole_market_intraday_snapshot@1", "whole_market_daily_history@1"),
    "sector_rotation@1": (
        "whole_market_intraday_snapshot@1",
        "whole_market_daily_history@1",
        "industry_sector_taxonomy@1",
    ),
    "market_macro_policy@1": ("whole_market_intraday_snapshot@1", "market_information@1"),
}
class MarketTeamBlocked(RuntimeError):
    def __init__(self, message: str, *, reason_code: str = "pipeline_blocked") -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class MarketPipelineResult:
    team: VersionRef
    subject: Any
    boundary: Any
    report: MarketTeamReport | None
    status: str
    blocked_insights: tuple[MarketInsight, ...]
    pipeline_ref: str = "market-overview-fixed@1"

    @property
    def conclusion(self) -> MarketTeamReport | None:
        # Reuse the generic TeamRunResult transport without giving a Market
        # report a Security stance/conclusion schema.
        return self.report


class MarketDecisionPipeline:
    """The Market topology is code-owned, not Manifest-configurable."""

    def __init__(self, catalog: ManifestCatalog) -> None:
        self.catalog = catalog

    def run(
        self,
        team_ref: str | VersionRef,
        snapshot: SnapshotResult,
        findings: dict[str, ResearchFinding],
        policy: ExecutionPolicy,
        *,
        agent_errors: dict[str, str] | None = None,
        **_ignored: Any,
    ) -> MarketPipelineResult:
        team = self.catalog.team(team_ref)
        if team.scope != ResearchScope.market or not snapshot.subject.is_market:
            raise MarketTeamBlocked("Market Pipeline Scope is invalid")
        agent_refs = tuple(str(item) for item in team.agents)
        if not agent_refs or any(agent_ref not in _INSIGHT_IDS for agent_ref in agent_refs):
            raise MarketTeamBlocked("Market Team contains an unsupported fixed Insight Agent")
        insight_ids = [_INSIGHT_IDS[agent_ref] for agent_ref in agent_refs]
        if len(insight_ids) != len(set(insight_ids)):
            raise MarketTeamBlocked("Market Team contains duplicate Insight kinds")
        # The Pipeline remains code-owned and fixed: a Team can only compose
        # the published typed Insight Agents, never supply custom stages or a
        # bespoke report schema.  Keeping membership flexible lets these
        # shared Market Agents be reused by a future compatible Team.
        errors = agent_errors or {}
        security_names = snapshot_security_names(snapshot)
        insights: list[MarketInsight] = []
        for agent_ref in agent_refs:
            snapshot_reason = _required_product_reason(agent_ref, snapshot)
            if snapshot_reason is not None:
                insights.append(MarketInsight(
                    insight_id=_INSIGHT_IDS[agent_ref],
                    agent=VersionRef.parse(agent_ref),
                    status="blocked",
                    reason_code=snapshot_reason,
                ))
                continue
            finding = findings.get(agent_ref)
            if finding is None:
                insights.append(MarketInsight(
                    insight_id=_INSIGHT_IDS[agent_ref],
                    agent=VersionRef.parse(agent_ref),
                    status="blocked",
                    reason_code=_reason_for(agent_ref, errors.get(agent_ref), snapshot),
                ))
                continue
            try:
                insights.append(_passed_insight(agent_ref, finding, snapshot, security_names=security_names))
            except Exception:
                insights.append(MarketInsight(
                    insight_id=_INSIGHT_IDS[agent_ref],
                    agent=VersionRef.parse(agent_ref),
                    status="blocked",
                    reason_code="agent_invalid",
                ))
        passed = tuple(item for item in insights if item.status == "passed")
        blocked = tuple(item for item in insights if item.status == "blocked")
        if not passed:
            return MarketPipelineResult(team.team, snapshot.subject, snapshot.boundary, None, "blocked", blocked)
        report = MarketTeamReport(
            team=team.team,
            subject=snapshot.subject,
            boundary=snapshot.boundary,
            status="passed" if not blocked else "partial",
            insights=tuple(insights),
            evidence_quality=FindingQuality(
                status="passed" if not blocked else "warning",
                checks=("fixed-market-pipeline", "evidence", "boundary"),
                limitations=tuple(f"{item.insight_id}:{item.reason_code}" for item in blocked),
            ),
            time_horizon="当前研究边界至下一个交易日的全市场观察窗口",
            risks=tuple(risk for item in passed for risk in item.risks)[:50],
            invalidation_conditions=tuple(condition for item in passed for condition in item.invalidation_conditions)[:50],
            provenance={
                "pipeline": "market-overview-fixed@1",
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_hash": snapshot.snapshot_hash,
                "agents": [str(item.agent) for item in insights],
                "insight_hash": content_hash([item.model_dump(mode="json") for item in insights]),
            },
        )
        _validate_market_report(report, security_names=security_names)
        return MarketPipelineResult(team.team, snapshot.subject, snapshot.boundary, report, report.status, blocked)


def _passed_insight(
    agent_ref: str,
    finding: ResearchFinding,
    snapshot: SnapshotResult,
    *,
    security_names: frozenset[str],
) -> MarketInsight:
    if finding.agent != VersionRef.parse(agent_ref) or finding.subject != snapshot.subject or finding.boundary != snapshot.boundary:
        raise MarketTeamBlocked("Market Finding identity is invalid", reason_code="agent_invalid")
    if finding.quality.status == "blocked" or not finding.evidence:
        raise MarketTeamBlocked("Market Finding quality is blocked", reason_code="quality_blocked")
    method = {
        key: finding.details[key]
        for key in ("inputs", "time_windows", "criteria", "grouping_or_ranking")
        if key in finding.details
    }
    required = {"inputs", "time_windows", "criteria"}
    if agent_ref == "sector_rotation@1":
        required.add("grouping_or_ranking")
    if not required.issubset(method) or any(not method[key] for key in required):
        raise MarketTeamBlocked("Market Finding lacks method disclosure", reason_code="agent_invalid")
    if contains_unsafe_market_output({
        "summary": finding.summary,
        "claims": [claim.model_dump(mode="json") for claim in finding.claims],
        "evidence": [item.model_dump(mode="json") for item in finding.evidence],
        "risks": finding.risks,
        "invalidation_conditions": finding.invalidation_conditions,
        "details": finding.details,
    }, security_names=security_names):
        raise MarketTeamBlocked("Market Finding contains a per-security recommendation", reason_code="agent_invalid")
    return MarketInsight(
        insight_id=_INSIGHT_IDS[agent_ref],
        agent=VersionRef.parse(agent_ref),
        status="passed",
        summary=finding.summary,
        claims=finding.claims,
        evidence=finding.evidence,
        risks=finding.risks,
        invalidation_conditions=finding.invalidation_conditions,
        method=method,
    )


def _reason_for(agent_ref: str, error: str | None, snapshot: SnapshotResult) -> str:
    unavailable = snapshot.unavailable
    needs = {
        "sector_rotation@1": "industry_sector_taxonomy@1",
        "market_macro_policy@1": "market_information@1",
    }.get(agent_ref)
    if needs and needs in unavailable:
        return "taxonomy_stale" if needs.startswith("industry_") else "information_unavailable"
    text = (error or "").lower()
    if "quality is blocked" in text or "quality_blocked" in text:
        return "quality_blocked"
    if "timeout" in text:
        return "agent_timeout"
    if "snapshot" in text or "product" in text:
        return "snapshot_unavailable"
    if "invalid" in text or "schema" in text:
        return "agent_invalid"
    return "quality_blocked"


def _required_product_reason(agent_ref: str, snapshot: SnapshotResult) -> str | None:
    """Enforce critical Market input quality in code, not in a prompt.

    A warning Product remains in a sealed Snapshot for audit and can still be
    useful to unrelated Agents.  It is not, however, sufficient evidence for
    an Insight whose fixed contract depends on that Product.  This prevents a
    model from publishing a sector or macro claim merely because it received a
    stale taxonomy or an empty information envelope.
    """
    for product_ref in _REQUIRED_PRODUCTS[agent_ref]:
        product = snapshot.products.get(product_ref)
        if product is not None and product.quality_status == "passed":
            payload = product.payload
            if product_ref == "industry_sector_taxonomy@1" and isinstance(payload, dict) and payload.get("stale_fallback"):
                return "taxonomy_stale"
            if product_ref == "market_information@1" and isinstance(payload, dict) and payload.get("status") in {
                "blocked", "empty", "unavailable", "warning",
            }:
                return "information_unavailable"
            continue
        if product_ref == "industry_sector_taxonomy@1":
            return "taxonomy_stale"
        if product_ref == "market_information@1":
            return "information_unavailable"
        return "snapshot_unavailable"
    return None


def _validate_market_report(report: MarketTeamReport, *, security_names: frozenset[str] = frozenset()) -> None:
    payload = report.model_dump(mode="json")
    if contains_unsafe_market_output(payload, security_names=security_names):
        raise MarketTeamBlocked("Market Report contains a per-security recommendation")


__all__ = ["MarketDecisionPipeline", "MarketPipelineResult", "MarketTeamBlocked"]
