import argparse
import json
from pathlib import Path
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from advisor.quality import QualityResult
from advisor.reporting.contracts import (
    AdviceItem,
    ReportPaths,
    ReviewItem,
    atomic_write_pair,
    load_premarket_link,
    normalize_context,
    normalize_quality_results,
    report_paths,
    validate_run_id,
    validate_unique_ids,
)
from advisor.reporting.premarket import DISCLAIMER
from advisor.reporting.failure import write_failure_report


def write_review_report(
    report_date: str,
    morning_advice: list[AdviceItem],
    review_items: list[ReviewItem],
    output_dir: Path,
    *,
    quality_results: Sequence[QualityResult] | None = None,
    context: Mapping[str, object] | None = None,
    run_id: str | None = None,
    rerun_reason: str | None = None,
    supersedes: str | None = None,
    premarket_run_id: str = "initial",
) -> ReportPaths:
    quality_status, quality_payload = normalize_quality_results(quality_results)
    paths, resolved_run_id, supersession = report_paths(
        output_dir,
        report_date,
        "review",
        run_id=run_id,
        rerun_reason=rerun_reason,
        supersedes=supersedes,
    )
    validate_run_id(premarket_run_id)
    if quality_status == "passed":
        validate_unique_ids(morning_advice, "advice_id")
        validate_unique_ids(review_items, "review_id")
        advice_by_id = {item.advice_id: item for item in morning_advice}
        for item in review_items:
            if item.advice_id not in advice_by_id:
                raise ValueError(f"orphan advice_id: {item.advice_id}")
        published_advice = morning_advice
        published_reviews = review_items
        published_context = normalize_context(context)
        linked_premarket = load_premarket_link(
            paths.json_path.parent,
            report_date,
            premarket_run_id,
            morning_advice,
        )
    else:
        advice_by_id = {}
        published_advice = []
        published_reviews = []
        published_context = {}
        linked_premarket = None
    lines = [
        f"# {report_date} 22:30 Daily Review",
        "",
        "## Data Quality",
        f"- Status: {quality_status}",
    ]
    for result in quality_payload:
        lines.append(
            f"- {result['check_name']}: {'passed' if result['passed'] else 'failed'} ({result['details']})"
        )
    lines.extend([
        "",
        "## Morning Advice",
    ])
    if not published_advice:
        lines.append("- No current entries")
    for item in published_advice:
        lines.append(f"- {item.advice_id}: {item.code} {item.action}")
    lines.extend(["", "## Market Outcome"])
    _append_context(lines, published_context, "market_outcome")
    lines.extend(["", "## Candidate Review"])
    if not published_reviews:
        lines.append("- No current entries")
    for item in published_reviews:
        linked = advice_by_id[item.advice_id]
        lines.extend(
            [
                f"### {item.review_id}",
                f"- Advice ID: {item.advice_id}",
                f"- Code: {linked.code}",
                f"- Outcome: {item.outcome}",
                f"- Review: {item.review_text}",
                "",
            ]
        )
    lines.extend(["## Evidence Worked/Failed/Missing"])
    _append_context(lines, published_context, "evidence_worked")
    _append_context(lines, published_context, "evidence_failed")
    _append_context(lines, published_context, "evidence_missing")
    lines.extend(["", "## Ledger Impact"])
    _append_context(lines, published_context, "ledger_impact")
    lines.extend(["", "## Stock Profile Updates"])
    _append_context(lines, published_context, "stock_profile_updates")
    lines.extend(["", "## Next-Day Carryover"])
    _append_context(lines, published_context, "next_day_carryover")
    lines.extend(["", "## Disclaimer", DISCLAIMER, ""])
    atomic_write_pair(
        paths,
        "\n".join(lines),
        json.dumps(
            {
                "context": published_context,
                "linked_premarket": linked_premarket,
                "morning_advice": [item.to_dict() for item in published_advice],
                "morning_advice_ids": [item.advice_id for item in published_advice],
                "quality_results": quality_payload,
                "quality_status": quality_status,
                "report_date": report_date,
                "report_time": "22:30",
                "report_type": "review",
                "review_ids": [item.review_id for item in published_reviews],
                "reviews": [item.to_dict() for item in published_reviews],
                "run_id": resolved_run_id,
                "supersession": supersession,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return paths


def _append_context(lines: list[str], context: dict[str, list[str]], key: str) -> None:
    values = context.get(key, [])
    lines.extend(f"- {value}" for value in values) if values else lines.append("- No current entries")


def main(argv: Sequence[str] | None = None, *, coordinator=None, snapshot_reader=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", type=datetime.fromisoformat)
    parser.add_argument("--date", "--report-date", dest="report_date")
    parser.add_argument("--codes")
    parser.add_argument("--events-db", type=Path, default=Path("data/state/events.sqlite"))
    parser.add_argument("--allowed-rids", type=Path, default=Path("config/allowed-rids.yaml"))
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument("--config", type=Path, default=Path("config/advisor.yaml"))
    parser.add_argument("--run-id")
    parser.add_argument("--report-run-id")
    parser.add_argument("--rerun-reason")
    parser.add_argument("--supersedes")
    parser.add_argument("--premarket-run-id", default="initial")
    args = parser.parse_args(argv)
    report_date = args.report_date or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    try:
        report_day = date.fromisoformat(report_date)
    except ValueError:
        parser.error("--date must be an ISO date")
    as_of = args.as_of or datetime.combine(
        report_day, time(22, 30), tzinfo=ZoneInfo("Asia/Shanghai")
    )
    if coordinator is None:
        from advisor.coordinator import run_review
        coordinator = run_review
    if snapshot_reader is None:
        from advisor.evidence.mx_adapter import read_collector_snapshot
        snapshot_reader = read_collector_snapshot
    try:
        snapshot = snapshot_reader(args.events_db, args.allowed_rids, as_of=as_of)
        result = coordinator(
            collector_snapshot=snapshot,
            as_of=as_of,
            report_date=report_date,
            candidate_codes=tuple(filter(None, args.codes.split(","))) if args.codes else (),
            output_dir=args.output_dir,
            config_path=args.config,
            run_id=args.run_id,
            report_run_id=args.report_run_id,
            rerun_reason=args.rerun_reason,
            supersedes=args.supersedes,
            premarket_run_id=args.premarket_run_id,
        )
        payload = {
            "json_path": str(result.report_paths.json_path),
            "markdown_path": str(result.report_paths.markdown_path),
            "run_id": result.run_id,
            "status": result.status,
            "warnings": list(result.warnings),
        }
        print(json.dumps(payload, sort_keys=True))
        return 0 if result.status in {"passed", "blocked"} else 1
    except Exception as error:
        run_id = f"review-cli-failed-{report_day:%Y%m%d}"
        if args.run_id is not None:
            try:
                validate_run_id(args.run_id)
            except ValueError:
                pass
            else:
                run_id = args.run_id
        payload = {"error": type(error).__name__, "run_id": run_id, "status": "failed"}
        try:
            paths = write_failure_report(
                report_date, "review",
                [QualityResult("runtime", "blocking", False, type(error).__name__)],
                args.output_dir, run_id=run_id,
            )
            payload.update({"json_path": str(paths.json_path), "markdown_path": str(paths.markdown_path)})
        except Exception:
            pass
        print(json.dumps(payload, sort_keys=True))
        return 1
