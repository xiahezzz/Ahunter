"""Operator-only CLI for the persistent Market Daily service."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from advisor.config import load_advisor_config, resolve_state_db
from advisor.market_daily.catch_up import CatchUpWorkflow
from advisor.market_daily.cold_start import ColdStartWorkflow
from advisor.market_daily.control import MarketDailyControlPlane
from advisor.market_daily.engine import MarketDailyEngine
from advisor.market_daily.live_validation import MarketDailyLiveValidator
from advisor.market_daily.preflight import MarketDailyPreflight
from advisor.market_daily.providers import (
    SinaDailyBarProvider,
    SinaIndexSessionProvider,
    SinaUniverseAdapter,
    SingleProvider,
)
from advisor.market_daily.repository import MarketDailyRepository
from advisor.market_daily.service import MarketDailyService
from advisor.market_daily.sessions import ObservedSessionService
from advisor.market_daily.universe import HistoricalUniverseService
from advisor.paths import repo_root


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        database = _database_path(args)
        now = datetime.now(tz=_SHANGHAI)
        if args.command == "cold-start":
            request = MarketDailyControlPlane(database).submit_cold_start_intent(now)
            _emit(
                {
                    "状态": "已提交",
                    "请求编号": request.request_id,
                    "说明": "请求已写入本地队列；Market Daily 服务将在 21:00 后冻结交易日并开始执行。",
                }
            )
            return 0
        if args.command == "status":
            _emit(_status(MarketDailyControlPlane(database), now))
            return 0
        if args.command == "preflight":
            report = _build_live_preflight(database, args.root.expanduser().resolve()).run(now)
            _emit(report.as_payload())
            return 0 if report.passed else 1
        if args.command == "validate":
            report = MarketDailyLiveValidator(database).validate(args.run_id)
            _emit(report.as_payload())
            return 0 if report.passed else 1
        if args.command == "service" and args.service_command == "run":
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s %(levelname)s %(name)s %(message)s",
                stream=sys.stdout,
            )
            service = _build_live_service(database, poll_seconds=args.poll_seconds)
            service.run_forever(max_ticks=1 if args.once else None)
            return 0
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(f"Market Daily 命令失败：{type(error).__name__}: {str(error)[:280]}")
    raise AssertionError("unreachable")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A Hunter Market Daily 本地服务")
    parser.add_argument("--root", type=Path, default=repo_root(), help="项目根目录")
    parser.add_argument("--db", type=Path, help="SQLite 数据库路径；默认读取项目配置")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("cold-start", help="只提交一次五年冷启动请求，不抓取行情")
    commands.add_parser("status", help="只读查看服务租约和最近运行")
    commands.add_parser("preflight", help="只读检查运行环境、数据源与冷启动前提")
    validate = commands.add_parser("validate", help="只读验收已封印的五年冷启动 Run")
    validate.add_argument("--run-id", help="指定冷启动 Run；默认使用最新 Run")
    service = commands.add_parser("service", help="Market Daily 常驻服务")
    service_commands = service.add_subparsers(dest="service_command", required=True)
    run = service_commands.add_parser("run", help="以内部 21:00 时钟常驻运行")
    run.add_argument("--poll-seconds", type=float, default=15.0)
    run.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    return parser


def _database_path(args: argparse.Namespace) -> Path:
    if args.db is not None:
        return args.db.expanduser().resolve()
    root = args.root.expanduser().resolve()
    config_path = root / "config" / "advisor.yaml"
    if config_path.is_file():
        return resolve_state_db(load_advisor_config(config_path), root)
    return root / "data" / "advisor" / "advisor.sqlite"


def _build_live_service(database: Path, *, poll_seconds: float) -> MarketDailyService:
    repository = MarketDailyRepository(database)
    control = MarketDailyControlPlane(database)
    owner_id = f"market-daily-{uuid.uuid4().hex[:12]}"
    sina = SinaDailyBarProvider()
    sessions = ObservedSessionService(
        repository,
        SinaIndexSessionProvider(sina, market="SH"),
        SinaIndexSessionProvider(sina, market="SZ"),
    )
    def heartbeat(owner_id: str) -> None:
        if not control.renew_lease(owner_id, datetime.now(tz=_SHANGHAI), lease_seconds=120):
            raise OSError("Market Daily 服务租约已失效")

    universe = HistoricalUniverseService(
        repository,
        (
            SinaUniverseAdapter(
                sina,
                known_listing_dates=repository.security_listing_dates,
                progress=lambda: heartbeat(owner_id),
            ),
        ),
    )

    engine = MarketDailyEngine(
        repository,
        control,
        SingleProvider(sina),
        sina,
        absences=sina,
        heartbeat=heartbeat,
        clock=lambda: datetime.now(tz=_SHANGHAI),
    )
    cold_start = ColdStartWorkflow(control, sessions, universe, engine)
    catch_up = CatchUpWorkflow(repository, control, sessions, universe, engine)
    return MarketDailyService(
        control,
        cold_start,
        catch_up,
        owner_id=owner_id,
        poll_seconds=poll_seconds,
    )


def _build_live_preflight(database: Path, root: Path) -> MarketDailyPreflight:
    sina = SinaDailyBarProvider()
    universe = SinaUniverseAdapter(sina)
    return MarketDailyPreflight(
        root=root,
        database_path=database,
        sina=sina,
        adjustments=sina,
        sh_sessions=SinaIndexSessionProvider(sina, market="SH"),
        sz_sessions=SinaIndexSessionProvider(sina, market="SZ"),
        universe=universe,
    )


def _status(control: MarketDailyControlPlane, now: datetime) -> dict[str, object]:
    lease = control.lease()
    latest = control.latest_run()
    active = lease is not None and lease[2] > now
    return {
        "服务状态": "运行中" if active else "未运行",
        "租约持有者": lease[0] if active and lease else None,
        "租约到期时间": lease[2].isoformat() if lease else None,
        "最近运行编号": latest.run_id if latest else None,
        "最近运行状态": _run_status_text(latest.status) if latest else "尚无运行",
        "目标交易日": latest.target_session.isoformat() if latest else None,
        "总证券数": latest.total_items if latest else 0,
        "完成数": latest.completed_items if latest else 0,
        "失败数": latest.failed_items if latest else 0,
    }


def _run_status_text(status: str) -> str:
    return {
        "pending": "等待执行",
        "running": "执行中",
        "partial": "部分完成，存在待补洞证券",
        "complete": "已完整完成",
        "failed": "失败",
        "cancelled": "已取消",
    }.get(status, "未知")


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
