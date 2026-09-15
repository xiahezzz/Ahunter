from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import math
import sqlite3
from typing import Any

from advisor.research.contracts import TeamConclusion, VersionRef


class TeamReviewStatus(StrEnum):
    passed = "passed"
    blocked = "blocked"


@dataclass(frozen=True)
class TeamReview:
    review_id: str
    conclusion_hash: str
    team: VersionRef
    subject_code: str
    morning_as_of: datetime
    review_as_of: datetime
    status: TeamReviewStatus
    outcome: str | None
    observed_change: float | None
    message: str | None = None


@dataclass(frozen=True)
class TeamProfileProjection:
    team_ref: str
    subject_code: str
    conclusion_hash: str
    profile: dict[str, Any]


class TeamScopedProfileStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def save(self, projection: TeamProfileProjection) -> str:
        team = str(VersionRef.parse(projection.team_ref))
        _validate_code(projection.subject_code)
        _validate_hash(projection.conclusion_hash)
        if not isinstance(projection.profile, dict):
            raise ValueError("team profile must be an object")
        profile_id = "profile-" + hashlib.sha256(
            f"{team}|{projection.subject_code}|{projection.conclusion_hash}".encode()
        ).hexdigest()[:32]
        self.connection.execute(
            """
            INSERT INTO research_team_profiles(
              profile_id, team_ref, subject_code, conclusion_hash, profile_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(team_ref, subject_code, conclusion_hash) DO NOTHING
            """,
            (
                profile_id,
                team,
                projection.subject_code,
                projection.conclusion_hash,
                json.dumps(projection.profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.connection.commit()
        return profile_id


def evaluate_team_review(
    conclusion: TeamConclusion,
    conclusion_hash: str,
    *,
    review_as_of: datetime,
    close: float | None,
    open_close: float | None,
    expected_team: str | None = None,
    expected_subject: str | None = None,
) -> TeamReview:
    _validate_hash(conclusion_hash)
    if review_as_of.tzinfo is None or review_as_of.utcoffset() is None:
        raise ValueError("review_as_of must be timezone-aware")
    if review_as_of < conclusion.boundary.as_of:
        raise ValueError("review_as_of cannot precede conclusion as_of")
    team = str(conclusion.team)
    if expected_team is not None and team != str(VersionRef.parse(expected_team)):
        raise ValueError("review Team does not match Team Conclusion")
    if expected_subject is not None and conclusion.subject.code != expected_subject:
        raise ValueError("review Subject does not match Team Conclusion")
    review_id = "review-" + hashlib.sha256(
        f"{conclusion_hash}|{team}|{conclusion.subject.code}|{review_as_of.isoformat()}".encode()
    ).hexdigest()[:32]
    if close is None or open_close is None or not _finite(close) or not _finite(open_close) or open_close == 0:
        return TeamReview(
            review_id, conclusion_hash, conclusion.team, conclusion.subject.code, conclusion.boundary.as_of,
            review_as_of, TeamReviewStatus.blocked, None, None, "reliable closing data is unavailable",
        )
    change = (float(close) - float(open_close)) / float(open_close)
    outcome = _outcome(conclusion.stance, change)
    return TeamReview(
        review_id, conclusion_hash, conclusion.team, conclusion.subject.code, conclusion.boundary.as_of,
        review_as_of, TeamReviewStatus.passed, outcome, change,
    )


def _outcome(stance: str, change: float) -> str:
    if abs(change) < 0.005:
        return "flat"
    if stance in {"watch_buy", "watch_add"}:
        return "aligned" if change > 0 else "misaligned"
    if stance in {"watch_reduce", "watch_exit"}:
        return "aligned" if change < 0 else "misaligned"
    return "aligned" if abs(change) < 0.02 else "risk_review"


def _validate_hash(value: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("conclusion_hash must be a SHA-256 hex digest")


def _validate_code(value: str) -> None:
    if not isinstance(value, str) or len(value) != 6 or not value.isdigit():
        raise ValueError("subject_code must be six digits")


def _finite(value: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
