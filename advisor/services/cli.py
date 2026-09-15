"""CLI for the explicit, two-service A Hunter service set."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from advisor.config import load_advisor_config, resolve_state_db
from advisor.paths import repo_root
from advisor.services.manager import ServiceSetManager, statuses_as_dict


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A Hunter 服务状态与服务集运维命令")
    parser.add_argument("--root", type=Path, default=repo_root())
    parser.add_argument("--db", type=Path)
    parser.add_argument("--launch-agents-dir", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="只读查看所有服务状态")
    for name in ("install", "load", "unload", "start", "stop"):
        action = commands.add_parser(name)
        action.add_argument("service", choices=("market-daily", "mx-listener", "research"))
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    database = args.db.expanduser().resolve() if args.db else resolve_state_db(load_advisor_config(root / "config" / "advisor.yaml"), root)
    manager = ServiceSetManager(root=root, database_path=database, launch_agents_dir=args.launch_agents_dir)
    try:
        if args.command == "status":
            print(json.dumps(statuses_as_dict(manager.statuses()), ensure_ascii=False, sort_keys=True))
            return 0
        method = getattr(manager, f"{args.command}_{args.service.replace('-', '_')}")
        changed = method()
        print(json.dumps({"状态": "已执行" if changed else "无需变更", "服务": args.service}, ensure_ascii=False))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        parser.error(f"服务命令失败：{type(error).__name__}: {str(error)[:280]}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
