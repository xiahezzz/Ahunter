from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from advisor.market_daily.control import MarketDailyControlPlane, MarketDailyRun, RunSecurity
from advisor.market_daily.service import MarketDailyService


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI)


class CrashBeforeRun:
    def resume_partial(self, _owner, _now):
        return None

    def execute_claimed(self, _request, _owner, _now):
        raise KeyboardInterrupt()


class CompleteCold:
    def __init__(self, control: MarketDailyControlPlane) -> None:
        self.control = control

    def resume_partial(self, _owner, _now):
        return None

    def execute_claimed(self, request, _owner, now):
        frozen = self.control.configure_claimed_request(
            request.request_id, date(2026, 8, 7), date(2026, 8, 7), date(2026, 8, 7), now
        )
        run = self.control.create_run(
            frozen.request_id,
            "a" * 64,
            (RunSecurity("600001", "fixture", "SH", date(2000, 1, 1), None, "active"),),
            now,
        )
        self.control.start_run(run.run_id, now)
        self.control.mark_item(run.run_id, "600001", "completed", now, selected_source="fixture")
        return self.control.finalize_run(run.run_id, now)


class NoCatch:
    def resume_partial(self, _owner, _now):
        return None

    def submit_due(self, _now):
        return None

    def execute_claimed(self, _request, _owner, _now):
        return None


def test_restart_recovers_request_claimed_before_run_creation(tmp_path):
    control = MarketDailyControlPlane(tmp_path / "advisor.sqlite")
    request = control.submit_cold_start_intent(NOW)
    crashed = MarketDailyService(
        control, CrashBeforeRun(), NoCatch(), owner_id="first", clock=lambda: NOW, lease_seconds=30
    )

    with pytest.raises(KeyboardInterrupt):
        crashed.tick()
    assert control.request(request.request_id).status == "claimed"

    resumed_at = NOW + timedelta(hours=3, seconds=31)
    recovered = MarketDailyService(
        control, CompleteCold(control), NoCatch(), owner_id="second", clock=lambda: resumed_at, lease_seconds=30
    ).tick()

    assert recovered.recovered_runs == 1
    assert recovered.action == "executed_cold_start"
    run = control.run_for_request(request.request_id)
    assert run is not None and run.status == "complete"


def test_graceful_stop_releases_only_its_own_lease(tmp_path):
    control = MarketDailyControlPlane(tmp_path / "advisor.sqlite")
    before_due = NOW.replace(hour=20, minute=59, second=0)
    holder: dict[str, MarketDailyService] = {}

    def stop_after_first_sleep(_seconds: float) -> None:
        holder["service"].request_stop()

    service = MarketDailyService(
        control,
        CompleteCold(control),
        NoCatch(),
        owner_id="graceful",
        clock=lambda: before_due,
        sleep=stop_after_first_sleep,
    )
    holder["service"] = service

    service.run_forever(max_ticks=2)

    assert control.lease() is None
