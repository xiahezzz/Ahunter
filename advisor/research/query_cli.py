from __future__ import annotations

import argparse
import json
from pathlib import Path

from advisor.research.query import CapsuleQuery


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capsule", type=Path, required=True)
    parser.add_argument("--product", required=True)
    parser.add_argument("--operation", default="get")
    parser.add_argument("--term")
    parser.add_argument("--field")
    parser.add_argument("--equals")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--codes")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--group-by")
    parser.add_argument("--metric")
    parser.add_argument("--value-field")
    parser.add_argument("--direction")
    parser.add_argument("--current-start")
    parser.add_argument("--current-end")
    parser.add_argument("--previous-start")
    parser.add_argument("--previous-end")
    args = parser.parse_args(argv)
    params = {
        key: value
        for key, value in {
            "term": args.term,
            "field": args.field,
            "equals": args.equals,
            "start": args.start,
            "limit": args.limit,
            "codes": args.codes.split(",") if args.codes else None,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "group_by": args.group_by,
            "metric": args.metric,
            "value_field": args.value_field,
            "direction": args.direction,
            "current_start": args.current_start,
            "current_end": args.current_end,
            "previous_start": args.previous_start,
            "previous_end": args.previous_end,
        }.items()
        if value is not None
    }
    result = CapsuleQuery(args.capsule).execute(args.product, args.operation, **params)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
