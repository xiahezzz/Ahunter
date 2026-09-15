from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from advisor.market_daily.contracts import (
    AdjustmentFactor,
    CanonicalDailyBar,
    MarketSecurity,
    ObservedTradingSession,
)
from advisor.market_daily.control import MarketDailyControlPlane, RunSecurity
from advisor.market_daily.live_validation import MarketDailyLiveValidator
from advisor.market_daily.repository import MarketDailyRepository


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI)
SESSIONS = (date(2026, 8, 6), date(2026, 8, 7))


def _complete_run(database: Path) -> str:
    repository = MarketDailyRepository(database)
    control = MarketDailyControlPlane(database)
    security = MarketSecurity(
        "600519", "贵州茅台", "SH", date(2001, 8, 27), None, "active", False,
        "fixture_exchange", NOW, NOW,
    )
    repository.upsert_security(security)
    for session in SESSIONS:
        repository.upsert_session(
            ObservedTradingSession(session, "fixture_primary", "fixture_fallback", NOW, NOW, NOW)
        )
    request = control.submit_cold_start(SESSIONS[-1], SESSIONS[0], NOW)
    claimed = control.claim_next_request("fixture-service", NOW)
    assert claimed is not None
    run = control.create_run(
        request.request_id,
        "a" * 64,
        (RunSecurity("600519", "贵州茅台", "SH", date(2001, 8, 27), None, "active"),),
        NOW,
    )
    control.start_run(run.run_id, NOW)
    for session in SESSIONS:
        repository.insert_bar(
            CanonicalDailyBar("600519", session, 100, 102, 99, 101, 1_000, 101_000, "fixture", NOW, NOW)
        )
        repository.upsert_factor(
            AdjustmentFactor("600519", session, 1.0, "fixture", NOW, NOW, "fixture@1")
        )
    control.mark_item(run.run_id, "600519", "completed", NOW, selected_source="fixture")
    control.finalize_run(run.run_id, NOW)
    return run.run_id


def test_live_validator_proves_a_complete_run_without_writing_any_market_fact(tmp_path: Path):
    database = tmp_path / "advisor.sqlite"
    run_id = _complete_run(database)

    report = MarketDailyLiveValidator(database).validate()

    assert report.passed is True
    assert report.run_id == run_id
    assert {check.name for check in report.checks} == {
        "运行状态", "股票池范围", "交易日覆盖", "K线契约", "复权因子",
    }
    assert report.as_payload()["状态"] == "通过"
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM market_daily").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM market_adjustment_factors").fetchone()[0] == 2
    finally:
        connection.close()


def test_live_validator_fails_closed_when_a_completed_run_lacks_a_factor(tmp_path: Path):
    database = tmp_path / "advisor.sqlite"
    _complete_run(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "DELETE FROM market_adjustment_factors WHERE code = ? AND trade_date = ?",
            ("600519", SESSIONS[0].isoformat()),
        )
        connection.commit()
    finally:
        connection.close()

    report = MarketDailyLiveValidator(database).validate()
    factors = next(check for check in report.checks if check.name == "复权因子")

    assert report.passed is False
    assert factors.passed is False
    assert factors.details["缺失因子"] == 1
