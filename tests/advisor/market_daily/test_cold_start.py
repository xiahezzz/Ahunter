from datetime import date, datetime
from zoneinfo import ZoneInfo

from advisor.market_daily.cold_start import ColdStartWorkflow, _calendar_years_before
from advisor.market_daily.contracts import MarketSecurity
from advisor.market_daily.control import MarketDailyControlPlane, MarketDailyRun
from advisor.market_daily.universe import UniverseSnapshot


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI)
TARGET = date(2026, 8, 7)
WINDOW_START = date(2021, 8, 9)
SESSIONS = (WINDOW_START, date(2026, 8, 6), TARGET)


def security(code: str, status: str, listed: date, delisted: date | None = None) -> MarketSecurity:
    return MarketSecurity(
        code=code,
        name=code,
        exchange="SH" if code.startswith("6") else "SZ",
        list_date=listed,
        delist_date=delisted,
        status=status,
        is_st=code == "000001",
        source="fixture_exchange",
        source_at=NOW,
        fetched_at=NOW,
    )


class Sessions:
    def __init__(self) -> None:
        self.refreshes = []

    def refresh(self, start, end, as_of):
        self.refreshes.append((start, end, as_of))

    def latest_completed_session(self, _as_of):
        return TARGET

    def sessions_for(self, start, end):
        return tuple(item for item in SESSIONS if start <= item <= end)


class Universe:
    def __init__(self) -> None:
        self.snapshot = UniverseSnapshot(
            (
                security("000001", "active", date(2000, 1, 1)),
                security("600001", "delisted", date(2000, 1, 1), date(2024, 5, 1)),
                security("300001", "active", date(2026, 8, 8)),
            )
        )

    def refresh(self):
        return self.snapshot


class Engine:
    def __init__(self) -> None:
        self.calls = []

    def execute_run(self, run_id, sessions, owner_id, now):
        self.calls.append((run_id, sessions, owner_id, now))
        return MarketDailyRun(
            run_id, "request", "cold_start", "complete", TARGET, WINDOW_START, TARGET,
            "a" * 64, 2, 2, 0, NOW, NOW, NOW, None,
        )


def test_calendar_year_window_handles_leap_day():
    assert _calendar_years_before(date(2028, 2, 29), 5) == date(2023, 2, 28)


def test_cold_start_intent_is_source_free_idempotent_and_freezes_eligible_universe(tmp_path):
    control = MarketDailyControlPlane(tmp_path / "advisor.sqlite")
    sessions = Sessions()
    engine = Engine()
    workflow = ColdStartWorkflow(control, sessions, Universe(), engine)

    first = workflow.submit(NOW)
    second = workflow.submit(NOW)

    assert first.request_id == second.request_id
    assert first.target_session is None
    assert sessions.refreshes == []
    claimed = control.claim_next_request("service", NOW)
    assert claimed is not None
    plan = workflow.prepare_claimed(claimed, NOW)

    assert plan.target_session == TARGET
    assert plan.start_date == WINDOW_START
    assert tuple(item.code for item in plan.securities) == ("000001", "600001")
    assert plan.run.total_items == 2
    assert control.run_securities(plan.run.run_id) == plan.securities


def test_existing_frozen_run_does_not_adopt_later_universe_changes(tmp_path):
    control = MarketDailyControlPlane(tmp_path / "advisor.sqlite")
    sessions = Sessions()
    universe = Universe()
    workflow = ColdStartWorkflow(control, sessions, universe, Engine())
    claimed = control.claim_next_request("service", NOW) if False else None
    request = workflow.submit(NOW)
    claimed = control.claim_next_request("service", NOW)
    assert claimed is not None
    first = workflow.prepare_claimed(claimed, NOW)
    universe.snapshot = UniverseSnapshot(
        (*universe.snapshot.securities, security("300002", "active", date(2025, 1, 1)))
    )

    second = workflow.prepare_claimed(control.request(request.request_id), NOW)

    assert second.run.run_id == first.run.run_id
    assert tuple(item.code for item in control.run_securities(first.run.run_id)) == ("000001", "600001")


def test_pending_recovery_run_resumes_without_replanning_sources(tmp_path):
    control = MarketDailyControlPlane(tmp_path / "advisor.sqlite")
    sessions = Sessions()
    engine = Engine()
    workflow = ColdStartWorkflow(control, sessions, Universe(), engine)
    request = workflow.submit(NOW)
    claimed = control.claim_next_request("previous", NOW)
    assert claimed is not None
    plan = workflow.prepare_claimed(claimed, NOW)

    resumed = workflow.resume_partial("replacement", NOW)

    assert resumed is not None and resumed.status == "complete"
    assert engine.calls == [(plan.run.run_id, SESSIONS, "replacement", NOW)]
    assert control.request(request.request_id).status == "claimed"
