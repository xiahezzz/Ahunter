from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from advisor.market_daily.catch_up import CatchUpError, CatchUpWorkflow
from advisor.market_daily.contracts import CanonicalDailyBar, MarketSecurity
from advisor.market_daily.control import MarketDailyControlPlane, RunSecurity
from advisor.market_daily.repository import MarketDailyRepository
from advisor.market_daily.universe import UniverseSnapshot


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI)
SESSIONS = (date(2026, 8, 5), date(2026, 8, 6), date(2026, 8, 7))


def security(code, listed, *, status="active", delisted=None):
    return MarketSecurity(
        code, code, "SH" if code.startswith("6") else "SZ", listed, delisted, status,
        False, "fixture_exchange", NOW, NOW,
    )


def bar(code, trade_date):
    return CanonicalDailyBar(code, trade_date, 10, 10, 10, 10, 100, 1000, "fixture", NOW, NOW)


class Sessions:
    def __init__(self):
        self.calls = []

    def refresh(self, start, end, now):
        self.calls.append((start, end, now))

    def latest_completed_session(self, _now):
        return SESSIONS[-1]

    def sessions_for(self, start, end):
        return tuple(item for item in SESSIONS if start <= item <= end)


class Universe:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def refresh(self):
        return self.snapshot


class Engine:
    def __init__(self):
        self.calls = []

    def execute_run(self, run_id, sessions, owner_id, now):
        self.calls.append((run_id, sessions, owner_id, now))
        return None


def complete_cold_start(control, securities):
    request = control.submit_cold_start(SESSIONS[-1], SESSIONS[0], NOW)
    claimed = control.claim_next_request("service", NOW)
    assert claimed
    run = control.create_run(claimed.request_id, "a" * 64, securities, NOW)
    control.start_run(run.run_id, NOW)
    for item in securities:
        control.mark_item(run.run_id, item.code, "completed", NOW)
    return control.finalize_run(run.run_id, NOW)


def test_catch_up_requires_a_complete_cold_start(tmp_path):
    repository = MarketDailyRepository(tmp_path / "advisor.sqlite")
    control = MarketDailyControlPlane(repository.database_path)
    workflow = CatchUpWorkflow(repository, control, Sessions(), Universe(UniverseSnapshot((security("600001", SESSIONS[0]),))), Engine())

    with pytest.raises(CatchUpError, match="冷启动"):
        workflow.plan_due(NOW)


def test_catch_up_plans_only_missing_per_security_intervals_and_is_idempotent(tmp_path):
    repository = MarketDailyRepository(tmp_path / "advisor.sqlite")
    control = MarketDailyControlPlane(repository.database_path)
    all_securities = (
        security("600001", SESSIONS[0]),
        security("000001", SESSIONS[1]),
        security("600002", SESSIONS[0], status="delisted", delisted=SESSIONS[1]),
    )
    complete_cold_start(
        control,
        tuple(
            RunSecurity(item.code, item.name, item.exchange, item.list_date, item.delist_date, item.status)
            for item in all_securities
        ),
    )
    repository.insert_bar(bar("600001", SESSIONS[0]))
    repository.insert_bar(bar("600002", SESSIONS[0]))
    repository.insert_bar(bar("600002", SESSIONS[1]))
    sessions = Sessions()
    workflow = CatchUpWorkflow(repository, control, sessions, Universe(UniverseSnapshot(all_securities)), Engine())

    plan = workflow.plan_due(NOW)
    assert plan is not None
    assert plan.item_ranges == {
        "000001": (SESSIONS[1], SESSIONS[-1]),
        "600001": (SESSIONS[1], SESSIONS[-1]),
    }
    first = workflow.submit_due(NOW)
    second = workflow.submit_due(NOW)
    assert first is not None and second is not None
    assert first.request_id == second.request_id
    assert sessions.calls


def test_already_covered_market_is_a_true_noop_without_request(tmp_path):
    repository = MarketDailyRepository(tmp_path / "advisor.sqlite")
    control = MarketDailyControlPlane(repository.database_path)
    item = security("600001", SESSIONS[0])
    complete_cold_start(control, (RunSecurity(item.code, item.name, item.exchange, item.list_date, None, item.status),))
    for session in SESSIONS:
        repository.insert_bar(bar("600001", session))
    workflow = CatchUpWorkflow(repository, control, Sessions(), Universe(UniverseSnapshot((item,))), Engine())

    assert workflow.plan_due(NOW) is None
    assert workflow.submit_due(NOW) is None
