from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from advisor import coordinator as coordinator_module
from advisor.calendar import latest_expected_session
from advisor.config import load_advisor_config, resolve_state_db
from advisor.data_sources.backfill import update_market_database
from advisor.data_sources.free_sources import ConfiguredProviderRegistry
from advisor.db.migrate import migrate_database
from advisor.evidence.mx_adapter import read_collector_snapshot
from advisor.scheduler.premarket import _expanded_candidate_codes, _parse_codes, _three_year_start


def main(argv: Sequence[str] | None = None, *, coordinator=None) -> int:
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

    default_as_of = datetime.now(ZoneInfo("Asia/Shanghai"))
    as_of = args.as_of or default_as_of
    report_date = args.report_date or as_of.date().isoformat()
    try:
        report_day = date.fromisoformat(report_date)
    except ValueError:
        parser.error("--date must be an ISO date")
    if args.as_of is None:
        as_of = datetime.combine(report_day, time(22, 30), tzinfo=ZoneInfo("Asia/Shanghai"))
    config_path = args.config.resolve()
    root = config_path.parent.parent if config_path.parent.name == "config" else config_path.parent
    config = load_advisor_config(config_path)
    db_path = resolve_state_db(config, root)
    migrate_database(db_path)

    snapshot = read_collector_snapshot(args.events_db, args.allowed_rids, as_of=as_of)
    explicit_codes = _parse_codes(args.codes)
    candidate_codes = _expanded_candidate_codes(db_path, explicit_codes, snapshot)
    if candidate_codes:
        expected_session = latest_expected_session(as_of)
        registry = ConfiguredProviderRegistry.from_yaml(config_path.with_name("data-sources.yaml"))
        update_market_database(
            db_path,
            registry.historical_provider,
            candidate_codes,
            _three_year_start(expected_session),
            expected_session,
            as_of=as_of,
        )

    active_coordinator = coordinator or coordinator_module.run_review
    result = active_coordinator(
        collector_snapshot=snapshot,
        as_of=as_of,
        report_date=report_date,
        candidate_codes=candidate_codes,
        output_dir=args.output_dir,
        config_path=config_path,
        run_id=args.run_id,
        report_run_id=args.report_run_id,
        rerun_reason=args.rerun_reason,
        supersedes=args.supersedes,
        premarket_run_id=args.premarket_run_id,
    )
    print(json.dumps({
        "json_path": str(result.report_paths.json_path),
        "markdown_path": str(result.report_paths.markdown_path),
        "run_id": result.run_id,
        "status": result.status,
        "warnings": list(result.warnings),
    }, sort_keys=True))
    return 0 if result.status in {"passed", "blocked"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
