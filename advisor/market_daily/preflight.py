"""Non-destructive readiness checks before a real Market Daily cold start."""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from advisor.market_daily.control import MarketDailyControlPlane


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MIN_FREE_BYTES = 5 * 1024**3
_REQUIRED_TABLES = frozenset(
    {
        "market_daily",
        "market_adjustment_factors",
        "market_daily_absences",
        "trading_sessions",
        "trading_session_observations",
        "market_daily_requests",
        "market_daily_runs",
        "market_daily_run_items",
        "market_daily_service_leases",
    }
)


class _DailyBarProvider(Protocol):
    def fetch_daily_bars(self, code: str, start: date, end: date) -> tuple[object, ...]: ...


class _AdjustmentProvider(Protocol):
    def fetch_adjustment_factors(self, code: str, start: date, end: date) -> tuple[object, ...]: ...


class _SessionProvider(Protocol):
    def observe_sessions(self, start: date, end: date) -> tuple[date, ...]: ...


class _UniverseProvider(Protocol):
    def probe(self) -> dict[str, int]: ...


@dataclass(frozen=True)
class PreflightCheck:
    """One concise, user-facing prerequisite result."""

    name: str
    passed: bool
    message: str
    details: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "项目": self.name,
            "状态": "通过" if self.passed else "失败",
            "说明": self.message,
            "明细": self.details,
        }


@dataclass(frozen=True)
class MarketDailyPreflightReport:
    checks: tuple[PreflightCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def as_payload(self) -> dict[str, object]:
        return {
            "状态": "通过" if self.passed else "失败",
            "检查": [check.as_dict() for check in self.checks],
        }


class MarketDailyPreflight:
    """Verify the actual runtime without inserting, updating, or deleting facts."""

    def __init__(
        self,
        *,
        root: Path,
        database_path: Path,
        sina: _DailyBarProvider,
        adjustments: _AdjustmentProvider,
        sh_sessions: _SessionProvider,
        sz_sessions: _SessionProvider,
        universe: _UniverseProvider,
        runtime_python: Path | None = None,
        executable: Path | None = None,
        disk_usage: Callable[[Path], object] = shutil.disk_usage,
        minimum_free_bytes: int = _MIN_FREE_BYTES,
        retry_delay_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(minimum_free_bytes, int) or isinstance(minimum_free_bytes, bool) or minimum_free_bytes <= 0:
            raise ValueError("最小可用磁盘空间必须为正整数")
        if not isinstance(retry_delay_seconds, (int, float)) or retry_delay_seconds < 0:
            raise ValueError("重试间隔必须为非负数")
        self.root = root.expanduser().resolve()
        self.database_path = database_path.expanduser().resolve()
        self._sina = sina
        self._adjustments = adjustments
        self._sh_sessions = sh_sessions
        self._sz_sessions = sz_sessions
        self._universe = universe
        self._runtime_python = (runtime_python or self.root / ".venv-runtime" / "bin" / "python").expanduser().resolve()
        self._executable = (executable or Path(sys.executable)).expanduser().resolve()
        self._disk_usage = disk_usage
        self._minimum_free_bytes = minimum_free_bytes
        self._retry_delay_seconds = float(retry_delay_seconds)
        self._sleep = sleep

    def run(self, now: datetime) -> MarketDailyPreflightReport:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("预检时间必须带时区")
        current = now.astimezone(_SHANGHAI)
        end = current.date() - timedelta(days=7)
        start = end - timedelta(days=14)
        checks = (
            self._runtime_check(),
            self._database_check(),
            self._disk_check(),
            self._logs_check(),
            self._lease_check(current),
            self._bar_check("新浪股票行情", self._sina, start, end, attempts=1),
            self._factor_check(start, end),
            self._sessions_check(start, end),
            self._universe_check(),
        )
        return MarketDailyPreflightReport(checks)

    def _runtime_check(self) -> PreflightCheck:
        if not self._runtime_python.is_file() or self._runtime_python.is_symlink():
            return PreflightCheck("运行环境", False, "运行环境 Python 不可用", {"路径": str(self._runtime_python)})
        if self._executable != self._runtime_python:
            return PreflightCheck(
                "运行环境",
                False,
                "请从项目 .venv-runtime 运行此命令",
                {"当前": str(self._executable), "应为": str(self._runtime_python)},
            )
        return PreflightCheck("运行环境", True, "正在使用项目独立运行环境", {"路径": str(self._runtime_python)})

    def _database_check(self) -> PreflightCheck:
        if self.database_path.is_symlink() or not self.database_path.is_file():
            return PreflightCheck("数据库", False, "数据库文件不可用", {"路径": str(self.database_path)})
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"{self.database_path.as_uri()}?mode=rw", uri=True, timeout=2)
            integrity = connection.execute("PRAGMA quick_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                return PreflightCheck("数据库", False, "SQLite 完整性检查失败", {"结果": str(integrity)[:240]})
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            missing = sorted(_REQUIRED_TABLES.difference(tables))
            if missing:
                return PreflightCheck("数据库", False, "Market Daily 表尚未就绪", {"缺少表": missing})
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ROLLBACK")
            return PreflightCheck("数据库", True, "可读写且表结构完整", {"路径": str(self.database_path)})
        except (OSError, sqlite3.Error) as error:
            return _failed("数据库", "数据库不可安全读写", error)
        finally:
            if connection is not None:
                connection.close()

    def _disk_check(self) -> PreflightCheck:
        try:
            usage = self._disk_usage(self.root)
            free = int(getattr(usage, "free"))
        except (OSError, TypeError, ValueError, AttributeError) as error:
            return _failed("磁盘空间", "无法读取可用空间", error)
        details = {"可用字节": free, "最低要求字节": self._minimum_free_bytes}
        if free < self._minimum_free_bytes:
            return PreflightCheck("磁盘空间", False, "可用空间不足", details)
        return PreflightCheck("磁盘空间", True, "可用空间满足冷启动下限", details)

    def _logs_check(self) -> PreflightCheck:
        logs = self.root / "logs"
        if not logs.is_dir() or logs.is_symlink() or not os.access(logs, os.W_OK | os.X_OK):
            return PreflightCheck("日志目录", False, "日志目录不可写", {"路径": str(logs)})
        return PreflightCheck("日志目录", True, "日志目录可写", {"路径": str(logs)})

    def _lease_check(self, now: datetime) -> PreflightCheck:
        try:
            lease = MarketDailyControlPlane(self.database_path, ensure_schema=False).lease()
        except (OSError, RuntimeError, sqlite3.Error, ValueError) as error:
            return _failed("服务租约", "无法读取服务租约", error)
        if lease is not None and lease[2] > now:
            return PreflightCheck(
                "服务租约",
                False,
                "已有 Market Daily 实例正在运行",
                {"持有者": lease[0], "到期时间": lease[2].isoformat()},
            )
        return PreflightCheck("服务租约", True, "未观察到活动实例", {})

    def _bar_check(
        self, name: str, provider: _DailyBarProvider, start: date, end: date, *, attempts: int
    ) -> PreflightCheck:
        try:
            bars, used_attempts = _retry(
                lambda: provider.fetch_daily_bars("600000", start, end),
                attempts,
                retry_delay_seconds=self._retry_delay_seconds,
                sleep=self._sleep,
            )
            if not bars:
                return PreflightCheck(name, False, "探测区间没有返回日线", {**_range_details(start, end), "尝试次数": used_attempts})
            return PreflightCheck(
                name,
                True,
                "可返回完整历史日线",
                {**_range_details(start, end), "K线数": len(bars), "尝试次数": used_attempts},
            )
        except Exception as error:
            return _failed(name, "历史日线探测失败", error, {**_range_details(start, end), "尝试次数": attempts})

    def _factor_check(self, start: date, end: date) -> PreflightCheck:
        name = "新浪复权因子"
        try:
            factors = self._adjustments.fetch_adjustment_factors("600000", start, end)
            if not factors:
                return PreflightCheck(name, False, "探测区间没有返回复权因子", _range_details(start, end))
            return PreflightCheck(
                name,
                True,
                "可返回完整复权因子",
                {**_range_details(start, end), "因子数": len(factors), "来源": "新浪"},
            )
        except Exception as error:
            return _failed(name, "复权因子探测失败", error, _range_details(start, end))

    def _sessions_check(self, start: date, end: date) -> PreflightCheck:
        try:
            sh_result, sh_attempts = _retry(
                lambda: self._sh_sessions.observe_sessions(start, end),
                1,
                retry_delay_seconds=self._retry_delay_seconds,
                sleep=self._sleep,
            )
            sz_result, sz_attempts = _retry(
                lambda: self._sz_sessions.observe_sessions(start, end),
                1,
                retry_delay_seconds=self._retry_delay_seconds,
                sleep=self._sleep,
            )
            sh = tuple(sh_result)
            sz = tuple(sz_result)
            if not sh or sh != sz:
                return PreflightCheck(
                    "新浪沪深交易日",
                    False,
                    "新浪上证与深证基准交易日不一致或为空",
                    {**_range_details(start, end), "上证数": len(sh), "深证数": len(sz)},
                )
            return PreflightCheck(
                "新浪沪深交易日",
                True,
                "新浪上证与深证基准交易日一致",
                {
                    **_range_details(start, end),
                    "上证数": len(sh),
                    "上证尝试次数": sh_attempts,
                    "深证数": len(sz),
                    "深证尝试次数": sz_attempts,
                    "最新交易日": sh[-1].isoformat(),
                },
            )
        except Exception as error:
            return _failed("新浪沪深交易日", "交易日探测失败", error, _range_details(start, end))

    def _universe_check(self) -> PreflightCheck:
        try:
            counts, attempts = _retry(
                self._universe.probe,
                2,
                retry_delay_seconds=self._retry_delay_seconds,
                sleep=self._sleep,
            )
            if (
                not isinstance(counts, dict)
                or not isinstance(counts.get("SH"), int)
                or not isinstance(counts.get("SZ"), int)
                or counts["SH"] <= 0
                or counts["SZ"] <= 0
            ):
                return PreflightCheck(
                    "新浪沪深股票池",
                    False,
                    "新浪沪深股票池返回为空或无效",
                    {"结果": counts, "尝试次数": attempts},
                )
            return PreflightCheck(
                "新浪沪深股票池",
                True,
                "新浪上海与深圳 A 股节点可用",
                {"上海": counts["SH"], "深圳": counts["SZ"], "尝试次数": attempts},
            )
        except Exception as error:
            return _failed("新浪沪深股票池", "股票池探测失败", error)


def _range_details(start: date, end: date) -> dict[str, object]:
    return {"开始日期": start.isoformat(), "结束日期": end.isoformat(), "证券": "600000"}


def _failed(name: str, message: str, error: Exception, details: dict[str, object] | None = None) -> PreflightCheck:
    payload = dict(details or {})
    payload["原因"] = f"{type(error).__name__}: {str(error)[:240]}"
    return PreflightCheck(name, False, message, payload)


def _retry(
    action: Callable[[], object],
    attempts: int,
    *,
    retry_delay_seconds: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[object, int]:
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts <= 0:
        raise ValueError("尝试次数必须为正整数")
    if not isinstance(retry_delay_seconds, (int, float)) or retry_delay_seconds < 0:
        raise ValueError("重试间隔必须为非负数")
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return action(), attempt
        except Exception as error:
            last_error = error
            if attempt < attempts and retry_delay_seconds:
                sleep(float(retry_delay_seconds))
    assert last_error is not None
    raise last_error
