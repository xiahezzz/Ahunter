from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from advisor.market_daily.catch_up import CatchUpError
from advisor.market_daily.control import MarketDailyControlPlane, MarketDailyRun, RunSecurity
from advisor.market_daily.service import MarketDailyService
from advisor.market_daily.sessions import SessionObservationError


SHANGHAI = ZoneInfo("Asia/Shanghai")
BEFORE = datetime(2026, 8, 7, 20, 59, 59, tzinfo=SHANGHAI)
AFTER = datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI)


class Cold:
    def __init__(self, run=None):
        self.resume_calls = 0
        self.execute_calls = []
        self.run = run

    def resume_partial(self, owner, now):
        self.resume_calls += 1
        return self.run

    def execute_claimed(self, request, owner, now):
        self.execute_calls.append((request, owner, now))
        return self.run or fake_run("cold_start")


class Catch:
    def __init__(self, request=None):
        self.request = request
        self.resume_calls = 0
        self.submit_calls = 0

    def resume_partial(self, owner, now):
        self.resume_calls += 1
        return None

    def submit_due(self, now):
        self.submit_calls += 1
        if self.request is None:
            raise CatchUpError("冷启动基线尚未完整，不能执行日常补洞")
        return self.request

    def execute_claimed(self, request, owner, now):
        return fake_run("catch_up")


def fake_run(run_type, *, status="complete"):
    return MarketDailyRun(
        "run", "request", run_type, status, date(2026, 8, 7), date(2021, 8, 9),
        date(2026, 8, 7), "a" * 64, 1, 1, 0, AFTER, AFTER, AFTER, None,
    )


def service(tmp_path, now, cold=None, catch=None, owner="one"):
    control = MarketDailyControlPlane(tmp_path / "advisor.sqlite")
    return control, MarketDailyService(
        control, cold or Cold(), catch or Catch(), owner_id=owner, clock=lambda: now, sleep=lambda _seconds: None
    )


def test_before_2100_waits_without_claiming_or_calling_workflows(tmp_path):
    cold = Cold()
    catch = Catch()
    control, running = service(tmp_path, BEFORE, cold, catch)
    control.submit_cold_start_intent(BEFORE)

    result = running.tick()

    assert result.action == "before_21_00"
    assert control.pending_request_count() == 1
    assert cold.resume_calls == 0
    assert catch.submit_calls == 0


def test_before_2100_resumes_a_run_that_was_already_frozen(tmp_path):
    cold = Cold(run=fake_run("cold_start"))
    catch = Catch()
    control, running = service(tmp_path, BEFORE, cold, catch)
    request = control.submit_cold_start(date(2026, 8, 7), date(2021, 8, 9), AFTER)
    claimed = control.claim_next_request("previous", AFTER)
    assert claimed is not None
    control.create_run(
        request.request_id,
        "a" * 64,
        (RunSecurity("600001", "fixture", "SH", date(2000, 1, 1), None, "active"),),
        AFTER,
    )

    result = running.tick()

    assert result.action == "resumed_cold_start"
    assert result.status == "complete"
    assert cold.resume_calls == 1
    assert catch.submit_calls == 0


def test_two_instances_allow_exactly_one_lease_holder(tmp_path):
    control, first = service(tmp_path, AFTER, owner="first")
    second = MarketDailyService(control, Cold(), Catch(), owner_id="second", clock=lambda: AFTER)

    one = first.tick()
    two = second.tick()

    assert one.status == "waiting_for_cold_start"
    assert two.status == "standby"


def test_partial_cold_start_has_priority_over_catch_up(tmp_path):
    cold = Cold(run=fake_run("cold_start"))
    catch = Catch()
    _control, running = service(tmp_path, AFTER, cold, catch)

    result = running.tick()

    assert result.action == "resumed_cold_start"
    assert catch.submit_calls == 0


def test_partial_cold_start_is_retried_at_most_once_per_service_day(tmp_path):
    clock = {"now": AFTER}
    cold = Cold(run=fake_run("cold_start", status="partial"))
    catch = Catch()
    control = MarketDailyControlPlane(tmp_path / "advisor.sqlite")
    running = MarketDailyService(
        control,
        cold,
        catch,
        owner_id="bounded-partial-retry",
        clock=lambda: clock["now"],
        sleep=lambda _seconds: None,
    )

    first = running.tick()
    for offset in (15, 30, 45):
        clock["now"] = AFTER + timedelta(seconds=offset)
        running.tick()

    assert first.action == "resumed_cold_start"
    assert first.status == "partial"
    assert cold.resume_calls == 1

    clock["now"] = AFTER + timedelta(days=1)
    next_day = running.tick()

    assert next_day.action == "resumed_cold_start"
    assert cold.resume_calls == 2


def test_crossing_2100_submits_automatic_catch_up_only_once_per_service_day(tmp_path):
    class DueCatch:
        def __init__(self):
            self.submit_calls = 0

        def resume_partial(self, _owner, _now):
            return None

        def submit_due(self, _now):
            self.submit_calls += 1
            return None

        def execute_claimed(self, _request, _owner, _now):
            raise AssertionError("no request should be executed")

    control = MarketDailyControlPlane(tmp_path / "advisor.sqlite")
    clock = {"now": BEFORE}
    catch = DueCatch()
    cold = Cold()
    running = MarketDailyService(control, cold, catch, owner_id="one", clock=lambda: clock["now"])

    assert running.tick().action == "before_21_00"
    clock["now"] = AFTER
    assert running.tick().action == "up_to_date"
    clock["now"] = AFTER + timedelta(seconds=15)
    running.tick()

    assert catch.submit_calls == 1

    request = control.submit_cold_start_intent(clock["now"])
    clock["now"] = AFTER + timedelta(seconds=30)
    result = running.tick()

    assert result.action == "executed_cold_start"
    assert result.request_id == request.request_id
    assert len(cold.execute_calls) == 1


def test_repeated_source_failures_back_off_between_service_ticks(tmp_path):
    class UnavailableCold(Cold):
        def execute_claimed(self, _request, _owner, _now):
            raise SessionObservationError("source unavailable")

    control = MarketDailyControlPlane(tmp_path / "advisor.sqlite")
    control.submit_cold_start_intent(AFTER)
    sleeps: list[float] = []
    running = MarketDailyService(
        control,
        UnavailableCold(),
        Catch(),
        owner_id="backoff",
        clock=lambda: AFTER,
        sleep=sleeps.append,
        poll_seconds=15,
    )

    running.run_forever(max_ticks=4)

    assert sleeps == [15.0, 30.0, 60.0]
