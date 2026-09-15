"""Team-scoped close review for already-published Research Conclusions."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time
import json
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from advisor.config import load_advisor_config, resolve_research_artifact_dir, resolve_state_db
from advisor.research.artifacts import ArtifactStore
from advisor.research.contracts import TeamConclusion, VersionRef
from advisor.research.reporting.reviews import TeamReviewPublication, TeamReviewReporter
from advisor.research.repository import ResearchRepository
from advisor.research.reviews import evaluate_team_review


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate published Team Conclusions without calling Codex.")
    parser.add_argument("--as-of", type=datetime.fromisoformat)
    parser.add_argument("--date", "--report-date", dest="report_date")
    parser.add_argument("--codes")
    parser.add_argument("--team", action="append")
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument("--config", type=Path, default=Path("config/advisor.yaml"))
    parser.add_argument("--db", type=Path)
    parser.add_argument("--artifact-dir", type=Path)
    args = parser.parse_args(argv)

    try:
        report_day = date.fromisoformat(args.report_date) if args.report_date else datetime.now(_SHANGHAI).date()
        review_as_of = args.as_of or datetime.combine(report_day, time(22, 30), tzinfo=_SHANGHAI)
        if review_as_of.tzinfo is None or review_as_of.utcoffset() is None:
            raise ValueError("--as-of must include a timezone offset")
        config = load_advisor_config(args.config)
        root = args.config.resolve().parent.parent
        db_path = (args.db or resolve_state_db(config, root)).resolve()
        artifact_dir = (args.artifact_dir or resolve_research_artifact_dir(config, root)).resolve()
        repository = ResearchRepository.open(db_path)
        store = ArtifactStore(artifact_dir)
        try:
            codes = _parse_codes(args.codes)
            teams = {str(VersionRef.parse(item)) for item in args.team} if args.team else None
            publications: list[TeamReviewPublication] = []
            for cycle_dir in sorted((args.output_dir / report_day.isoformat()).iterdir() if (args.output_dir / report_day.isoformat()).is_dir() else ()):
                if not cycle_dir.is_dir() or cycle_dir.name == "reviews":
                    continue
                cycle_path = cycle_dir / "cycle.json"
                if not cycle_path.is_file():
                    continue
                cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
                subject = cycle.get("subject", {})
                code = subject.get("code")
                if code not in codes if codes else False:
                    continue
                snapshot_id = cycle.get("snapshot", {}).get("snapshot_id") if isinstance(cycle.get("snapshot"), dict) else None
                for team_dir in sorted((cycle_dir / "teams").iterdir() if (cycle_dir / "teams").is_dir() else ()):
                    if not team_dir.is_dir() or (teams is not None and team_dir.name not in teams):
                        continue
                    conclusion_path = team_dir / "conclusion.json"
                    if not conclusion_path.is_file():
                        continue
                    conclusion_payload = json.loads(conclusion_path.read_text(encoding="utf-8"))
                    conclusion = TeamConclusion.model_validate(conclusion_payload)
                    if conclusion.subject.code != code:
                        continue
                    conclusion_hash = _conclusion_hash(repository, cycle_dir.name, team_dir.name, conclusion_payload)
                    if conclusion_hash is None:
                        continue
                    close, open_close = _closing_values(repository, store, snapshot_id, review_as_of, conclusion.boundary.as_of)
                    review = evaluate_team_review(
                        conclusion,
                        conclusion_hash,
                        review_as_of=review_as_of,
                        close=close,
                        open_close=open_close,
                        expected_team=team_dir.name,
                        expected_subject=code,
                    )
                    publication = TeamReviewReporter(args.output_dir).publish(review)
                    _persist_review(repository, store, review, publication)
                    publications.append(publication)
        finally:
            repository.close()
        print(json.dumps({
            "status": "passed" if publications else "blocked",
            "reviews": [str(item.json_path) for item in publications],
        }, ensure_ascii=False, sort_keys=True))
        return 0 if publications else 1
    except Exception as error:
        print(json.dumps({"status": "failed", "error": type(error).__name__, "message": str(error)[:240]}, ensure_ascii=False, sort_keys=True))
        return 1


def _parse_codes(raw: str | None) -> set[str] | None:
    if raw is None or not raw.strip():
        return None
    values = {item.strip() for item in raw.split(",")}
    if any(len(item) != 6 or not item.isdigit() for item in values):
        raise ValueError("--codes must contain six-digit A-share codes")
    return values


def _conclusion_hash(repository: ResearchRepository, cycle_id: str, team_ref: str, payload: dict) -> str | None:
    row = repository.connection.execute(
        "SELECT conclusion_hash FROM research_team_conclusions WHERE research_run_id = ? AND team_ref = ?",
        (f"{cycle_id}:{team_ref}", team_ref),
    ).fetchone()
    if row and isinstance(row[0], str):
        return row[0]
    return None


def _closing_values(repository: ResearchRepository, store: ArtifactStore, snapshot_id: str | None, review_as_of: datetime, morning_as_of: datetime):
    if not snapshot_id:
        return None, None
    row = repository.connection.execute(
        "SELECT artifact_hash FROM research_snapshot_products WHERE snapshot_id = ? AND product_ref = 'market_daily_bars@1'",
        (snapshot_id,),
    ).fetchone()
    if not row:
        return None, None
    try:
        envelope = store.read_json(row[0])
        bars = envelope.get("payload", {}).get("rows", [])
        usable = sorted(
            (item for item in bars if isinstance(item, dict) and isinstance(item.get("trade_date"), str) and isinstance(item.get("close"), (int, float))),
            key=lambda item: item["trade_date"],
        )
        review_day = review_as_of.date().isoformat()
        morning_day = morning_as_of.date().isoformat()
        close_rows = [item for item in usable if item["trade_date"] <= review_day]
        open_rows = [item for item in usable if item["trade_date"] <= morning_day]
        return (
            float(close_rows[-1]["close"]) if close_rows else None,
            float(open_rows[-1]["close"]) if open_rows else None,
        )
    except (OSError, ValueError, TypeError, KeyError):
        return None, None


def _persist_review(repository: ResearchRepository, store: ArtifactStore, review, publication: TeamReviewPublication) -> None:
    review_ref = store.put_bytes(publication.json_path.read_bytes(), media_type="application/json")
    relative_path = str(store._path_for(review_ref.content_hash).relative_to(store.root))
    repository.record_artifact(review_ref, relative_path=relative_path)
    repository.record_review(
        review_id=review.review_id,
        conclusion_hash=review.conclusion_hash,
        team_ref=str(review.team),
        subject_code=review.subject_code,
        as_of=review.review_as_of.isoformat(),
        status=review.status.value,
        outcome=review.outcome,
        review_hash=review_ref.content_hash,
    )
    repository.connection.commit()


if __name__ == "__main__":
    raise SystemExit(main())
