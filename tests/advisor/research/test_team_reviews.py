from __future__ import annotations

from datetime import datetime, timezone

import pytest

from advisor.research.contracts import ResearchBoundary, ResearchSubject, TeamConclusion, VersionRef
from advisor.research.reviews import TeamReviewStatus, evaluate_team_review


def _conclusion(team="core@1"):
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))
    return TeamConclusion(
        team=VersionRef.parse(team), subject=ResearchSubject(code="600519"), boundary=boundary, stance="watch_buy", conviction="medium",
        evidence_quality={"status": "passed"}, thesis="fixture", evidence=(), key_risks=(), invalidation_conditions=(), time_horizon="swing"
    )


def test_review_is_bound_to_exact_team_conclusion_and_never_creates_a_new_stance():
    conclusion = _conclusion()
    review = evaluate_team_review(conclusion, "a" * 64, review_as_of=datetime(2026, 8, 6, 22, 30, tzinfo=timezone.utc), close=101.0, open_close=100.0)

    assert review.status == TeamReviewStatus.passed
    assert review.conclusion_hash == "a" * 64
    assert not hasattr(review, "stance")


def test_review_blocks_when_close_or_conclusion_scope_is_not_reliable():
    conclusion = _conclusion()
    review = evaluate_team_review(conclusion, "a" * 64, review_as_of=datetime(2026, 8, 6, 22, 30, tzinfo=timezone.utc), close=None, open_close=100.0)

    assert review.status == TeamReviewStatus.blocked
    assert review.outcome is None


def test_review_rejects_cross_team_or_cross_subject_binding():
    with pytest.raises(ValueError):
        evaluate_team_review(_conclusion("core@1"), "a" * 64, review_as_of=datetime(2026, 8, 6, 22, 30, tzinfo=timezone.utc), close=101.0, open_close=100.0, expected_team="value@1")
