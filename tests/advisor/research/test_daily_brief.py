from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from advisor.research.batch import DailyBatchResult
from advisor.research.contracts import ResearchBoundary, ResearchSubject, RunStatus, VersionRef
from advisor.research.reporting.daily import DailyBriefRenderer
from advisor.research.state_machine import ResearchCycleResult, TeamRunResult


def test_daily_brief_is_navigation_only_and_team_scoped(tmp_path: Path):
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))
    cycles = {
        "600519": ResearchCycleResult(
            "cycle-600519", ResearchSubject(code="600519"), boundary, RunStatus.passed, "a" * 64, None, {}, {},
            {"core@1": TeamRunResult(VersionRef.parse("core@1"), RunStatus.passed, conclusion=SimpleNamespace(conclusion={"thesis": "core-only"})), "other@1": TeamRunResult(VersionRef.parse("other@1"), RunStatus.passed, conclusion=SimpleNamespace(conclusion={"thesis": "other-secret"}))},
        ),
        "000001": ResearchCycleResult(
            "cycle-000001", ResearchSubject(code="000001"), boundary, RunStatus.blocked, "b" * 64, None, {}, {"core": "failed"},
            {"core@1": TeamRunResult(VersionRef.parse("core@1"), RunStatus.blocked, message="missing Stage")},
        ),
    }
    batch = DailyBatchResult("batch-1", boundary, ("core@1",), "codex@1", cycles, {"600519": ("explicit",), "000001": ("explicit",)})

    publication = DailyBriefRenderer(tmp_path).render(batch, "core@1")
    text = publication.read_text(encoding="utf-8")
    assert "600519" in text and "000001" in text
    assert "other-secret" not in text and "core-only" not in text
    assert "# 每日研究简报" in text
    assert "已阻断" in text and "missing Stage" not in text
    assert "Daily Team Brief" not in text
