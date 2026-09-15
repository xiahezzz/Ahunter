from dataclasses import replace
from datetime import datetime, timedelta, date
import json

import pytest

from advisor.research.experiments.clock import PhaseClock, phase_schedule
from advisor.research.experiments.data.access import HistoricalQueries, Product
from advisor.research.experiments.data.temporal import Availability
from advisor.research.experiments.records import Fenced
from advisor.research.experiments.repository import Conflict
from advisor.research.experiments.timing import submission_timing
from tests.advisor.research.test_experiment_registration import registry, register_plan
from tests.advisor.research.test_experiment_bundles import bundle


@pytest.fixture
def running(registry):
    service, _, definition, _, real = registry
    test = register_plan(registry)["tests"][0]
    records = service.records
    test_id = test["record_id"]
    records.transition(test_id, "preflight", action_id="fixture-preflight")
    records.transition(test_id, "queued", action_id="fixture-queue")
    lease = records.claim(test_id, worker_id="worker-1", lease_seconds=10000)
    records.transition(test_id, "running", action_id="fixture-start", lease=lease)
    yield PhaseClock(records, lease), real, definition


def empty():
    return {"items": [], "required_items": []}


def active(clock, *, key="initial"):
    clock.create(action_id="create")
    clock.activate(empty(), action_id="open-" + key)
    return clock.session("main")


def finish(clock, key, reason="completed"):
    clock.begin_close(reason, action_id="closing-" + key)
    clock.finish_close(action_id="closed-" + key)
    return clock.advance(action_id="advance-" + key)


def evidence(clock, phase, *, late=False, future=False, quality="historical_publication_declared"):
    payload = {"known": "auction fixture"}
    artifact = clock.records.artifacts.put_json(payload)
    return {"item_id": "auction-result", "product_ref": "auction-results", "content_hash": artifact.content_hash,
            "availability": Availability(event_at=phase.boundary.event_cutoff + timedelta(seconds=1 if future else 0),
                available_at=phase.boundary.snapshot_at + timedelta(seconds=1 if late else 0),
                fetched_at=clock.records._now(), version_public_at=None, time_quality=quality).model_dump(mode="json")}


def test_schedule_is_bound_to_resolved_dates_and_has_no_continuous_model_phase(running):
    clock, _, definition = running
    schedule = phase_schedule(definition["value"]["specification"], "august-tuning")
    assert len(schedule) == 16
    assert [p.trading_date for p in schedule if p.kind == "auction"] == [date(2026, 8, d) for d in range(3, 8)]
    assert schedule[0].boundary.snapshot_at.isoformat() == "2026-08-02T23:05:00+08:00"
    assert schedule[2].boundary.event_cutoff.isoformat() == "2026-08-03T09:25:00+08:00"
    assert schedule[2].boundary.snapshot_at.isoformat() == "2026-08-03T09:25:05+08:00"
    assert schedule[2].plan_received_at.isoformat() == "2026-08-03T09:29:00+08:00"
    assert all(p.boundary.snapshot_at.hour < 10 or p.boundary.snapshot_at.hour == 23 for p in schedule)
    assert schedule[-1].plan_received_at is None and schedule[-1].valuation_trade_date == date(2026, 8, 7)


def test_real_midnight_changes_elapsed_time_only_and_closing_fences_children(running):
    clock, real, _ = running
    clock.records.renew(clock.lease, lease_seconds=90000)
    real[0] = real[0].replace(hour=23, minute=59)
    parent = active(clock)
    child = parent.child("child")
    before = clock.observe(parent.scope)
    real[0] += timedelta(minutes=2)
    later = clock.observe(child.scope)
    assert later["phase"] == before["phase"] and later["snapshot"] == before["snapshot"]
    assert later["actual_elapsed_seconds"] == 120
    closing = clock.begin_close("completed", action_id="finish")
    with pytest.raises(Fenced): child.check()
    with pytest.raises(Fenced): clock.observe(parent.scope)
    assert clock.begin_close("completed", action_id="finish") == closing
    clock.finish_close(action_id="closed")
    clock.advance(action_id="advance")
    assert clock._state()["value"]["status"] == "pending"
    assert clock.schedule[clock._state()["value"]["index"]].kind == "auction"


def test_query_commit_rechecks_durable_phase_state_and_strips_late_provider_output(running):
    clock, *_ = running
    session = active(clock)
    def close_during_read(_):
        clock.begin_close("completed", action_id="closing")
        return []
    queries = HistoricalQueries(session, [Product("fixture", "v1", False, close_during_read)])
    with pytest.raises(Fenced):
        queries.query({"product": "fixture", "start": "2026-08-02", "end": "2026-08-02", "limit": 2}, action_id="late-query")
    assert clock.records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='query'").fetchone()[0] == 0


@pytest.mark.parametrize("cause", ["late", "future", "missing"])
def test_required_auction_snapshot_failure_never_moves_the_cutoff(running, cause):
    clock, *_ = running
    active(clock)
    finish(clock, "initial")
    clock.activate(empty(), action_id="open-auction")
    finish(clock, "auction")
    phase = clock.schedule[2]
    supplied = {"items": [] if cause == "missing" else [evidence(clock, phase, late=cause == "late", future=cause == "future")],
                "required_items": ["auction-result"]}
    clock.activate(supplied, action_id="postauction")
    state = clock._state()["value"]
    assert state["blocked"] and state["status"] == "closing" and state["index"] == 2
    with pytest.raises(Fenced): clock.session("main")
    clock.finish_close(action_id="quality-close")
    with pytest.raises(Conflict): clock.advance(action_id="quality-advance")
    snapshot = clock.records.read(state["snapshot_id"])
    body = json.loads(clock.records.artifacts.read_bytes(snapshot["value"]["snapshot_hash"]))
    assert body["boundary"] == phase.boundary.model_dump(mode="json") and body["items"] == []


def test_valid_delayed_auction_and_receipts_are_frozen_and_not_replaced(running):
    clock, *_ = running
    active(clock)
    finish(clock, "initial")
    clock.activate(empty(), action_id="auction")
    finish(clock, "auction")
    phase = clock.schedule[2]
    supplied = {"items": [evidence(clock, phase)], "required_items": ["auction-result"]}
    opened = clock.activate(supplied, action_id="postauction")
    session = clock.session("main")
    observed = clock.observe(session.scope)["snapshot"]["items"][0]
    assert observed["item_id"] == "auction-result" and observed["value"] == {"known": "auction fixture"}
    assert clock.activate(supplied, action_id="postauction") == opened
    with pytest.raises(Conflict): clock.activate(empty(), action_id="postauction")
    altered = replace(session.scope, boundary=session.scope.boundary.model_copy(update={"event_cutoff": phase.boundary.snapshot_at}))
    with pytest.raises(Fenced): clock.assert_scope(altered)


def test_timeout_stops_permissions_without_advancing_simulated_clock(running):
    clock, real, _ = running
    session = active(clock)
    initial = clock._state()["value"]
    with pytest.raises(Conflict): clock.begin_close("phase_timeout", action_id="early-timeout")
    real[0] += timedelta(seconds=clock.spec.runtime.phase_wall_timeout_seconds)
    with pytest.raises(Fenced): session.check()
    clock.begin_close("phase_timeout", action_id="timeout")
    timed = clock._state()["value"]
    assert timed["index"] == initial["index"] and timed["deadline_at"] == initial["deadline_at"]
    assert timed["close_reason"] == "phase_timeout"
    clock.finish_close(action_id="closed")
    clock.advance(action_id="advance")
    assert clock._state()["value"]["index"] == 1


def test_worker_recovery_retains_snapshot_deadline_and_revokes_old_worker(running):
    clock, real, _ = running
    old = active(clock)
    before = clock._state()["value"]
    clock.records.renew(clock.lease, lease_seconds=1)
    real[0] += timedelta(seconds=2)
    lease = clock.records.claim(clock.lease.test_id, worker_id="worker-2", lease_seconds=10000)
    recovered = PhaseClock(clock.records, lease)
    with pytest.raises(Fenced): old.check()
    with pytest.raises(Fenced): recovered.session("main")
    recovered.resume(action_id="resume")
    state = recovered._state()["value"]
    assert state["deadline_at"] == before["deadline_at"] and state["snapshot_id"] == before["snapshot_id"]
    assert recovered.observe(recovered.session("main").scope)["phase"] == clock.schedule[0].model_dump(mode="json")
    with pytest.raises(Fenced): clock.begin_close("completed", action_id="old-close")


@pytest.mark.parametrize("point", ["before_event", "after_event", "after_projection", "before_commit", "after_commit"])
def test_activation_crash_recovery_keeps_a_single_frozen_snapshot(running, point):
    clock, *_ = running
    clock.create(action_id="create")
    hits = [0]
    def fail(actual):
        # before/after_commit also run during snapshot preparation; both boundaries
        # may be interrupted and retry must acknowledge any already sealed fact.
        if actual == point:
            hits[0] += 1
            raise KeyboardInterrupt("fixture interruption")
    clock.records.fault = fail
    with pytest.raises(KeyboardInterrupt): clock.activate(empty(), action_id="open")
    clock.records.fault = lambda _: None
    result = clock.activate(empty(), action_id="open")
    assert clock.activate(empty(), action_id="open") == result
    assert clock._state()["value"]["status"] == "active"
    assert clock.records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='snapshot'").fetchone()[0] == 1


def test_projection_rebuild_restores_same_clock_and_snapshot(running):
    clock, *_ = running
    session = active(clock)
    first = clock.observe(session.scope)
    clock.records.db.execute("DELETE FROM lagent_projections WHERE test_id=? AND name='phase_clock'", (clock.lease.test_id,))
    clock.records.db.commit()
    with pytest.raises(Conflict): session.check()
    clock.records.rebuild(clock.lease)
    assert clock.observe(session.scope) == first


def test_budget_exhaustion_replays_remaining_schedule_without_reactivating_research(running):
    clock, *_ = running
    active(clock)
    finish(clock, "initial", reason="budget_exhausted")
    for index in range(1, len(clock.schedule)):
        clock.activate(empty(), action_id=f"skip-{index}")
        with pytest.raises(Fenced): clock.session("main")
        assert clock._state()["value"]["close_reason"] == "budget_exhausted"
        clock.finish_close(action_id=f"close-{index}")
        clock.advance(action_id=f"advance-{index}")
    assert clock._state()["value"]["status"] == "finished"
    assert clock._state()["value"]["valuation_trade_date"] == "2026-08-07"
    assert clock.records.status(clock.lease.test_id) == "running"  # Episode evaluation belongs to LE-010.
    assert clock.records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind IN ('ledger','evaluation')").fetchone()[0] == 0


@pytest.mark.parametrize("reason", ["cancelled", "platform_failure"])
def test_host_stop_cannot_be_misread_as_candidate_failure_and_continue(running, reason):
    clock, *_ = running
    active(clock)
    clock.begin_close(reason, action_id="stop")
    clock.finish_close(action_id="closed")
    with pytest.raises(Conflict): clock.advance(action_id="advance")


def test_ingress_waits_for_market_acceptance_and_cannot_backfill_opening_auction(running, bundle):
    clock, *_ = running
    rule = dict(bundle[4]["rules"][0]["payload"])
    rule["accept_orders"] = [{"start": "09:15:00", "end": "09:25:00"}, {"start": "09:30:00", "end": "15:00:00"}]
    pre = submission_timing(clock.schedule[1], rule, order_latency_ms=100, receipt_latency_ms=100, operation="submit")
    assert pre["exchange_accepted_at"] == "2026-08-03T09:24:30.100000+08:00"
    assert pre["receipt_available_at"] == "2026-08-03T09:24:30.200000+08:00" and pre["can_participate_opening_auction"]
    post = submission_timing(clock.schedule[2], rule, order_latency_ms=100, receipt_latency_ms=100, operation="submit")
    assert post["platform_received_at"] == "2026-08-03T09:29:00+08:00"
    assert post["exchange_accepted_at"] == "2026-08-03T09:30:00.100000+08:00" and not post["can_participate_opening_auction"]
    cancel = submission_timing(clock.schedule[1], rule, order_latency_ms=100, receipt_latency_ms=100, operation="cancel")
    assert cancel["status"] == "rejected" and cancel["exchange_accepted_at"] is None
    assert submission_timing(clock.schedule[0], rule, order_latency_ms=100, receipt_latency_ms=100, operation="submit")["code"] == "phase_does_not_accept_plans"
    rule["accept_orders"] = [{"start": "09:15:00", "end": "09:25:00"}]
    assert submission_timing(clock.schedule[2], rule, order_latency_ms=100, receipt_latency_ms=100, operation="submit")["code"] == "no_market_acceptance_window"


def test_late_callback_is_audited_by_current_owner_without_releasing_its_content(running):
    from advisor.research.experiments.resolution import digest
    clock, *_ = running
    session = active(clock)
    output_hash = digest({"untrusted": "late plan must not become next phase memory"})
    with pytest.raises(Conflict): clock.discard_late(session.scope, actor_id="main", callback_id="c1", output_hash=output_hash)
    clock.begin_close("completed", action_id="closing")
    first = clock.discard_late(session.scope, actor_id="main", callback_id="c1", output_hash=output_hash)
    assert clock.discard_late(session.scope, actor_id="main", callback_id="c1", output_hash=output_hash) == first
    assert first["kind"] == "phase_late_result_discarded"
    assert "untrusted" not in json.dumps(first)
    assert clock._state()["value"]["status"] == "closing"


def test_expired_phase_cannot_gain_time_through_recovery(running):
    clock, real, _ = running
    active(clock)
    clock.records.renew(clock.lease, lease_seconds=1)
    real[0] += timedelta(seconds=clock.spec.runtime.phase_wall_timeout_seconds + 1)
    lease = clock.records.claim(clock.lease.test_id, worker_id="late-worker", lease_seconds=10000)
    recovered = PhaseClock(clock.records, lease)
    with pytest.raises(Fenced): recovered.resume(action_id="resume")
    recovered.begin_close("phase_timeout", action_id="timeout")
    assert recovered._state()["value"]["index"] == 0


def test_postauction_rule_mismatch_and_delay_crossing_window_end_are_explicit(running, bundle):
    clock, *_ = running
    rule = dict(bundle[4]["rules"][0]["payload"])
    rule["opening_auction"] = {"start": "09:15:00", "end": "09:26:00"}
    assert submission_timing(clock.schedule[2], rule, order_latency_ms=100, receipt_latency_ms=100, operation="submit")["code"] == "phase_market_clock_conflict"
    rule["opening_auction"] = {"start": "09:15:00", "end": "09:25:00"}
    rule["accept_orders"] = [{"start": "09:15:00", "end": "09:25:00"}, {"start": "09:30:00", "end": "15:00:00"}]
    result = submission_timing(clock.schedule[1], rule, order_latency_ms=30000, receipt_latency_ms=100, operation="submit")
    assert result["exchange_accepted_at"] == "2026-08-03T09:30:30+08:00"
    assert not result["can_participate_opening_auction"]



def test_arbitrary_duration_uses_explicit_holiday_calendar_without_extra_phases():
    from advisor.research.experiments.contracts import CalendarResolution, original_case
    from advisor.research.experiments.resolution import resolve, digest
    raw = original_case()
    raw["tasks"] = [raw["tasks"][0]]
    raw["tasks"][0]["period"]["trading_days"] = 7
    days = tuple(date(2026, 8, d) for d in (3, 4, 6, 7, 10, 11, 12))  # synthetic closure on Aug 5
    def calendar(ref, period):
        return CalendarResolution(calendar_ref=ref, content_hash=digest([str(day) for day in days]),
                                  coverage_start=date(2026, 8, 1), coverage_end=date(2026, 8, 31), sessions=days, complete=True)
    sealed = resolve(raw, calendar=calendar).specification
    schedule = phase_schedule(sealed, raw["tasks"][0]["task_id"])
    assert len(schedule) == 22 and tuple(p.trading_date for p in schedule if p.kind == "auction") == days
    assert all(p.trading_date != date(2026, 8, 5) for p in schedule)
    raw["tasks"][0]["research_start_date"] = "2026-08-03"
    # Full resolution rejects the conflicting initial window before the phase
    # planner can receive a sealed specification.
    result = resolve(raw, calendar=calendar)
    assert result.specification is None and result.errors


def test_snapshot_preparation_rechecks_worker_lease_before_publishing(running):
    clock, real, _ = running
    clock.create(action_id="create")
    original = clock.records.prepare
    def expired_after_prepare(**kwargs):
        result = original(**kwargs)
        real[0] += timedelta(seconds=10001)
        return result
    clock.records.prepare = expired_after_prepare
    with pytest.raises(Fenced): clock.activate(empty(), action_id="expired")
    assert clock.records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='snapshot'").fetchone()[0] == 0
    assert clock._state()["value"]["status"] == "pending"
