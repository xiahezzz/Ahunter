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
from advisor.db.repository import connect
from advisor.evidence.mx_adapter import read_collector_snapshot
from advisor.quality import QualityResult
from advisor.reporting.contracts import validate_run_id
from advisor.reporting.failure import write_failure_report


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
    args = parser.parse_args(argv)

    default_as_of = datetime.now(ZoneInfo("Asia/Shanghai"))
    as_of = args.as_of or default_as_of
    report_date = args.report_date or as_of.date().isoformat()
    try:
        report_day = date.fromisoformat(report_date)
    except ValueError:
        parser.error("--date must be an ISO date")
    try:
        if args.as_of is None:
            as_of = datetime.combine(report_day, time(8, 30), tzinfo=ZoneInfo("Asia/Shanghai"))
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

        active_coordinator = coordinator or coordinator_module.run_premarket
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
        )
        print(json.dumps({
            "json_path": str(result.report_paths.json_path),
            "markdown_path": str(result.report_paths.markdown_path),
            "run_id": result.run_id,
            "status": result.status,
            "warnings": list(result.warnings),
        }, sort_keys=True))
        return 0 if result.status in {"passed", "blocked"} else 1
    except Exception as error:
        run_id = _failure_run_id(args.run_id, report_day)
        payload = {"error": type(error).__name__, "run_id": run_id, "status": "failed"}
        try:
            paths = write_failure_report(
                report_date,
                "premarket",
                [QualityResult("runtime", "blocking", False, type(error).__name__)],
                args.output_dir,
                run_id=run_id,
            )
            payload.update({
                "json_path": str(paths.json_path),
                "markdown_path": str(paths.markdown_path),
            })
        except Exception:
            pass
        print(json.dumps(payload, sort_keys=True))
        return 1


def _failure_run_id(raw_run_id: str | None, report_day: date) -> str:
    if raw_run_id is not None:
        try:
            validate_run_id(raw_run_id)
        except ValueError:
            pass
        else:
            return raw_run_id
    return f"premarket-runtime-failed-{report_day:%Y%m%d}"


def _parse_codes(raw_codes: str | None) -> tuple[str, ...]:
    if raw_codes is None:
        return ()
    parts = raw_codes.split(",")
    if any(part != part.strip() or not part for part in parts):
        raise SystemExit("codes must be comma-separated six-digit values without spaces")
    return tuple(parts)


def _expanded_candidate_codes(db_path: Path, explicit_codes: tuple[str, ...], snapshot) -> tuple[str, ...]:
    connection = connect(db_path)
    try:
        return coordinator_module._expanded_candidate_codes(connection, explicit_codes, snapshot)
    finally:
        connection.close()


def _three_year_start(day: date) -> date:
    try:
        return day.replace(year=day.year - 3)
    except ValueError:
        return day.replace(year=day.year - 3, day=28)


if __name__ == "__main__":
    raise SystemExit(main())
