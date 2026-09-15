"""Launchd-facing entry point for the self-contained Research Engine."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from advisor.research.cli import main as research_main


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run A Hunter's Research Engine batch.")
    parser.add_argument("--as-of", type=datetime.fromisoformat)
    parser.add_argument("--date", "--report-date", dest="report_date")
    parser.add_argument("--codes")
    parser.add_argument("--team", action="append")
    parser.add_argument("--events-db", type=Path, default=Path("data/state/events.sqlite"))
    parser.add_argument("--allowed-rids", type=Path, default=Path("config/allowed-rids.yaml"))
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument("--batch-id")
    args = parser.parse_args(argv)

    delegated = ["batch"]
    if args.codes:
        delegated.extend(("--codes", args.codes))
    if args.as_of is not None:
        delegated.extend(("--as-of", args.as_of.isoformat()))
    if args.report_date is not None:
        delegated.extend(("--date", args.report_date))
    if args.team:
        for team in args.team:
            delegated.extend(("--team", team))
    delegated.extend(("--events-db", str(args.events_db), "--allowed-rids", str(args.allowed_rids)))
    delegated.extend(("--output-dir", str(args.output_dir)))
    if args.batch_id is not None:
        delegated.extend(("--batch-id", args.batch_id))
    return research_main(delegated)


if __name__ == "__main__":
    raise SystemExit(main())
