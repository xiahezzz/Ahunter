import json
from pathlib import Path
from collections.abc import Mapping, Sequence

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
