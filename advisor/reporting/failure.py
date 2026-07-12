from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from advisor.quality import QualityResult
from advisor.reporting.contracts import ReportPaths, atomic_write_pair, validate_run_id


_SAFE_DESCRIPTIONS = {
    "collector_state": "Collector state check failed.",
    "trading_calendar": "Trading calendar check failed.",
    "market_staleness": "Market data freshness check failed.",
    "three_year_candidate_coverage": "Market history coverage check failed.",
    "future_data_leakage": "Future-data boundary check failed.",
    "ledger_replay": "Ledger replay check failed.",
    "analyst_contract_readiness": "Required output readiness check failed.",
    "market_source_state": "Market source state check failed.",
    "optional_source_coverage": "Alternate source coverage check failed.",
}


def write_failure_report(
    report_date: str,
    run_type: Literal["premarket", "review"],
    failures: Sequence[QualityResult],
    output_dir: Path,
    *,
    run_id: str,
) -> ReportPaths:
    if run_type not in {"premarket", "review"}:
        raise ValueError("invalid attempted run type")
    validate_run_id(run_id)
    if not isinstance(failures, Sequence) or isinstance(failures, (str, bytes)):
        raise ValueError("blocking quality failures are required")
    blocking = list(failures)
    if not blocking or len(blocking) > 50 or not all(_valid_blocking_failure(item) for item in blocking):
        raise ValueError("blocking quality failures are required")

    quality = [_safe_failure(failure) for failure in blocking]
    directory = output_dir / report_date
    paths = ReportPaths(
        markdown_path=directory / f"failure.{run_id}.md",
        json_path=directory / f"failure.{run_id}.json",
    )
    lines = [
        f"# {report_date} Run Failure",
        "",
        f"- Attempted run type: {run_type}",
        "- Quality status: blocked",
        "",
        "## Quality Failures",
    ]
    for item in quality:
        lines.append(f"- {item['check_name']}: failed ({item['details']})")
    lines.append("")
    atomic_write_pair(
        paths,
        "\n".join(lines),
        json.dumps(
            {
                "attempted_run_type": run_type,
                "quality_results": quality,
                "quality_status": "blocked",
                "report_date": report_date,
                "report_time": "08:30" if run_type == "premarket" else "22:30",
                "report_type": "failure",
                "run_id": run_id,
            },
            indent=2,
            sort_keys=True,
        ) + "\n",
    )
    return paths


def _valid_blocking_failure(value: object) -> bool:
    return (
        isinstance(value, QualityResult)
        and value.severity == "blocking"
        and value.passed is False
        and isinstance(value.check_name, str)
        and bool(value.check_name)
        and isinstance(value.details, str)
        and bool(value.details)
    )


def _safe_failure(failure: QualityResult) -> dict[str, object]:
    check_name = failure.check_name if failure.check_name in _SAFE_DESCRIPTIONS else "quality_check"
    return {
        "check_name": check_name,
        "details": _SAFE_DESCRIPTIONS.get(check_name, "Quality check failed."),
        "passed": False,
        "severity": "blocking",
    }
