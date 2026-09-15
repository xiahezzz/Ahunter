from datetime import date, timedelta
from decimal import Decimal

import pytest

from advisor.research.experiments.orders import StagePlans
from advisor.research.experiments.account import SimulatedAccount, AccountUnavailable
from advisor.research.experiments.clock import PhaseClock
from advisor.research.experiments.contracts import original_case
from advisor.research.experiments.execution import ExecutionEngine, ExchangeRejected, ExecutionEvidenceMissing
from advisor.research.experiments.queue import QueueReplay
from advisor.research.experiments.records import Fenced
from advisor.research.experiments.repository import Conflict
from advisor.research.experiments.resolution import resolve
from tests.advisor.research.test_experiment_contracts import calendar, SESSIONS
from tests.advisor.research.test_experiment_registration import registry, candidate, plan_input
from tests.advisor.research.test_experiment_bundles import bundle
from tests.advisor.research.test_experiment_account import dt, order
from tests.advisor.research.test_experiment_clock import empty, finish
from tests.advisor.research.test_experiment_execution import proof, availability
from tests.advisor.research.test_experiment_queue import capture


@pytest.fixture
def setup(registry, bundle, request):
    service, experiment, _, package, real = registry
    raw = original_case()
    raw["execution"].update(getattr(request, "param", {}))
    sealed = resolve(raw, calendar=calendar).specification
    definition = service.definition(experiment, sealed, submission_identity="orders-definition")
    candidate(service, experiment, package)
    plan = service.test_plan(experiment, definition["record_id"], plan_input(definition, plan_id="orders-plan"), submission_identity="orders-plan", purpose="tuning")
    records, test_id = service.records, plan["tests"][0]["record_id"]
    records.transition(test_id, "preflight", action_id="preflight")
    records.transition(test_id, "queued", action_id="queued")
    lease = records.claim(test_id, worker_id="orders-fixture", lease_seconds=10000)
    records.transition(test_id, "running", action_id="running", lease=lease)
    api = SimulatedAccount(records, lease, calendar_sessions=SESSIONS, calendar_hash=sealed.tasks[0].calendar_hash)
    api.initialize(action_id="initialize")
    clock = PhaseClock(records, lease)
    clock.create(action_id="clock")
    clock.activate(empty(), action_id="initial")
    finish(clock, "initial")
    clock.activate(empty(), action_id="auction")
    fixture = (api, real, bundle[4]["rules"][0]["payload"], bundle[4]["fees"][0]["payload"])
    yield fixture, clock, StagePlans(api, clock, main_actor_id="main")


def postauction(setup):
    fixture, clock, plans = setup
    finish(clock, "auction")
    clock.activate(empty(), action_id="postauction")
    return fixture, clock.session("main"), plans


def submit_new(setup, *, quantity=100, price="10", plan_id="new-plan"):
    fixture, session, plans = postauction(setup)
    at = dt("2026-08-03T09:29:00")
    plan = {"plan_id": plan_id, "instructions": [{"kind": "new", "order": order(fixture, "new", quantity=quantity, price=price)}]}
    event = plans.submit(session, plan, [proof(fixture, at)], action_id="submit")
    return fixture, session, plans, plan, event


def market(fixture, at, *, stream=None, sequence=None):
    value = proof(fixture, at)
    value["source_refs"] = [*value["source_refs"], "position-source"]
    return {"market": value, "cursor": None if stream is None else {"stream_id": stream, "after_sequence": sequence, "source_ref": "position-source"}}


def test_plan_accepts_once_freezes_before_exchange_and_dispatches_only_at_fixed_time(setup):
    fixture, session, plans, plan, event = submit_new(setup)
    api = fixture[0]
    assert api._state()["value"]["orders"]["new"]["status"] == "pending_acceptance"
    assert plans.engine._state()["value"]["orders"] == {}
    assert plans.submit(session, plan, [proof(fixture, dt("2026-08-03T09:29:00"))], action_id="submit") == event
    with pytest.raises(Conflict, match="one successful"):
        plans.submit(session, {"plan_id": "second", "instructions": []}, [], action_id="second")
    with pytest.raises(ExecutionEvidenceMissing):
        plans.advance(at=dt("2026-08-03T09:29:00.200"), evidence=[market(fixture, dt("2026-08-03T09:29:00.200"))], action_id="skip")
    at = dt("2026-08-03T09:29:00.100")
    plans.advance(at=at, evidence=[market(fixture, at)], action_id="dispatch")
    assert api._state()["value"]["orders"]["new"]["status"] == "reserved"
    assert plans.engine._state()["value"]["orders"]["new"]["accepted_at"] == at.isoformat()


def test_rejected_plan_can_be_corrected_and_cannot_reserve_partial_batch(setup):
    fixture, session, plans = postauction(setup)
    at = dt("2026-08-03T09:29:00")
    before = fixture[0]._state()
    bad = {"plan_id": "bad", "instructions": [{"kind": "new", "order": order(fixture, "first")}, {"kind": "new", "order": order(fixture, "too-large", quantity=30000)}]}
    with pytest.raises(AccountUnavailable): plans.submit(session, bad, [proof(fixture, at)], action_id="bad")
    assert fixture[0]._state() == before and plans._state()["value"]["phases"] == {}
    plans.submit(session, {"plan_id": "corrected", "instructions": []}, [], action_id="corrected")
    assert plans._state()["value"]["plans"]["corrected"]["status"] == "accepted"


def test_children_cannot_submit_even_if_they_reuse_the_main_actor_name(setup):
    fixture, session, plans = postauction(setup)
    with pytest.raises(Fenced, match="main phase"):
        plans.submit(session.child("main"), {"plan_id": "child", "instructions": []}, [], action_id="child")
    assert plans._state()["sequence"] is None


def test_deadline_is_rechecked_inside_the_commit_transaction(setup):
    fixture, session, plans = postauction(setup)
    before = fixture[0]._state()
    def expire(point):
        if point == "before_event":
            from datetime import datetime
            fixture[1][0] = datetime.fromisoformat(setup[1]._state()["value"]["deadline_at"]) + timedelta(seconds=1)
    fixture[0].records.fault = expire
    with pytest.raises(Fenced): plans.submit(session, {"plan_id": "late", "instructions": []}, [], action_id="late")
    fixture[0].records.fault = lambda _: None
    assert fixture[0]._state() == before and plans._state()["sequence"] is None


def test_closed_phase_rejects_new_plan_but_returns_exact_committed_retry(setup):
    fixture, session, plans, plan, event = submit_new(setup)
    setup[1].begin_close("completed", action_id="closing")
    assert plans.submit(session, plan, [proof(fixture, dt("2026-08-03T09:29:00"))], action_id="submit") == event
    with pytest.raises(Fenced): plans.submit(session, {"plan_id": "late", "instructions": []}, [], action_id="late")


def old_order(setup, *, quantity=300):
    fixture, _, _ = setup
    api = fixture[0]
    at = dt("2026-08-03T09:24:30")
    ExecutionEngine(api).accept_batch([order(fixture, "old", quantity=quantity, price="11")], [proof(fixture, at)], at=at, action_id="old-accept")
    api.fill("old", quantity=100, price="11", at=dt("2026-08-03T09:25:00"), action_id="old-auction-fill")


@pytest.mark.parametrize("setup", [{"cancel_latency_ms": 120000}], indirect=True)
def test_replacement_counts_fills_before_cancel_and_rejects_illegal_150_without_rounding(setup):
    old_order(setup)
    fixture, session, plans = postauction(setup)
    at = dt("2026-08-03T09:29:00")
    plan = {"plan_id": "replace", "instructions": [{"kind": "replace", "old_order_id": "old", "new_order_id": "replacement", "target_total": 300, "limit_price": "11"}]}
    plans.submit(session, plan, [proof(fixture, at)], action_id="replace-submit")
    queue = capture(fixture, total=5000, ahead=5000)
    queue["route_reason"] = "cancel_match_ordering"
    queue["positions"] = [{"order_id": "old", "after_sequence": 0, "source_ref": "position-source"}]
    QueueReplay(plans.engine).advance(queue, action_id="before-cancel")
    assert fixture[0]._state()["value"]["orders"]["old"]["filled_quantity"] == 150
    effective = dt("2026-08-03T09:31:00")
    plans.advance(at=effective, evidence=[market(fixture, effective, stream="synthetic-queue", sequence=3)], action_id="cancel-effective")
    state = fixture[0]._state()["value"]
    assert state["orders"]["old"]["status"] == "cancel_effective"
    assert Decimal(state["orders"]["old"]["cash_reserved"]) > 0
    # A complete next source window allows ordering the receipt after the
    # cancellation took effect, with no intervening historical matches.
    next_queue = capture(fixture, total=5000, ahead=5000)
    next_queue["capture"].update(start_at=dt("2026-08-03T09:31:00"), end_at=dt("2026-08-03T09:32:00"), start_sequence=4, end_sequence=3)
    next_queue["snapshot"].update(last_sequence=3, orders=[])
    next_queue.update(events=[], positions=[], route_reason="cancel_match_ordering", availability=availability(dt("2026-08-03T09:32:00")))
    next_queue["volumes"][0].update(start=dt("2026-08-03T09:31:00"), end=dt("2026-08-03T09:32:00"), shares=0)
    receipt = dt("2026-08-03T09:31:00.100")
    QueueReplay(plans.engine).advance(next_queue, action_id="receipt-cut", through_at=receipt, through_sequence=3)
    plans.advance(at=receipt, evidence=[market(fixture, receipt, stream="synthetic-queue", sequence=3)], action_id="cancel-receipt")
    flow = plans._state()["value"]["workflows"]["replace:0"]
    assert flow["replacement_quantity"] == 150 and flow["state"] == "replacement_rejected"
    assert "replacement" not in fixture[0]._state()["value"]["orders"]
    assert fixture[0]._state()["value"]["orders"]["old"]["status"] == "cancel_confirmed"


@pytest.mark.parametrize("bad", ["duplicate-old", "target-too-low", "new-opposite"])
def test_conflicting_plan_does_not_cancel_old_order_or_release_its_resources(setup, bad):
    old_order(setup)
    fixture, session, plans = postauction(setup)
    instructions = [{"kind": "replace", "old_order_id": "old", "new_order_id": "replacement", "target_total": 300, "limit_price": "11"}]
    if bad == "duplicate-old": instructions.append({"kind": "cancel", "old_order_id": "old"})
    elif bad == "target-too-low": instructions[0]["target_total"] = 50
    else: instructions.append({"kind": "new", "order": order(fixture, "sell", side="sell", price="10")})
    before = fixture[0]._state()
    with pytest.raises(ExchangeRejected): plans.submit(session, {"plan_id": "bad", "instructions": instructions}, [proof(fixture, dt("2026-08-03T09:29:00"))], action_id="bad")
    assert fixture[0]._state() == before


@pytest.mark.parametrize("point", ["before_event", "after_event", "after_projection", "before_commit", "after_commit"])
def test_plan_success_marker_workflows_and_reservations_recover_once(setup, point):
    fixture, session, plans = postauction(setup)
    plan = {"plan_id": "plan", "instructions": [{"kind": "new", "order": order(fixture, "buy")}]}
    inputs = [proof(fixture, dt("2026-08-03T09:29:00"))]
    def fault(actual):
        if actual == point: raise KeyboardInterrupt("fixture crash")
    fixture[0].records.fault = fault
    with pytest.raises(KeyboardInterrupt): plans.submit(session, plan, inputs, action_id="submit")
    fixture[0].records.fault = lambda _: None
    first = plans.submit(session, plan, inputs, action_id="submit")
    assert plans.submit(session, plan, inputs, action_id="submit") == first
    before, state = fixture[0]._state()["value"], plans._state()["value"]
    fixture[0].records.rebuild(fixture[0].lease)
    assert fixture[0]._state()["value"] == before and plans._state()["value"] == state
    assert len(state["phases"]) == 1 and len(state["workflows"]) == 1


def test_plan_identity_not_transport_attempt_controls_idempotent_retries(setup):
    fixture, session, plans, plan, event = submit_new(setup)
    inputs = [proof(fixture, dt("2026-08-03T09:29:00"))]
    assert plans.submit(session, plan, inputs, action_id="another-transport-attempt") == event
    changed = {**plan, "instructions": []}
    with pytest.raises(Conflict, match="plan identity"):
        plans.submit(session, changed, [], action_id="changed-body")


def closing_capture(fixture, order_ids):
    value = capture(fixture, auction=True)
    value["stream_id"] = "closing"
    value["capture"].update(session="close", start_at=dt("2026-08-03T14:57:00"), end_at=dt("2026-08-03T15:00:00"), start_sequence=1, end_sequence=0)
    value["snapshot"].update(session="close", orders=[], last_sequence=0)
    value.update(events=[], auction={"session": "close", "price": None, "volume": 0, "volume_unit": "share"}, route_reason="closing_auction", availability=availability(dt("2026-08-03T15:00:00")))
    value["positions"] = [{"order_id": identity, "after_sequence": 0, "source_ref": "position-source"} for identity in order_ids]
    value["volumes"][0].update(start=dt("2026-08-03T14:59:00"), end=dt("2026-08-03T15:00:00"), shares=0)
    return value


def test_day_expiry_requires_complete_close_and_releases_without_fees_or_new_orders(setup):
    fixture, _, plans, _, _ = submit_new(setup)
    dispatch = dt("2026-08-03T09:29:00.100")
    plans.advance(at=dispatch, evidence=[market(fixture, dispatch)], action_id="dispatch")
    expiry = dt("2026-08-03T15:00:00.100")
    cursors = {"SH600000": {"stream_id": "closing", "after_sequence": 0, "source_ref": "position-source"}}
    with pytest.raises(ExecutionEvidenceMissing, match="closing-auction"):
        plans.expire_day(date(2026,8,3), cursors, at=expiry, market_rules=[fixture[2]], action_id="early-expiry")
    QueueReplay(plans.engine).advance(closing_capture(fixture, ["new"]), action_id="close")
    event = plans.expire_day(date(2026,8,3), cursors, at=expiry, market_rules=[fixture[2]], action_id="expiry")
    assert plans.expire_day(date(2026,8,3), cursors, at=expiry, market_rules=[fixture[2]], action_id="expiry") == event
    balance = fixture[0].balances(at=expiry)
    assert balance["frozen_cash"] == "0" and balance["cash"] == "200000" and balance["fees_assessed"] == {}
    assert fixture[0]._state()["value"]["orders"]["new"]["status"] == "day_expired"


def test_day_expiry_cannot_skip_an_unprocessed_exchange_acceptance(setup):
    fixture, _, plans, _, _ = submit_new(setup)
    with pytest.raises(ExecutionEvidenceMissing, match="earlier workflow"):
        plans.expire_day(date(2026,8,3), {}, at=dt("2026-08-03T15:00:00.100"), market_rules=[fixture[2]], action_id="skip-to-close")


def test_replacement_reserves_new_order_after_receipt_and_resets_exchange_priority(setup):
    fixture, _, _ = setup
    fixture[2]["buy_increment"] = 1  # Explicit synthetic quantity rule allows 150.
    old_order(setup)
    fixture, session, plans = postauction(setup)
    at = dt("2026-08-03T09:29:00")
    plans.submit(session, {"plan_id": "replace", "instructions": [{"kind": "replace", "old_order_id": "old", "new_order_id": "replacement", "target_total": 250, "limit_price": "10"}]}, [proof(fixture, at)], action_id="submit")
    effective, receipt, accepted = (dt("2026-08-03T09:29:00." + suffix) for suffix in ("100", "200", "300"))
    plans.advance(at=effective, evidence=[market(fixture, effective)], action_id="cancel-effective")
    frozen = fixture[0].balances(at=effective)["frozen_cash"]
    assert Decimal(frozen) > 0 and "replacement" not in fixture[0]._state()["value"]["orders"]
    plans.advance(at=receipt, evidence=[market(fixture, receipt)], action_id="cancel-receipt")
    new = fixture[0]._state()["value"]["orders"]["replacement"]
    assert new["quantity"] == 150 and new["status"] == "pending_acceptance"
    assert Decimal(new["cash_reserved"]) == Decimal("1505.02")
    plans.advance(at=accepted, evidence=[market(fixture, accepted)], action_id="replacement-accepted")
    metadata = plans.engine._state()["value"]["orders"]
    assert metadata["replacement"]["priority"] > metadata["old"]["priority"]
    assert metadata["replacement"]["accepted_at"] == accepted.isoformat()


def test_replacement_resource_failure_keeps_old_fill_and_confirmed_cancellation(setup):
    old_order(setup)
    fixture, session, plans = postauction(setup)
    at = dt("2026-08-03T09:29:00")
    plans.submit(session, {"plan_id": "huge", "instructions": [{"kind": "replace", "old_order_id": "old", "new_order_id": "unfunded", "target_total": 30100, "limit_price": "11"}]}, [proof(fixture, at)], action_id="submit")
    effective, receipt = dt("2026-08-03T09:29:00.100"), dt("2026-08-03T09:29:00.200")
    plans.advance(at=effective, evidence=[market(fixture, effective)], action_id="effective")
    plans.advance(at=receipt, evidence=[market(fixture, receipt)], action_id="receipt")
    state = fixture[0]._state()["value"]
    assert state["orders"]["old"]["filled_quantity"] == 100 and state["orders"]["old"]["status"] == "cancel_confirmed"
    assert "unfunded" not in state["orders"]
    assert plans._state()["value"]["workflows"]["huge:0"]["state"] == "replacement_rejected"


def test_target_already_satisfied_creates_no_replacement(setup):
    old_order(setup)
    fixture, session, plans = postauction(setup)
    at = dt("2026-08-03T09:29:00")
    plans.submit(session, {"plan_id": "done", "instructions": [{"kind": "replace", "old_order_id": "old", "new_order_id": "unneeded", "target_total": 100, "limit_price": "10"}]}, [proof(fixture, at)], action_id="submit")
    for identity, at in (("effective",dt("2026-08-03T09:29:00.100")),("receipt",dt("2026-08-03T09:29:00.200"))):
        plans.advance(at=at, evidence=[market(fixture, at)], action_id=identity)
    assert plans._state()["value"]["workflows"]["done:0"]["state"] == "target_satisfied"
    assert "unneeded" not in fixture[0]._state()["value"]["orders"]


def test_exchange_cage_rejection_releases_only_on_receipt_and_charges_no_fee(setup):
    setup[0][2]["price_cage_fraction"] = ".02"
    fixture, _, plans, _, _ = submit_new(setup)
    effective, receipt = dt("2026-08-03T09:29:00.100"), dt("2026-08-03T09:29:00.200")
    rejected = market(fixture, effective)
    rejected["market"]["buy_cage_upper"] = "9.99"
    plans.advance(at=effective, evidence=[rejected], action_id="rejected")
    assert fixture[0]._state()["value"]["orders"]["new"]["status"] == "rejected_pending_receipt"
    assert Decimal(fixture[0].balances(at=effective)["frozen_cash"]) > 0
    plans.advance(at=receipt, evidence=[market(fixture, receipt)], action_id="rejected-receipt")
    assert fixture[0].balances(at=receipt)["frozen_cash"] == "0"
    assert fixture[0]._state()["value"]["fees_assessed"] == {}


@pytest.mark.parametrize("point", ["before_event", "after_event", "after_projection", "before_commit", "after_commit"])
def test_cancel_receipt_and_replacement_reservation_commit_once_together(setup, point):
    old_order(setup)
    fixture, session, plans = postauction(setup)
    received = dt("2026-08-03T09:29:00")
    plans.submit(session, {"plan_id": "replace", "instructions": [{"kind": "replace", "old_order_id": "old", "new_order_id": "replacement", "target_total": 300, "limit_price": "10"}]}, [proof(fixture, received)], action_id="submit")
    effective, receipt = dt("2026-08-03T09:29:00.100"), dt("2026-08-03T09:29:00.200")
    plans.advance(at=effective, evidence=[market(fixture, effective)], action_id="effective")
    def fault(actual):
        if actual == point: raise KeyboardInterrupt("fixture crash")
    fixture[0].records.fault = fault
    with pytest.raises(KeyboardInterrupt): plans.advance(at=receipt, evidence=[market(fixture, receipt)], action_id="receipt")
    fixture[0].records.fault = lambda _: None
    first = plans.advance(at=receipt, evidence=[market(fixture, receipt)], action_id="receipt")
    assert plans.advance(at=receipt, evidence=[market(fixture, receipt)], action_id="receipt") == first
    state = fixture[0]._state()["value"]
    assert state["orders"]["old"]["status"] == "cancel_confirmed"
    assert state["orders"]["replacement"]["quantity"] == 200
    assert Decimal(state["orders"]["replacement"]["cash_reserved"]) == Decimal("2005.02")
    assert Decimal(state["fees_assessed"]["commission"]) == 5
    expected = plans._state()["value"]
    fixture[0].records.rebuild(fixture[0].lease)
    assert fixture[0]._state()["value"] == state and plans._state()["value"] == expected


def test_pending_replacement_identity_cannot_be_reused_before_its_shares_are_reserved(setup):
    old_order(setup)
    fixture, session, plans = postauction(setup)
    at = dt("2026-08-03T09:29:00")
    plans.submit(session, {"plan_id": "replace", "instructions": [{"kind": "replace", "old_order_id": "old", "new_order_id": "reserved-id", "target_total": 300, "limit_price": "10"}]}, [proof(fixture, at)], action_id="submit")
    with pytest.raises(Conflict, match="reserved by an accepted"):
        plans.engine.accept_batch([order(fixture, "reserved-id")], [proof(fixture, at)], at=at, action_id="steal-id")


def test_unproven_matching_cursor_cannot_finalize_cancellation(setup):
    fixture, clock, plans = setup
    # Auction-phase cancellation needs explicit placement relative to the
    # historical exchange stream, even when its timestamp is known.
    at = dt("2026-08-03T09:20:00")
    plans.engine.accept_batch([order(fixture, "old", price="11")], [proof(fixture, at)], at=at, action_id="old")
    phase = clock.schedule[1]
    plans.submit(clock.session("main"), {"plan_id": "cancel", "instructions": [{"kind": "cancel", "old_order_id": "old"}]},
                 [proof(fixture, phase.plan_received_at)], action_id="submit")
    due = datetime_from_flow(plans, "cancel:0")
    before = fixture[0]._state()
    with pytest.raises(ExecutionEvidenceMissing, match="proven ordering"):
        plans.advance(at=due, evidence=[market(fixture, due)], action_id="unknown-order")
    assert fixture[0]._state() == before


def datetime_from_flow(plans, identity):
    from datetime import datetime
    return datetime.fromisoformat(plans._state()["value"]["workflows"][identity]["next_at"])


def test_forbidden_auction_cancel_records_rejection_and_keeps_old_order_live(setup):
    fixture, clock, plans = setup
    at = dt("2026-08-03T09:20:00")
    plans.engine.accept_batch([order(fixture, "old", price="11")], [proof(fixture, at)], at=at, action_id="old")
    phase = clock.schedule[1]
    plans.submit(clock.session("main"), {"plan_id": "cancel", "instructions": [{"kind": "cancel", "old_order_id": "old"}]},
                 [proof(fixture, phase.plan_received_at)], action_id="submit")
    due = datetime_from_flow(plans, "cancel:0")
    value = capture(fixture, auction=True)
    value["capture"].update(start_sequence=1, end_sequence=0)
    value["snapshot"].update(last_sequence=0, orders=[])
    value.update(events=[], positions=[{"order_id": "old", "after_sequence": 0, "source_ref": "position-source"}])
    value["volumes"][0]["shares"] = 0
    value["auction"].update(price=None, volume=0)
    QueueReplay(plans.engine).advance(value, action_id="cancel-cut", through_at=due, through_sequence=0)
    plans.advance(at=due, evidence=[market(fixture, due, stream="synthetic-queue", sequence=0)], action_id="forbidden")
    assert plans._state()["value"]["workflows"]["cancel:0"]["state"] == "cancel_rejected"
    assert fixture[0]._state()["value"]["orders"]["old"]["status"] == "reserved"
    assert Decimal(fixture[0]._state()["value"]["orders"]["old"]["cash_reserved"]) > 0


@pytest.mark.parametrize("setup", [{"receipt_latency_ms": 86400000}], indirect=True)
def test_late_cancellation_receipt_never_recreates_a_replacement_next_day(setup):
    old_order(setup)
    fixture, session, plans = postauction(setup)
    at = dt("2026-08-03T09:29:00")
    plans.submit(session, {"plan_id": "late", "instructions": [{"kind": "replace", "old_order_id": "old", "new_order_id": "never-next-day", "target_total": 300, "limit_price": "10"}]}, [proof(fixture, at)], action_id="submit")
    effective = dt("2026-08-03T09:29:00.100")
    plans.advance(at=effective, evidence=[market(fixture, effective)], action_id="effective")
    receipt = dt("2026-08-04T09:29:00.100")
    plans.advance(at=receipt, evidence=[market(fixture, receipt)], action_id="late-receipt")
    assert plans._state()["value"]["workflows"]["late:0"]["state"] == "replacement_expired"
    assert "never-next-day" not in fixture[0]._state()["value"]["orders"]
    assert fixture[0]._state()["value"]["orders"]["old"]["status"] == "cancel_confirmed"
