from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

import pytest

from advisor.research.contracts import ResearchBoundary, ResearchSubject, TeamConclusion
from advisor.research.reviews import TeamProfileProjection, TeamScopedProfileStore


def _conclusion(team: str) -> TeamConclusion:
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))
    return TeamConclusion(
        team=team, subject=ResearchSubject(code="600519"), boundary=boundary, stance="hold", conviction="medium",
        evidence_quality={"status": "passed"}, thesis=f"{team} thesis", evidence=(), key_risks=(), invalidation_conditions=(), time_horizon="swing"
    )


def test_team_profiles_preserve_two_teams_for_the_same_subject_without_overwrite():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """CREATE TABLE research_team_profiles (
          profile_id TEXT PRIMARY KEY, team_ref TEXT NOT NULL, subject_code TEXT NOT NULL,
          conclusion_hash TEXT NOT NULL, profile_json TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(team_ref, subject_code, conclusion_hash)
        )"""
    )
    store = TeamScopedProfileStore(connection)
    store.save(TeamProfileProjection("core@1", "600519", "a" * 64, {"thesis": "core"}))
    store.save(TeamProfileProjection("value@1", "600519", "b" * 64, {"thesis": "value"}))

    rows = connection.execute("SELECT team_ref, subject_code, conclusion_hash FROM research_team_profiles ORDER BY team_ref").fetchall()
    assert rows == [("core@1", "600519", "a" * 64), ("value@1", "600519", "b" * 64)]


def test_team_profile_rejects_unscoped_or_invalid_conclusion_hash():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE research_team_profiles (profile_id TEXT PRIMARY KEY, team_ref TEXT, subject_code TEXT, conclusion_hash TEXT, profile_json TEXT, created_at TEXT, UNIQUE(team_ref, subject_code, conclusion_hash))")
    store = TeamScopedProfileStore(connection)
    with pytest.raises(ValueError):
        store.save(TeamProfileProjection("core@1", "600519", "not-a-hash", {"thesis": "x"}))
