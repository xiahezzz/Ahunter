from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from advisor.db.migrate import migrate_database
from advisor.market_daily import cli
from advisor.market_daily.control import MarketDailyControlPlane
from advisor.market_daily.preflight import MarketDailyPreflight, MarketDailyPreflightReport, PreflightCheck


NOW = datetime(2026, 8, 7, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class Bars:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure

    def fetch_daily_bars(self, _code: str, _start: date, _end: date) -> tuple[object, ...]:
        if self.failure is not None:
            raise self.failure
        return (object(),)


class FlakyBars:
    def __init__(self) -> None:
        self.calls = 0

    def fetch_daily_bars(self, _code: str, _start: date, _end: date) -> tuple[object, ...]:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary network failure")
        return (object(),)


class Factors:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure

    def fetch_adjustment_factors(self, _code: str, _start: date, _end: date) -> tuple[object, ...]:
        if self.failure is not None:
            raise self.failure
        return (object(),)


class Sessions:
    def observe_sessions(self, _start: date, _end: date) -> tuple[date, ...]:
        return (date(2026, 7, 31),)


class Universe:
    def __init__(self, count: int = 1) -> None:
        self.count = count

    def probe(self) -> dict[str, int]:
        return {"SH": self.count, "SZ": self.count}


def _preflight(tmp_path: Path, *, bars: Bars | None = None) -> tuple[MarketDailyPreflight, Path]:
    root = tmp_path / "project"
    database = root / "data" / "advisor.sqlite"
    runtime_python = root / ".venv-runtime" / "bin" / "python"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_text("", encoding="utf-8")
    (root / "logs").mkdir(parents=True)
    migrate_database(database)
    current_bars = bars or Bars()
    return (
        MarketDailyPreflight(
            root=root,
            database_path=database,
            sina=current_bars,
            adjustments=Factors(),
            sh_sessions=Sessions(),
            sz_sessions=Sessions(),
            universe=Universe(2),
            runtime_python=runtime_python,
            executable=runtime_python,
            disk_usage=lambda _path: SimpleNamespace(free=10 * 1024**3),
            retry_delay_seconds=0,
        ),
        database,
    )


def test_preflight_passes_without_writing_market_facts(tmp_path: Path):
    preflight, database = _preflight(tmp_path)

    report = preflight.run(NOW)

    assert report.passed is True
    assert {check.name for check in report.checks} == {
        "运行环境", "数据库", "磁盘空间", "日志目录", "服务租约", "新浪股票行情", "新浪复权因子", "新浪沪深交易日", "新浪沪深股票池"
    }
    assert all(check.passed for check in report.checks)
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM market_daily").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM market_daily_requests").fetchone()[0] == 0
    finally:
        connection.close()


def test_preflight_reports_source_and_active_lease_failures(tmp_path: Path):
    preflight, database = _preflight(tmp_path, bars=Bars(failure=RuntimeError("offline")))
    MarketDailyControlPlane(database).acquire_lease("other", NOW, lease_seconds=120)

    report = preflight.run(NOW)
    by_name = {check.name: check for check in report.checks}

    assert report.passed is False
    assert by_name["新浪股票行情"].passed is False
    assert by_name["服务租约"].passed is False
    assert "offline" in str(by_name["新浪股票行情"].details["原因"])


def test_preflight_leaves_bounded_http_retries_to_the_sina_adapter(tmp_path: Path):
    preflight, _database = _preflight(tmp_path)
    flaky = FlakyBars()
    preflight._sina = flaky

    report = preflight.run(NOW)
    primary = next(check for check in report.checks if check.name == "新浪股票行情")

    assert report.passed is False
    assert flaky.calls == 1
    assert primary.details["尝试次数"] == 1


def test_preflight_reports_adjustment_factor_failure(tmp_path: Path):
    preflight, _database = _preflight(tmp_path)
    preflight._adjustments = Factors(failure=RuntimeError("factor offline"))

    report = preflight.run(NOW)
    factors = next(check for check in report.checks if check.name == "新浪复权因子")

    assert report.passed is False
    assert factors.passed is False
    assert "factor offline" in str(factors.details["原因"])


def test_preflight_cli_emits_chinese_report_and_failure_exit_code(tmp_path: Path, capsys, monkeypatch):
    class FakePreflight:
        def run(self, _now: datetime) -> MarketDailyPreflightReport:
            return MarketDailyPreflightReport((PreflightCheck("数据源", False, "不可用", {}),))

    monkeypatch.setattr(cli, "_build_live_preflight", lambda *_args: FakePreflight())

    assert cli.main(["--db", str(tmp_path / "advisor.sqlite"), "preflight"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["状态"] == "失败"
    assert payload["检查"][0]["项目"] == "数据源"
