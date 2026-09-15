from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from advisor.research.contracts import (
    EvidenceRef,
    FindingQuality,
    ResearchBoundary,
    ResearchFinding,
    ResearchScope,
    ResearchSubject,
    RunStatus,
    TeamConclusion,
    VersionRef,
)
from advisor.research.data_products.engine import ProductResult, SnapshotResult
from advisor.research.decision.contracts import MarketInsight, MarketTeamReport, TraderProposal
from advisor.research.decision.market import MarketPipelineResult
from advisor.research.decision.pipeline import PipelineResult, StageResult
from advisor.research.reporting.cycle import CycleReporter
from advisor.research.state_machine import ResearchCycleResult, TeamRunResult


def _cycle():
    subject = ResearchSubject(code="600519", name="Fixture")
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))
    evidence = EvidenceRef(evidence_id="product:bars@1:fixture", source="fixture", observed_at=boundary.as_of, excerpt="最新收盘价为10元。")
    finding = ResearchFinding(
        agent={"id": "market", "version": 1}, subject=subject, boundary=boundary, summary="价格处于观察区间，短期方向尚不明确。",
        claims=(), evidence=(evidence,), risks=("成交量不足。",), invalidation_conditions=("价格突破观察区间。",), quality={"status": "passed"}, details={}
    )
    conclusion = TeamConclusion(
        team={"id": "core", "version": 1}, subject=subject, boundary=boundary, stance="hold", conviction="medium",
        evidence_quality={"status": "passed"}, thesis="现有证据支持继续观察，暂不增加风险。", evidence=(evidence,), key_risks=("成交量不足。",),
        invalidation_conditions=("价格突破观察区间。",), time_horizon="未来一到十个交易日", position_limit="保持较小的观察仓位。"
    )
    trader = TraderProposal(
        stage="trader", stance="hold", conviction="medium", thesis="当前更适合继续观察，暂不采取行动。",
        evidence=(evidence,), key_risks=("成交量不足。",), invalidation_conditions=("价格突破观察区间。",),
        time_horizon="未来一到十个交易日", price_range=None, position_limit="保持较小的观察仓位。", quality={"status": "passed"},
    )
    pipeline = PipelineResult(
        VersionRef.parse("core@1"), subject, boundary, conclusion,
        {
            "finding_quality": StageResult("finding_quality", FindingQuality(status="passed"), "a" * 64, None, "b" * 64),
            "trader": StageResult("trader", trader, "c" * 64, None, "d" * 64),
        },
        VersionRef.parse("decision@1"),
    )
    return ResearchCycleResult(
        "cycle-1", subject, boundary, RunStatus.passed,
        "f" * 64, SimpleNamespace(snapshot_id="snapshot-1", snapshot_hash="s" * 64),
        {"market@1": SimpleNamespace(finding=finding)}, {},
        {
            "core@1": TeamRunResult(VersionRef.parse("core@1"), RunStatus.passed, pipeline),
            "blocked@1": TeamRunResult(VersionRef.parse("blocked@1"), RunStatus.blocked, message="missing Agent market@1"),
        },
    )


def _unsafe_direct_market_cycle() -> ResearchCycleResult:
    """A typed report that did not pass through MarketDecisionPipeline.run."""
    subject = ResearchSubject(scope=ResearchScope.market)
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))
    evidence = EvidenceRef(
        evidence_id="product:whole_market_intraday_snapshot@1:fixture",
        source="fixture",
        observed_at=boundary.as_of,
        excerpt="固定市场快照。",
    )
    insight = MarketInsight(
        insight_id="breadth_sentiment",
        agent=VersionRef.parse("market_breadth@1"),
        status="passed",
        summary="市场整体广度仅供观察。",
        evidence=(evidence,),
        method={
            "inputs": ["固定快照"],
            "time_windows": ["当前边界"],
            "criteria": {"reader_note": "贵州茅台建议投资。"},
        },
    )
    report = MarketTeamReport(
        team=VersionRef.parse("a_share_market_overview@1"),
        subject=subject,
        boundary=boundary,
        status="passed",
        insights=(insight,),
        evidence_quality={"status": "passed"},
        time_horizon="当前研究边界至下一个交易日的全市场观察窗口",
        risks=("固定样例覆盖范围有限。",),
        invalidation_conditions=("后续数据与当前观察相反。",),
        provenance={"pipeline": "direct-fixture"},
    )
    snapshot = SnapshotResult(
        snapshot_id="snapshot-market-fixture",
        subject=subject,
        boundary=boundary,
        products={
            "whole_market_intraday_snapshot@1": ProductResult(
                product=VersionRef.parse("whole_market_intraday_snapshot@1"),
                payload={
                    "rows": [{"code": "600519", "name": "贵州茅台"}],
                    "expected_universe": {"securities": [{"code": "600519", "name": "贵州茅台"}]},
                },
                artifact_hash="a" * 64,
                provider="fixture",
                quality_status="passed",
                quality_message=None,
                attempts=(),
            ),
        },
        snapshot_hash="b" * 64,
    )
    team = VersionRef.parse("a_share_market_overview@1")
    pipeline = MarketPipelineResult(team, subject, boundary, report, "passed", ())
    return ResearchCycleResult(
        cycle_id="cycle-market-unsafe",
        subject=subject,
        boundary=boundary,
        status=RunStatus.passed,
        fingerprint="c" * 64,
        snapshot=snapshot,
        agent_results={},
        agent_errors={},
        team_results={team.__str__(): TeamRunResult(team, RunStatus.passed, pipeline)},
    )


def test_cycle_reporter_publishes_independent_team_directories(tmp_path: Path):
    publication = CycleReporter(tmp_path).publish(_cycle())
    root = publication.root

    assert (root / "cycle.json").is_file()
    assert (root / "index.md").is_file()
    assert (root / "teams" / "core@1" / "conclusion.json").is_file()
    assert (root / "teams" / "core@1" / "report.md").is_file()
    assert (root / "teams" / "blocked@1" / "status.json").is_file()
    assert not (root / "teams" / "blocked@1" / "conclusion.json").exists()
    report = (root / "teams" / "core@1" / "report.md").read_text(encoding="utf-8")
    assert "当前更适合继续观察，暂不采取行动。" in report
    assert "# 团队研究报告" in report
    assert "## 专家观点" in report
    assert "## 最终结论" in report
    assert "判断：`持有观察`" in report
    assert "# Team Report" not in report
    index = (root / "index.md").read_text(encoding="utf-8").lower()
    assert "# 研究任务索引" in index
    assert "研究团队" in index and "状态" in index and "文件" in index
    for forbidden in ("stance", "conviction", "thesis", "comparison", "consensus", "ranking"):
        assert forbidden not in index
    TeamConclusion.model_validate(json.loads((root / "teams" / "core@1" / "conclusion.json").read_text(encoding="utf-8")))
    marker = json.loads((root / "complete.json").read_text(encoding="utf-8"))
    assert marker["files"]["cycle.json"]


def test_cycle_reporter_never_overwrites_a_published_cycle(tmp_path: Path):
    reporter = CycleReporter(tmp_path)
    reporter.publish(_cycle())
    with pytest.raises(FileExistsError):
        reporter.publish(_cycle())


def test_cycle_reporter_recovers_only_a_matching_complete_publication(tmp_path: Path):
    reporter = CycleReporter(tmp_path)
    published = reporter.publish(_cycle())

    recovered = reporter.recover(_cycle())

    assert recovered == published
    with pytest.raises(FileExistsError, match="not recoverable"):
        reporter.recover(replace(_cycle(), fingerprint="e" * 64))


def test_cycle_reporter_retries_from_a_staged_write_interruption_without_occupying_the_final_path(tmp_path: Path, monkeypatch):
    reporter = CycleReporter(tmp_path)
    original = reporter._write_new
    calls = 0

    def interrupted(path, content):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("fixture interruption after cycle.json")
        original(path, content)

    monkeypatch.setattr(reporter, "_write_new", interrupted)
    with pytest.raises(OSError, match="fixture interruption"):
        reporter.publish(_cycle())
    final_root = tmp_path / "2026-08-06" / "cycle-1"
    assert not final_root.exists()
    assert list(final_root.parent.glob(".cycle-1.staging-*"))

    monkeypatch.setattr(reporter, "_write_new", original)
    publication = reporter.publish_or_recover(_cycle())

    assert publication.root == final_root
    assert publication.complete_marker.is_file()


def test_cycle_reporter_refuses_to_publish_english_reader_text(tmp_path: Path):
    cycle = _cycle()
    team_result = cycle.team_results["core@1"]
    pipeline = team_result.conclusion
    conclusion = pipeline.conclusion.model_copy(update={"thesis": "English-only conclusion."})
    changed_pipeline = replace(pipeline, conclusion=conclusion)
    changed_team = replace(team_result, conclusion=changed_pipeline)
    changed_cycle = replace(
        cycle,
        cycle_id="cycle-english",
        team_results={**cycle.team_results, "core@1": changed_team},
    )

    with pytest.raises(ValueError, match="简体中文"):
        CycleReporter(tmp_path).publish(changed_cycle)
    assert not (tmp_path / "2026-08-06" / "cycle-english" / "complete.json").exists()


def test_cycle_reporter_rejects_an_unsafe_typed_market_report_that_bypassed_the_pipeline(tmp_path: Path):
    with pytest.raises(ValueError, match="per-security recommendation"):
        CycleReporter(tmp_path).publish(_unsafe_direct_market_cycle())

    assert not (tmp_path / "2026-08-06" / "cycle-market-unsafe").exists()
