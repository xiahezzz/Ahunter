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
    atomic_write_pair,
    normalize_context,
    normalize_quality_results,
    report_paths,
    validate_run_id,
    validate_unique_ids,
)
from advisor.reporting.failure import write_failure_report


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
    quality_status, quality_payload = normalize_quality_results(quality_results)
    paths, resolved_run_id, supersession = report_paths(
        output_dir,
        report_date,
        "premarket",
        run_id=run_id,
        rerun_reason=rerun_reason,
        supersedes=supersedes,
    )
    if quality_status == "passed":
        validate_unique_ids(advice_items, "advice_id")
        published_advice = advice_items
        published_context = normalize_context(context)
    else:
        published_advice = []
        published_context = {}
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
    args = parser.parse_args(argv)
    report_date = args.report_date or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    try:
        report_day = date.fromisoformat(report_date)
    except ValueError:
        parser.error("--date must be an ISO date")
    as_of = args.as_of or datetime.combine(
        report_day, time(8, 30), tzinfo=ZoneInfo("Asia/Shanghai")
    )
    if coordinator is None:
        from advisor.coordinator import run_premarket
        coordinator = run_premarket
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
        run_id = f"premarket-cli-failed-{report_day:%Y%m%d}"
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
                report_date, "premarket",
                [QualityResult("runtime", "blocking", False, type(error).__name__)],
                args.output_dir, run_id=run_id,
            )
            payload.update({"json_path": str(paths.json_path), "markdown_path": str(paths.markdown_path)})
        except Exception:
            pass
        print(json.dumps(payload, sort_keys=True))
        return 1
