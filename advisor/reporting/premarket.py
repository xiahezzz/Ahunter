import json
from pathlib import Path
from collections.abc import Mapping, Sequence

from advisor.quality import QualityResult
from advisor.reporting.contracts import (
    AdviceItem,
    ReportPaths,
    atomic_write_pair,
    ensure_archive_is_new,
    normalize_context,
    normalize_quality_results,
    report_paths,
    validate_unique_ids,
)


DISCLAIMER = "Research output only. This is not an order, broker instruction, or guaranteed return."


def write_premarket_report(
    report_date: str,
    advice_items: list[AdviceItem],
    output_dir: Path,
    *,
    quality_results: Sequence[QualityResult] | None = None,
    context: Mapping[str, object] | None = None,
    run_id: str | None = None,
    rerun_reason: str | None = None,
    supersedes: str | None = None,
) -> ReportPaths:
    validate_unique_ids(advice_items, "advice_id")
    quality_status, quality_payload = normalize_quality_results(quality_results)
    context_payload = normalize_context(context)
    paths, resolved_run_id, supersession = report_paths(
        output_dir,
        "premarket",
        run_id=run_id,
        rerun_reason=rerun_reason,
        supersedes=supersedes,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    ensure_archive_is_new(paths)
    published_advice = advice_items if quality_status == "passed" else []
    published_context = context_payload if quality_status == "passed" else {}
    lines = [
        f"# {report_date} 08:30 Premarket Advice",
        "",
        "## Data Quality",
        f"- Status: {quality_status}",
    ]
    for result in quality_payload:
        lines.append(
            f"- {result['check_name']}: {'passed' if result['passed'] else 'failed'} ({result['details']})"
        )
    lines.extend(["", "## Market Regime/Index Context"])
    _append_context(lines, published_context, "market_regime")
    lines.extend(["", "## Information Flow"])
    _append_context(lines, published_context, "information_flow")
    lines.extend(["", "## Capital Flow"])
    _append_context(lines, published_context, "capital_flow")
    lines.extend(["", "## Analyst Flow"])
    _append_context(lines, published_context, "analyst_flow")
    lines.extend(["", "## Suggested Watchlist/Actions"])
    if not published_advice:
        lines.append("- No current entries")
    for item in published_advice:
        lines.extend(
            [
                f"### {item.code} {item.action}",
                f"- Advice ID: {item.advice_id}",
                f"- Confidence: {item.confidence:.2f}",
                f"- Rationale: {item.rationale}",
                f"- Evidence: {', '.join(item.evidence_ids)}",
                "",
            ]
        )
    lines.extend(["## Ledger Exposure/Risk"])
    _append_context(lines, published_context, "ledger_exposure")
    lines.extend(["", "## Entry/Invalidation/Risk Controls"])
    _append_context(lines, published_context, "risk_controls")
    lines.extend(["", "## Evidence"])
    _append_context(lines, published_context, "evidence")
    lines.extend(["", "## Disclaimer", DISCLAIMER, ""])
    atomic_write_pair(
        paths,
        "\n".join(lines),
        json.dumps(
            {
                "advice": [item.to_dict() for item in published_advice],
                "advice_ids": [item.advice_id for item in published_advice],
                "context": published_context,
                "quality_results": quality_payload,
                "quality_status": quality_status,
                "report_date": report_date,
                "report_time": "08:30",
                "report_type": "premarket",
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
