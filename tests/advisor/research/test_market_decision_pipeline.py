from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from advisor.research.catalog import load_catalog
from advisor.research.contracts import EvidenceRef, ResearchBoundary, ResearchFinding, ResearchScope, ResearchSubject, VersionRef, content_hash
from advisor.research.data_products.engine import ProductResult, SnapshotResult
from advisor.research.decision.market import MarketDecisionPipeline


ROOT = Path(__file__).resolve().parents[3]
BOUNDARY = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))
TEAM = "a_share_market_overview@1"


def _product(ref: str, *, quality: str = "passed", payload: dict | None = None) -> ProductResult:
    return ProductResult(
        product=VersionRef.parse(ref),
        payload=payload or {"status": "passed"},
        artifact_hash=content_hash(ref),
        provider="fixture",
        quality_status=quality,
        quality_message=None,
        attempts=(),
    )


def _snapshot(*, unavailable: dict[str, str] | None = None, products: dict[str, ProductResult] | None = None) -> SnapshotResult:
    base_products = {
        "whole_market_intraday_snapshot@1": _product(
            "whole_market_intraday_snapshot@1",
            payload={
                "rows": [{"code": "600519", "name": "贵州茅台"}],
                "expected_universe": {
                    "securities": [
                        {"code": "600519", "name": "贵州茅台"},
                        {"code": "600030", "name": "中信证券"},
                    ],
                },
            },
        ),
        "whole_market_daily_history@1": _product("whole_market_daily_history@1"),
        "industry_sector_taxonomy@1": _product("industry_sector_taxonomy@1"),
        "market_information@1": _product("market_information@1"),
    }
    return SnapshotResult(
        snapshot_id="snapshot-market-fixture",
        subject=ResearchSubject(scope=ResearchScope.market),
        boundary=BOUNDARY,
        products=base_products if products is None else products,
        snapshot_hash="a" * 64,
        unavailable=unavailable or {},
    )


def _finding(agent_ref: str) -> ResearchFinding:
    details = {
        "inputs": ["固定快照"],
        "time_windows": ["当前边界"],
        "criteria": "只使用已封存的公开数据。",
    }
    if agent_ref == "sector_rotation@1":
        details["grouping_or_ranking"] = "按一级行业比较相对强弱。"
    return ResearchFinding(
        agent=agent_ref,
        subject=ResearchSubject(scope=ResearchScope.market),
        boundary=BOUNDARY,
        summary="固定市场快照显示观察结果仍需结合后续数据确认。",
        claims=(),
        evidence=(
            EvidenceRef(
                evidence_id=f"product:fixture:{agent_ref}",
                source="fixture",
                observed_at=BOUNDARY.as_of,
                excerpt="固定快照已通过边界校验。",
            ),
        ),
        risks=("固定样例覆盖范围有限。",),
        invalidation_conditions=("后续同边界数据与当前观察相反。",),
        quality={"status": "passed"},
        details=details,
    )


def _pipeline() -> MarketDecisionPipeline:
    return MarketDecisionPipeline(load_catalog(ROOT))


def test_fixed_market_pipeline_publishes_all_three_typed_insights_without_a_security_conclusion():
    findings = {
        agent: _finding(agent)
        for agent in ("market_breadth@1", "sector_rotation@1", "market_macro_policy@1")
    }

    result = _pipeline().run(TEAM, _snapshot(), findings, load_catalog(ROOT).execution_policy("codex@1"))

    assert result.status == "passed"
    assert result.report is not None and result.report.status == "passed"
    assert [item.insight_id for item in result.report.insights] == [
        "breadth_sentiment",
        "sector_rotation",
        "macro_policy",
    ]
    serialized = result.report.model_dump(mode="json")
    assert {"stance", "price_range", "position", "candidate"}.isdisjoint(serialized)


@pytest.mark.parametrize("source", ["东方财富一级行业分类", "看好东方财富"])
def test_source_attribution_is_not_correlated_with_unrelated_market_actions(source):
    snapshot = _snapshot()
    snapshot.products["whole_market_intraday_snapshot@1"].payload["rows"].append(
        {"code": "300059", "name": "东方财富"}
    )
    findings = {agent: _finding(agent) for agent in (
        "market_breadth@1", "sector_rotation@1", "market_macro_policy@1",
    )}
    breadth = findings["market_breadth@1"]
    findings["market_breadth@1"] = breadth.model_copy(update={"summary": "全市场上涨家数较多，仍需观察。"})
    sector = findings["sector_rotation@1"]
    findings["sector_rotation@1"] = sector.model_copy(update={
        "evidence": (sector.evidence[0].model_copy(update={"source": source}),),
    })
    result = _pipeline().run(TEAM, snapshot, findings, load_catalog(ROOT).execution_policy("codex@1"))
    assert result.report is not None
    assert result.status == ("passed" if source == "东方财富一级行业分类" else "partial")
    if source == "看好东方财富":
        assert [item.insight_id for item in result.blocked_insights] == ["sector_rotation"]


def test_fixed_market_pipeline_keeps_failed_members_as_bounded_partial_insights():
    findings = {
        "market_breadth@1": _finding("market_breadth@1"),
        "market_macro_policy@1": _finding("market_macro_policy@1"),
    }

    result = _pipeline().run(
        TEAM,
        _snapshot(),
        findings,
        load_catalog(ROOT).execution_policy("codex@1"),
        agent_errors={"sector_rotation@1": "fixture timeout"},
    )

    assert result.status == "partial"
    assert result.report is not None and result.report.status == "partial"
    blocked = [item for item in result.report.insights if item.status == "blocked"]
    assert [(item.insight_id, item.reason_code) for item in blocked] == [
        ("sector_rotation", "agent_timeout")
    ]
    assert result.report.evidence_quality.limitations == ("sector_rotation:agent_timeout",)


def test_fixed_market_pipeline_blocks_a_long_per_security_action_hidden_in_nested_method_details():
    findings = {
        agent: _finding(agent)
        for agent in ("market_breadth@1", "sector_rotation@1", "market_macro_policy@1")
    }
    breadth = findings["market_breadth@1"]
    findings["market_breadth@1"] = breadth.model_copy(update={
        "details": {
            **breadth.details,
            "criteria": {
                "reader_note": (
                    "600519 的名称仅用于说明异常样本；其后是一段超过旧距离阈值的市场方法说明，"
                    "包括分组、窗口、成交额和覆盖范围。建议买入该证券。"
                ),
            },
        },
    })

    result = _pipeline().run(TEAM, _snapshot(), findings, load_catalog(ROOT).execution_policy("codex@1"))

    assert result.status == "partial"
    assert result.report is not None
    blocked = [item for item in result.report.insights if item.status == "blocked"]
    assert [(item.insight_id, item.reason_code) for item in blocked] == [
        ("breadth_sentiment", "agent_invalid")
    ]


def test_fixed_market_pipeline_blocks_a_named_security_stance_from_the_sealed_universe():
    findings = {
        agent: _finding(agent)
        for agent in ("market_breadth@1", "sector_rotation@1", "market_macro_policy@1")
    }
    breadth = findings["market_breadth@1"]
    findings["market_breadth@1"] = breadth.model_copy(update={
        "details": {
            **breadth.details,
            "criteria": {"reader_note": "贵州茅台看多，适合继续持有。"},
        },
    })

    result = _pipeline().run(TEAM, _snapshot(), findings, load_catalog(ROOT).execution_policy("codex@1"))

    assert result.status == "partial"
    assert result.report is not None
    blocked = [item for item in result.report.insights if item.status == "blocked"]
    assert [(item.insight_id, item.reason_code) for item in blocked] == [
        ("breadth_sentiment", "agent_invalid")
    ]


def test_fixed_market_pipeline_blocks_a_named_security_stance_hidden_in_a_method_key():
    findings = {
        agent: _finding(agent)
        for agent in ("market_breadth@1", "sector_rotation@1", "market_macro_policy@1")
    }
    breadth = findings["market_breadth@1"]
    findings["market_breadth@1"] = breadth.model_copy(update={
        "details": {
            **breadth.details,
            "criteria": {"贵州茅台看多，适合继续持有。": "仅作为方法备注。"},
        },
    })

    result = _pipeline().run(TEAM, _snapshot(), findings, load_catalog(ROOT).execution_policy("codex@1"))

    assert result.status == "partial"
    assert result.report is not None
    blocked = [item for item in result.report.insights if item.status == "blocked"]
    assert [(item.insight_id, item.reason_code) for item in blocked] == [
        ("breadth_sentiment", "agent_invalid")
    ]


def test_fixed_market_pipeline_blocks_a_named_security_absent_from_quote_rows_but_in_sealed_universe():
    findings = {
        agent: _finding(agent)
        for agent in ("market_breadth@1", "sector_rotation@1", "market_macro_policy@1")
    }
    breadth = findings["market_breadth@1"]
    findings["market_breadth@1"] = breadth.model_copy(update={
        "details": {
            **breadth.details,
            "criteria": {"reader_note": "中信证券看多，适合继续持有。"},
        },
    })

    result = _pipeline().run(TEAM, _snapshot(), findings, load_catalog(ROOT).execution_policy("codex@1"))

    assert result.status == "partial"
    assert result.report is not None
    blocked = [item for item in result.report.insights if item.status == "blocked"]
    assert [(item.insight_id, item.reason_code) for item in blocked] == [
        ("breadth_sentiment", "agent_invalid")
    ]


@pytest.mark.parametrize(
    "entity,action",
    (
        ("贵州茅台", "值得买"),
        ("贵州茅台", "可以买"),
        ("贵州茅台", "适合做多"),
        ("贵州茅台", "低吸"),
        ("贵州茅台", "上车"),
        ("贵州茅台", "加大配置"),
        ("贵州茅台", "看好"),
        ("贵州茅台", "首选"),
        ("贵州茅台", "优选"),
        ("贵州茅台", "具备投资价值"),
        ("贵州茅台", "建议投资"),
        ("贵州茅台", "建议持仓"),
        ("贵州茅台", "值得投资"),
        ("SH600519", "值得买"),
    ),
)
def test_fixed_market_pipeline_blocks_common_named_security_recommendation_wording(entity: str, action: str):
    findings = {
        agent: _finding(agent)
        for agent in ("market_breadth@1", "sector_rotation@1", "market_macro_policy@1")
    }
    breadth = findings["market_breadth@1"]
    findings["market_breadth@1"] = breadth.model_copy(update={
        "details": {
            **breadth.details,
            "criteria": {"reader_note": f"{entity}{action}，应作为个股操作。"},
        },
    })

    result = _pipeline().run(TEAM, _snapshot(), findings, load_catalog(ROOT).execution_policy("codex@1"))

    assert result.status == "partial"
    assert result.report is not None
    assert next(item for item in result.report.insights if item.insight_id == "breadth_sentiment").reason_code == "agent_invalid"


def test_fixed_market_pipeline_blocks_stale_taxonomy_even_if_an_agent_returns_a_valid_finding():
    products = _snapshot().products
    products["industry_sector_taxonomy@1"] = _product(
        "industry_sector_taxonomy@1",
        quality="warning",
        payload={"status": "warning", "stale_fallback": True},
    )
    findings = {
        agent: _finding(agent)
        for agent in ("market_breadth@1", "sector_rotation@1", "market_macro_policy@1")
    }

    result = _pipeline().run(TEAM, _snapshot(products=products), findings, load_catalog(ROOT).execution_policy("codex@1"))

    assert result.status == "partial"
    assert result.report is not None
    blocked = [item for item in result.report.insights if item.status == "blocked"]
    assert [(item.insight_id, item.reason_code) for item in blocked] == [
        ("sector_rotation", "taxonomy_stale")
    ]


def test_fixed_market_pipeline_blocks_empty_information_even_if_an_agent_returns_a_valid_finding():
    products = _snapshot().products
    products["market_information@1"] = _product(
        "market_information@1",
        quality="warning",
        payload={"status": "empty", "items": []},
    )
    findings = {
        agent: _finding(agent)
        for agent in ("market_breadth@1", "sector_rotation@1", "market_macro_policy@1")
    }

    result = _pipeline().run(TEAM, _snapshot(products=products), findings, load_catalog(ROOT).execution_policy("codex@1"))

    assert result.status == "partial"
    assert result.report is not None
    blocked = [item for item in result.report.insights if item.status == "blocked"]
    assert [(item.insight_id, item.reason_code) for item in blocked] == [
        ("macro_policy", "information_unavailable")
    ]


def test_fixed_market_pipeline_blocks_when_no_insight_can_be_safely_published():
    result = _pipeline().run(
        TEAM,
        _snapshot(unavailable={"market_information@1": "fixture unavailable"}),
        {},
        load_catalog(ROOT).execution_policy("codex@1"),
    )

    assert result.status == "blocked"
    assert result.report is None
    assert [(item.insight_id, item.reason_code) for item in result.blocked_insights] == [
        ("breadth_sentiment", "quality_blocked"),
        ("sector_rotation", "quality_blocked"),
        ("macro_policy", "information_unavailable"),
    ]


@pytest.mark.parametrize("timestamp", [
    "2026-09-08T02:12:04.423751+00:00",
    "2026-09-08T02:12:40.428540+00:00",
    "2026-09-08 10:12:40.600519",
    "2026-09-08T02:12:40.600519Z",
])
def test_timestamp_fraction_does_not_become_a_security_recommendation(timestamp):
    from advisor.research.market_safety import contains_unsafe_market_output
    value = {"summary": "全市场上涨范围扩大。", "method": {"window": timestamp}}
    assert not contains_unsafe_market_output(value, security_names={"贵州茅台"})
    # Timestamp handling must not hide a real code/name in the same or another field.
    assert contains_unsafe_market_output({**value, "details": "600519"})
    assert contains_unsafe_market_output({**value, "details": "贵州茅台"}, security_names={"贵州茅台"})
    assert contains_unsafe_market_output({"summary": f"截至 {timestamp}，建议买入 600519。"})
