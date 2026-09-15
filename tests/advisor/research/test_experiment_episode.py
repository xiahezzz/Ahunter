from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest

from advisor.research.experiments.account import SimulatedAccount
from advisor.research.experiments.contracts import original_case
from advisor.research.experiments.episode import EpisodeReplay, ReplayProgram
from advisor.research.experiments.records import Fenced
from advisor.research.experiments.repository import Conflict
from advisor.research.experiments.resolution import resolve
from tests.advisor.research.test_experiment_registration import registry, candidate, plan_input
from tests.advisor.research.test_experiment_bundles import bundle
from tests.advisor.research.test_experiment_contracts import calendar, SESSIONS
from tests.advisor.research.test_experiment_account import dt, order, close_quote
from tests.advisor.research.test_experiment_clock import empty
from tests.advisor.research.test_experiment_execution import minute, proof
from tests.advisor.research.test_experiment_orders import market, closing_capture


@pytest.fixture
def episode(registry, bundle, request):
    service, experiment, _, package, real = registry
    raw = original_case()
    days = getattr(request, "param", 5)
    raw["tasks"][0]["period"] = {"mode": "trading_days", "start": "2026-08-03", "trading_days": days}
    sealed = resolve(raw, calendar=calendar).specification
    definition = service.definition(experiment, sealed, submission_identity="episode-definition")
    candidate(service, experiment, package)
    plan = service.test_plan(experiment, definition["record_id"], plan_input(definition, plan_id="episode-plan"),
                             submission_identity="episode-plan", purpose="tuning")
    records, test_id = service.records, plan["tests"][0]["record_id"]
    records.transition(test_id, "preflight", action_id="preflight")
    records.transition(test_id, "queued", action_id="queued")
    lease = records.claim(test_id, worker_id="episode-fixture", lease_seconds=10000)
    records.transition(test_id, "running", action_id="running", lease=lease)
    account = SimulatedAccount(records, lease, calendar_sessions=SESSIONS, calendar_hash=sealed.tasks[0].calendar_hash)
    rule, fees = deepcopy(bundle[4]["rules"][0]["payload"]), bundle[4]["fees"][0]["payload"]
    rule["continuous"][0]["end"] = "11:30:00"  # Explicit synthetic rule, not production coverage.
    replay = EpisodeReplay(account, main_actor_id="main")
    return replay, (account, real, rule, fees)


def program(episode):
    replay, fixture = episode
    rule = fixture[2]
    points = [{"kind": "valuation", "purpose": "initial", "trade_date": "2026-08-02",
               "at": dt("2026-08-02T23:05:00"), "closes": [], "market_rules": [rule]}]
    for day in replay.account.task.trading_dates:
        text = day.isoformat()
        at = dt(text + "T09:29:00.100")
        points.append({"kind": "workflow", "at": at, "evidence": [market(fixture, at)]})
        points.append({"kind": "minute", "at": dt(text + "T09:32:00"),
                       "evidence": minute(fixture, start=text + "T09:31:00", end=text + "T09:32:00")})
        points.extend([
            {"kind": "settle_payables", "at": dt(text + "T14:59:59")},
            {"kind": "checkpoint", "at": dt(text + "T15:00:00")},
            {"kind": "day_expiry", "at": dt(text + "T15:00:00.100"), "day": day, "cursors": {}, "market_rules": [rule]},
            {"kind": "valuation", "purpose": "daily", "trade_date": day, "at": dt(text + "T23:00:00"),
             "closes": [close_quote(day=text)], "market_rules": [rule]},
        ])
    points.append({**points[-1], "purpose": "terminal"})
    return {"version": 1, "evidence_kind": "fixture", "test_id": replay.lease.test_id,
            "specification_hash": replay.clock.sealed.specification_hash,
            "snapshots": {p.phase_id: empty() for p in replay.clock.schedule}, "points": points}


def trade(replay, fixture, phase):
    day = phase.trading_date
    if phase.kind != "postauction" or day not in replay.account.task.trading_dates[:2]:
        return
    buy = day == replay.account.task.trading_dates[0]
    identity = "buy" if buy else "sell"
    session = replay.clock.session("main")
    replay.plans.submit(session, {"plan_id": identity, "instructions": [{"kind": "new",
        "order": order(fixture, identity, side=identity, price="10" if buy else "9")} ]},
        [proof(fixture, phase.plan_received_at)], action_id="submit-" + identity)


def run(replay, fixture, *, on_phase=None):
    seen = []
    for _ in range(300):
        result = replay.step()
        if result["kind"] == "research_required":
            phase = next(p for p in replay.clock.schedule if p.phase_id == result["phase_id"])
            seen.append(phase)
            if on_phase:
                on_phase(replay, fixture, phase)
            else:
                trade(replay, fixture, phase)
                replay.close_phase(phase.phase_id, "completed")
        elif result["kind"] != "progress":
            return result, seen
    raise AssertionError("Episode did not terminate within its fixture tick bound")


@pytest.mark.parametrize("episode", [5, 3], indirect=True)
def test_complete_declared_episode_preserves_t1_fees_daily_nav_and_phase_handoffs(episode):
    replay, fixture = episode
    replay.create(program(episode))
    result, seen = run(replay, fixture)
    assert result == {"kind": "ready_for_evaluation", "formal_ready": False}
    assert [p.phase_id for p in seen] == [p.phase_id for p in replay.clock.schedule]
    assert all(p.boundary.snapshot_at.hour < 10 or p.boundary.snapshot_at.hour == 23 for p in seen)
    state = replay._state()["value"]
    assert state["cursor"] == len(program(episode)["points"]) and state["pending"] is None
    marks = {key: replay.records.read(identity)["value"] for key, identity in state["valuations"].items()}
    assert marks["daily:2026-08-03"]["market_value"] == "1100"
    assert marks["daily:2026-08-04"]["market_value"] == "0"
    assert marks["daily:2026-08-03"]["nav"] == "200094.99"
    assert Decimal(marks["daily:2026-08-04"]["nav"]) < 200000
    balances = replay.account.balances(at=dt("2026-08-04T23:00:00"), observed=True)
    assert balances["frozen_cash"] == "0" and balances["fees_assessed"]["commission"] == "10.00"
    assert replay.plans._state()["value"]["expired_days"] == [d.isoformat() for d in replay.account.task.trading_dates]
    before = replay._state()
    assert replay.step() == result and replay._state() == before
    assert replay.records.status(replay.lease.test_id) == "running"  # Queue/evaluator own lifecycle transitions.


def until_phase(replay, fixture, *, kind="postauction"):
    for _ in range(80):
        result = replay.step()
        if result["kind"] == "research_required":
            phase = replay.clock.schedule[replay.clock._state()["value"]["index"]]
            if phase.kind == kind:
                return phase
            replay.close_phase(phase.phase_id, "completed")
    raise AssertionError("phase handoff missing")


@pytest.mark.parametrize("kind", ["valuation", "workflow", "minute", "settle_payables", "checkpoint", "day_expiry"])
def test_new_worker_retries_original_prepared_effect_after_commit_without_double_fill_or_skipping(episode, kind):
    replay, fixture = episode
    replay.create(program(episode))
    # Execute until the desired command has been prepared, including a real buy.
    for _ in range(100):
        pending = replay._state()["value"]["pending"]
        if pending and pending["command"]["kind"] == kind:
            break
        result = replay.step()
        if result["kind"] == "research_required":
            phase = replay.clock.schedule[replay.clock._state()["value"]["index"]]
            trade(replay, fixture, phase)
            replay.close_phase(phase.phase_id, "completed")
    else:
        raise AssertionError("missing prepared effect")
    before_cursor = replay._state()["value"]["cursor"]
    def crash(_):
        raise RuntimeError("worker lost after domain commit")
    replay.fault = crash
    with pytest.raises(RuntimeError, match="worker lost"):
        replay.step()
    account_before = replay.account._state()
    assert replay._state()["value"]["cursor"] == before_cursor
    fixture[1][0] += timedelta(seconds=10001)
    lease = replay.records.claim(replay.lease.test_id, worker_id="replacement", lease_seconds=10000)
    account = SimulatedAccount(replay.records, lease, calendar_sessions=SESSIONS, calendar_hash=replay.account.calendar_hash)
    recovered = EpisodeReplay(account, main_actor_id="main")
    with pytest.raises(Fenced):
        replay.step()
    recovered.records.rebuild(lease)
    recovered.step()
    assert recovered.account._state() == account_before
    assert recovered._state()["value"]["cursor"] == before_cursor + 1
    result, _ = run(recovered, (account, *fixture[1:]))
    assert result["kind"] == "ready_for_evaluation"
    orders = recovered.account._state()["value"]["orders"]
    assert orders["buy"]["filled_quantity"] == orders["sell"]["filled_quantity"] == 100
    assert recovered.account.balances(at=dt("2026-08-07T23:00:00"))["fees_assessed"]["commission"] == "10.00"


def test_budget_exhaustion_replays_accepted_buy_to_original_endpoint_without_further_research(episode):
    replay, fixture = episode
    replay.create(program(episode))
    phase = until_phase(replay, fixture)
    trade(replay, fixture, phase)
    replay.close_phase(phase.phase_id, "budget_exhausted")
    result, seen = run(replay, fixture)
    assert result["kind"] == "ready_for_evaluation" and seen == []
    assert replay.account._state()["value"]["orders"]["buy"]["filled_quantity"] == 100
    assert "sell" not in replay.account._state()["value"]["orders"]
    assert replay.clock._state()["value"]["research_stopped"]


@pytest.mark.parametrize("reason,status", [("cancelled", "cancelled"), ("platform_failure", "failed"), ("required_data_unavailable", "blocked")])
def test_stop_keeps_partial_account_without_replaying_accepted_order_or_creating_terminal_nav(episode, reason, status):
    replay, fixture = episode
    replay.create(program(episode))
    phase = until_phase(replay, fixture)
    trade(replay, fixture, phase)
    cursor = replay._state()["value"]["cursor"]
    replay.request_stop(reason)
    result, seen = run(replay, fixture)
    assert result["kind"] == status and seen == []
    assert replay._state()["value"]["cursor"] == cursor
    assert replay.account._state()["value"]["orders"]["buy"]["status"] == "pending_acceptance"
    assert not any(key.startswith("terminal:") for key in replay._state()["value"]["valuations"])


def test_candidate_error_keeps_period_and_next_phase_research(episode):
    replay, fixture = episode
    replay.create(program(episode))
    def fail_phase(replay, fixture, phase):
        trade(replay, fixture, phase)
        replay.close_phase(phase.phase_id, "candidate_recoveries_exhausted")
    result, seen = run(replay, fixture, on_phase=fail_phase)
    assert result["kind"] == "ready_for_evaluation" and len(seen) == 16
    assert Decimal(replay.records.read(replay._state()["value"]["valuations"]["terminal:2026-08-07"])["value"]["nav"]) < 200000


def test_program_binding_chronology_snapshot_and_daily_completeness_fail_closed(episode):
    replay, _ = episode
    original = program(episode)
    for mutation in ("snapshot", "daily", "test", "initial", "checkpoint", "order"):
        value = deepcopy(original)
        if mutation == "snapshot": value["snapshots"].pop(next(iter(value["snapshots"])))
        elif mutation == "daily": value["points"] = [p for p in value["points"] if p.get("purpose") != "daily"]
        elif mutation == "test": value["test_id"] = "another-test"
        elif mutation == "initial": value["points"][0]["at"] += timedelta(seconds=1)
        elif mutation == "checkpoint": value["points"] = [p for p in value["points"] if p["kind"] != "checkpoint"]
        else: value["points"][1:3] = reversed(value["points"][1:3])
        with pytest.raises((Conflict, ValueError)):
            replay.create(value)
    assert replay.records.projection(replay.lease.test_id, "episode") is None
    first = replay.create(original)
    assert replay.create(ReplayProgram.model_validate(original)) == first
    value = deepcopy(original)
    value["points"][2]["evidence"]["bar"]["volume"] = 11000
    with pytest.raises(Conflict, match="different sealed"):
        replay.create(value)


def test_omitted_workflow_cannot_be_hidden_by_minute_or_next_phase(episode):
    replay, fixture = episode
    value = program(episode)
    value["points"] = [p for p in value["points"] if p["kind"] != "workflow"]
    replay.create(value)
    phase = until_phase(replay, fixture)
    trade(replay, fixture, phase)
    replay.close_phase(phase.phase_id, "completed")
    result, _ = run(replay, fixture)
    assert result["kind"] == "blocked"
    assert replay.account._state()["value"]["orders"]["buy"]["filled_quantity"] == 0


def test_active_phase_ticks_do_not_advance_replay_and_late_outcomes_cannot_close_next_phase(episode):
    replay, fixture = episode
    replay.create(program(episode))
    phase = until_phase(replay, fixture, kind="initial_research")
    before = replay._state()
    for _ in range(3):
        assert replay.step()["kind"] == "research_required"
    assert replay._state() == before
    replay.close_phase(phase.phase_id, "completed")
    until_phase(replay, fixture, kind="auction")
    with pytest.raises(Fenced): replay.close_phase(phase.phase_id, "completed")


@pytest.mark.parametrize("kind", ["queue", "day_expiry"])
def test_closing_queue_and_day_release_recover_with_live_frozen_cash(episode, kind):
    replay, fixture = episode
    value = program(episode)
    value["points"][2]["evidence"]["bar"]["volume"] = 0
    close = closing_capture(fixture, ["buy"])
    position = next(i for i, p in enumerate(value["points"]) if p["kind"] == "checkpoint")
    value["points"].insert(position, {"kind": "queue", "at": dt("2026-08-03T15:00:00"),
                                     "evidence": close, "through_sequence": 0})
    expiry = next(p for p in value["points"] if p["kind"] == "day_expiry")
    expiry["cursors"] = {"SH600000": {"stream_id": "closing", "after_sequence": 0, "source_ref": "position-source"}}
    replay.create(value)
    phase = until_phase(replay, fixture)
    trade(replay, fixture, phase)
    replay.close_phase(phase.phase_id, "completed")
    for _ in range(30):
        pending = replay._state()["value"]["pending"]
        if pending and pending["command"]["kind"] == kind:
            break
        assert replay.step()["kind"] == "progress"
    else:
        raise AssertionError("live closing boundary not prepared")
    assert Decimal(replay.account.balances(at=dt("2026-08-03T15:00:00"))["frozen_cash"]) > 0
    def crash(_):
        raise RuntimeError("committed before cursor")
    replay.fault = crash
    with pytest.raises(RuntimeError): replay.step()
    before = replay.account._state()
    fixture[1][0] += timedelta(seconds=10001)
    lease = replay.records.claim(replay.lease.test_id, worker_id="close-recovery", lease_seconds=10000)
    account = SimulatedAccount(replay.records, lease, calendar_sessions=SESSIONS, calendar_hash=replay.account.calendar_hash)
    recovered = EpisodeReplay(account, main_actor_id="main")
    recovered.step()
    assert recovered.account._state() == before
    def empty_research(replay, fixture, phase):
        replay.close_phase(phase.phase_id, "completed")
    result, _ = run(recovered, (account, *fixture[1:]), on_phase=empty_research)
    assert result["kind"] == "ready_for_evaluation"
    balance = account.balances(at=dt("2026-08-07T23:00:00"))
    assert balance["cash"] == "200000" and balance["frozen_cash"] == "0"
    assert account._state()["value"]["orders"]["buy"]["status"] == "day_expired"
    assert balance["fees_assessed"] == {}


def test_committed_phase_activation_survives_worker_loss_without_blind_research_or_deadline_extension(episode):
    replay, fixture = episode
    replay.create(program(episode))
    replay.records.renew(replay.lease, lease_seconds=1)
    fired = False
    def crash(point):
        nonlocal fired
        row = replay.records.db.execute("SELECT kind FROM lagent_events ORDER BY event_sequence DESC LIMIT 1").fetchone()
        if point == "after_commit" and row[0] == "phase_activated" and not fired:
            fired = True
            raise RuntimeError("activation committed")
    replay.records.fault = crash
    with pytest.raises(RuntimeError, match="activation committed"):
        until_phase(replay, fixture, kind="initial_research")
    replay.records.fault = lambda _: None
    phase_before = replay.clock._state()["value"]
    fixture[1][0] += timedelta(seconds=2)
    lease = replay.records.claim(replay.lease.test_id, worker_id="phase-recovery", lease_seconds=10000)
    account = SimulatedAccount(replay.records, lease, calendar_sessions=SESSIONS, calendar_hash=replay.account.calendar_hash)
    recovered = EpisodeReplay(account, main_actor_id="main")
    assert recovered.step()["kind"] == "research_recovery_required"
    with pytest.raises(Fenced): replay.step()
    # The owning adapter must reconcile any original dispatch before this call.
    recovered.clock.resume(action_id="resume-after-cleanup")
    result = recovered.step()
    assert result["kind"] == "research_required"
    assert result["snapshot_id"] == phase_before["snapshot_id"]
    assert result["deadline_at"] == phase_before["deadline_at"]
    assert recovered.clock._state()["value"]["index"] == 0


def test_missing_snapshot_stops_before_candidate_handoff(episode):
    replay, fixture = episode
    value = program(episode)
    phase_id = replay.clock.schedule[0].phase_id
    value["snapshots"][phase_id]["required_items"] = ["unavailable"]
    replay.create(value)
    result, seen = run(replay, fixture)
    assert result["kind"] == "blocked" and seen == []
    assert replay._state()["value"]["cursor"] == 1


def test_bad_prepared_market_evidence_blocks_without_retry_loop_or_fabricated_fill(episode):
    replay, fixture = episode
    value = program(episode)
    value["points"][2]["evidence"]["corporate_actions_complete"] = False
    replay.create(value)
    result, _ = run(replay, fixture)
    assert result["kind"] == "blocked"
    assert replay._state()["value"]["pending"] is None
    assert replay.account._state()["value"]["orders"]["buy"]["filled_quantity"] == 0
    again = replay._state()
    assert replay.step() == result and replay._state() == again


def test_stop_after_effect_commit_finishes_only_original_pending_checkpoint(episode):
    replay, fixture = episode
    replay.create(program(episode))
    phase = until_phase(replay, fixture)
    trade(replay, fixture, phase)
    replay.close_phase(phase.phase_id, "completed")
    for _ in range(20):
        pending = replay._state()["value"]["pending"]
        if pending and pending["command"]["kind"] == "minute": break
        replay.step()
    else:
        raise AssertionError("minute was not prepared")
    def crash(_):
        raise RuntimeError("minute committed")
    replay.fault = crash
    with pytest.raises(RuntimeError): replay.step()
    cursor = replay._state()["value"]["cursor"]
    before = replay.account._state()
    replay.request_stop("cancelled")
    replay.fault = lambda _: None
    result, seen = run(replay, fixture)
    assert result["kind"] == "cancelled" and seen == []
    assert replay.account._state() == before
    assert replay._state()["value"]["cursor"] == cursor + 1
