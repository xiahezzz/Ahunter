from datetime import date, datetime
from zoneinfo import ZoneInfo

from advisor.market_daily.contracts import AdjustmentFactor, CanonicalDailyBar
from advisor.market_daily.control import MarketDailyControlPlane, RunSecurity
from advisor.market_daily.readiness import MarketDailyReadiness
from advisor.market_daily.repository import MarketDailyRepository


NOW = datetime(2026, 8, 7, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
SESSIONS = (date(2026, 8, 6), date(2026, 8, 7))


def bar(trade_date):
    return CanonicalDailyBar("600519", trade_date, 10, 10, 10, 10, 100, 1000, "fixture", NOW, NOW)


def test_security_readiness_can_pass_while_market_scope_is_blocked(tmp_path):
    repository = MarketDailyRepository(tmp_path / "advisor.sqlite")
    control = MarketDailyControlPlane(repository.database_path)
    readiness = MarketDailyReadiness(repository, control)
    security = RunSecurity("600519", "贵州茅台", "SH", date(2001, 1, 1), None, "active")
    missing = RunSecurity("000001", "平安银行", "SZ", date(1991, 4, 3), None, "active")
    for session in SESSIONS:
        repository.insert_bar(bar(session))
        repository.upsert_factor(
            AdjustmentFactor("600519", session, 1.0, "fixture", NOW, NOW, "fixture@1")
        )
    request = control.submit_cold_start(SESSIONS[-1], SESSIONS[0], NOW)
    claimed = control.claim_next_request("fixture-service", NOW)
    assert claimed is not None
    run = control.create_run(request.request_id, "a" * 64, (security, missing), NOW)
    control.start_run(run.run_id, NOW)
    control.mark_item(run.run_id, security.code, "completed", NOW)
    control.mark_item(run.run_id, missing.code, "source_missing", NOW, error="fixture source unavailable")
    control.finalize_run(run.run_id, NOW)

    individual = readiness.security(security, SESSIONS, SESSIONS[0], SESSIONS[-1])
    market = readiness.market(SESSIONS[-1])

    assert individual.ready is True
    assert market.ready is False
    assert market.run_id == run.run_id
    assert market.reason == "全市场运行尚未完整"
