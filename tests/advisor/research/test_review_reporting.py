from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from advisor.research.contracts import ResearchBoundary, ResearchSubject, TeamConclusion, VersionRef
from advisor.research.reporting.reviews import TeamReviewReporter
from advisor.research.reviews import evaluate_team_review


def test_team_review_report_is_scoped_and_immutable(tmp_path: Path):
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))
    conclusion = TeamConclusion(
        team=VersionRef.parse("a_share_core@1"),
        subject=ResearchSubject(code="600519"),
        boundary=boundary,
        stance="hold",
        conviction="medium",
        evidence_quality={"status": "passed"},
        thesis="fixture thesis",
        evidence=(),
        key_risks=("fixture risk",),
        invalidation_conditions=("fixture invalidation",),
        time_horizon="swing",
    )
    review = evaluate_team_review(
        conclusion,
        "a" * 64,
        review_as_of=datetime(2026, 8, 6, 22, 30, tzinfo=timezone.utc),
        close=101,
        open_close=100,
    )
    publication = TeamReviewReporter(tmp_path).publish(review)

    assert publication.json_path.is_file()
    assert publication.markdown_path.is_file()
    markdown = publication.markdown_path.read_text(encoding="utf-8")
    assert "a_share_core@1" in markdown
    assert "stance" not in markdown.lower()
    assert "# 团队晚间复盘" in markdown
    assert "复盘结果" in markdown
    assert "Team Review" not in markdown
