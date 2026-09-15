from decimal import Decimal

import pytest

from advisor.research.experiments.queue import QueueReplay
from advisor.research.experiments.execution import ExecutionEngine, ExecutionEvidenceMissing
from advisor.research.experiments.repository import Conflict
from advisor.research.experiments.resolution import digest
from tests.advisor.research.test_experiment_account import account, registry, bundle, dt, order
from tests.advisor.research.test_experiment_execution import availability, proof, minute


def setup(fixture, *, quantity=100, two=False, at="2026-08-03T09:30:00"):
    engine = ExecutionEngine(fixture[0])
    orders = [order(fixture, "z-first", quantity=quantity, price="11")]
    if two: orders.append(order(fixture, "a-second", quantity=quantity, price="11"))
    engine.accept_batch(orders, [proof(fixture, dt(at))], at=dt(at), action_id="accept")
    return engine, QueueReplay(engine)


def capture(fixture, *, total=10000, ahead=100, auction=False, two=False):
    session = "open" if auction else "continuous"
    start = "2026-08-03T09:15:00" if auction else "2026-08-03T09:30:00"
    end = "2026-08-03T09:25:00" if auction else "2026-08-03T09:31:00"
    window = "2026-08-03T09:24:00" if auction else start
    first = end if auction else "2026-08-03T09:30:10"
    added = "2026-08-03T09:24:40" if auction else "2026-08-03T09:30:20"
    # Auction additions precede all matching; continuous additions can follow
    # an earlier completed match. Both captures are complete and contiguous.
    events = []
    def event(sequence, kind, identity, quantity, when, contra=None):
        return {"at": dt(when), "event": {"session": session, "event_type": kind, "sequence": sequence,
            "order_id": identity, "contra_order_id": contra, "side": "buy", "price": "11", "quantity": quantity},
            "capacity_start": dt(window) if kind == "match" else None}
    if auction and total > ahead:
        events.append(event(3, "add", "behind", total-ahead, added))
        events.append(event(4, "match", "ahead", ahead, first, "seller"))
        events.append(event(5, "match", "behind", total-ahead, end, "seller"))
    else:
        events.append(event(3, "match", "ahead", ahead, first, "seller"))
        if total > ahead:
            events.append(event(4, "add", "behind", total-ahead, added))
            events.append(event(5, "match", "behind", total-ahead, "2026-08-03T09:30:30", "seller"))
    return {"security": "SH600000", "stream_id": "synthetic-queue", "market_rule": fixture[2],
        "price_bounds": {"lower": "9", "upper": "11"}, "capture": {"start_at": dt(start), "end_at": dt(end),
            "session": session, "start_sequence": 3, "end_sequence": events[-1]["event"]["sequence"], "complete": True},
        "snapshot": {"session": session, "last_sequence": 2, "complete": True, "orders": [
            {"order_id": "ahead", "side": "buy", "price": "11", "quantity": ahead, "priority": 1},
            {"order_id": "seller", "side": "sell", "price": "11", "quantity": total, "priority": 2}]},
        "events": events, "volumes": [{"start": dt(window), "end": dt(end), "shares": total,
            "source_hash": digest("synthetic-minute"), "source_ref": "volume-source"}],
        "positions": [{"order_id": identity, "after_sequence": 2, "source_ref": "position-source"} for identity in (["z-first", "a-second"] if two else ["z-first"])],
        "auction": {"session": "open", "price": "11", "volume": total, "volume_unit": "share"} if auction else None,
        "route_reason": "opening_auction" if auction else "limit_buy", "availability": availability(dt(end)),
        "source_refs": ["queue-source", "volume-source", "position-source"], "source_hash": digest("synthetic-queue-source"),
        "corporate_actions_complete": True}


def results(engine, action_id="queue"):
    return engine._state()["value"]["queue_results"][action_id]


def test_front_quantity_must_be_consumed_before_later_historical_orders_offer_fills(account):
    engine, replay = setup(account)
    value = capture(account)
    first = replay.advance(value, action_id="queue")
    assert replay.advance(value, action_id="queue") == first
    output = results(engine)
    assert [(fill["sequence"], fill["quantity"], fill["price"]) for fill in output["fills"]] == [(5, 100, "11")]
    assert output["historical_audit"][0]["eligible_order_ids"] == []
    assert engine._state()["value"]["queues"]["SH600000:synthetic-queue"]["book"] == {}
    assert next(iter(engine._state()["value"]["liquidity"].values()))["used"] == 100
    assert Decimal(account[0].balances(at=dt("2026-08-03T09:31:00"))["cash"]) == Decimal("198894.99")


def test_proven_queue_not_consumed_yields_no_fill_even_with_price_and_volume(account):
    engine, replay = setup(account)
    replay.advance(capture(account, ahead=10000), action_id="queue")
    assert results(engine)["fills"] == []
    assert results(engine)["results"][0]["status"] == "model_no_fill"
    assert account[0]._state()["value"]["stock_lots"] == {}


def test_fixed_auction_price_and_shared_capacity_allocate_partial_fills_in_time_order(account):
    engine, replay = setup(account, two=True, at="2026-08-03T09:24:30")
    value = capture(account, total=15000, auction=True, two=True)
    replay.advance(value, action_id="queue")
    assert [(fill["order_id"], fill["quantity"], fill["price"]) for fill in results(engine)["fills"]] == [("z-first", 100, "11"), ("a-second", 50, "11")]
    assert all(fill["at"] == dt("2026-08-03T09:25:00").isoformat() for fill in results(engine)["fills"])
    assert next(iter(engine._state()["value"]["liquidity"].values()))["used"] == 150


def test_prefix_commits_only_proven_earlier_matches_and_recovers_without_reallocation(account):
    engine, replay = setup(account)
    value = capture(account)
    replay.advance(value, action_id="before-later", through_at=dt("2026-08-03T09:30:10"), through_sequence=3)
    assert account[0]._state()["value"]["stock_lots"] == {}
    assert engine._state()["value"]["queues"]["SH600000:synthetic-queue"]["book"]["seller"]["quantity"] == 9900
    account[0].records.rebuild(account[0].lease)
    replay.advance(value, action_id="queue")
    assert sum(fill["quantity"] for fill in results(engine)["fills"]) == 100
    assert next(iter(engine._state()["value"]["liquidity"].values()))["used"] == 100


def test_only_proven_ahead_cancellation_improves_position(account):
    engine, replay = setup(account)
    value = capture(account)
    value["events"][0]["event"].update(event_type="cancel", contra_order_id=None)
    value["events"][0]["capacity_start"] = None
    value["events"][1]["event"]["quantity"] = 10000
    value["events"][2]["event"]["quantity"] = 10000
    replay.advance(value, action_id="queue")
    assert results(engine)["historical_audit"][0]["kind"] == "cancel"
    assert sum(fill["quantity"] for fill in results(engine)["fills"]) == 100


@pytest.mark.parametrize("bad", ["gap", "duplicate-priority", "incomplete", "unknown-cancel", "unknown-contra", "priority-violation", "volume", "position-time", "position-source", "wrong-route", "auction-price", "future-source"])
def test_incomplete_or_contradictory_queue_proof_never_debits_cash_or_capacity(account, bad):
    auction = bad == "auction-price"
    engine, replay = setup(account, at="2026-08-03T09:24:30" if auction else "2026-08-03T09:30:00")
    value = capture(account, auction=auction)
    if bad == "gap": value["events"][1]["event"]["sequence"] += 1
    elif bad == "duplicate-priority": value["snapshot"]["orders"][1]["priority"] = 1
    elif bad == "incomplete": value["capture"]["complete"] = False
    elif bad == "unknown-cancel":
        value["events"][1]["event"].update(event_type="cancel", order_id="not-in-book")
    elif bad == "unknown-contra": value["events"][0]["event"]["contra_order_id"] = "missing"
    elif bad == "priority-violation":
        value["snapshot"]["orders"][0]["quantity"] = 200
    elif bad == "volume": value["volumes"][0]["shares"] += 1
    elif bad == "position-time": value["positions"][0]["after_sequence"] = 4
    elif bad == "position-source": value["positions"][0]["source_ref"] = "missing-source"
    elif bad == "wrong-route": value["route_reason"] = "opening_auction"
    elif bad == "auction-price": value["auction"]["price"] = "10"
    elif bad == "future-source": value["availability"]["available_at"] = dt("2026-08-03T10:00:00")
    before, execution = account[0]._state(), engine._state()
    with pytest.raises(ExecutionEvidenceMissing): replay.advance(value, action_id="bad")
    assert account[0]._state() == before and engine._state() == execution


def test_bad_late_event_cannot_be_hidden_by_a_short_prefix(account):
    engine, replay = setup(account)
    value = capture(account)
    value["events"][-1]["event"]["contra_order_id"] = "missing"
    before = account[0]._state()
    with pytest.raises(ExecutionEvidenceMissing):
        replay.advance(value, action_id="early", through_at=dt("2026-08-03T09:30:10"), through_sequence=3)
    assert account[0]._state() == before


@pytest.mark.parametrize("point", ["before_event", "after_event", "after_projection", "before_commit", "after_commit"])
def test_queue_fills_book_cursor_and_capacity_are_atomic_and_rebuild_once(account, point):
    engine, replay = setup(account, two=True)
    value = capture(account, total=15000, two=True)
    def fault(actual):
        if actual == point: raise KeyboardInterrupt("fixture crash")
    account[0].records.fault = fault
    with pytest.raises(KeyboardInterrupt): replay.advance(value, action_id="queue")
    account[0].records.fault = lambda _: None
    replay.advance(value, action_id="queue")
    before, execution = account[0]._state()["value"], engine._state()["value"]
    assert sum(lot["quantity"] for lot in before["stock_lots"].values()) == 150
    account[0].records.rebuild(account[0].lease)
    assert account[0]._state()["value"] == before and engine._state()["value"] == execution


def test_minute_executor_cannot_reuse_a_queue_routed_capacity_window(account):
    engine, replay = setup(account)
    replay.advance(capture(account, ahead=10000), action_id="queue")
    value = minute(account, start="2026-08-03T09:30:00", end="2026-08-03T09:31:00")
    # Same canonical volume source, different model: route ownership still blocks it.
    with pytest.raises(Conflict): engine.advance_minute(value, action_id="minute-again")


def test_ambiguous_prefix_time_sequence_and_order_position_changes_are_rejected(account):
    engine, replay = setup(account)
    value = capture(account)
    with pytest.raises(ExecutionEvidenceMissing, match="prefix time"):
        replay.advance(value, action_id="inconsistent", through_at=dt("2026-08-03T09:30:25"), through_sequence=3)
    replay.advance(value, action_id="prefix", through_at=dt("2026-08-03T09:30:10"), through_sequence=3)
    value["positions"][0]["after_sequence"] = 1
    with pytest.raises(Conflict, match="queue position"):
        replay.advance(value, action_id="improved-position")


@pytest.mark.parametrize("auction", [False, True])
def test_complete_zero_volume_and_no_counterparty_is_no_fill_not_missing_evidence(account, auction):
    engine, replay = setup(account, at="2026-08-03T09:24:30" if auction else "2026-08-03T09:30:00")
    value = capture(account, auction=auction)
    value["snapshot"]["orders"] = value["snapshot"]["orders"][:1]
    value["events"] = []
    value["capture"]["end_sequence"] = 2
    value["volumes"][0]["shares"] = 0
    if auction: value["auction"].update(price=None, volume=0)
    replay.advance(value, action_id="queue")
    assert results(engine)["results"][0]["status"] == "model_no_fill"
    assert results(engine)["fills"] == []


def test_zero_auction_result_cannot_hide_executable_crossed_orders(account):
    engine, replay = setup(account, at="2026-08-03T09:24:30")
    value = capture(account, auction=True)
    value["events"] = []
    value["capture"]["end_sequence"] = 2
    value["volumes"][0]["shares"] = 0
    value["auction"].update(price=None, volume=0)
    with pytest.raises(ExecutionEvidenceMissing, match="unconsumed executable"):
        replay.advance(value, action_id="bad-zero-auction")


def test_prefix_fill_is_visible_only_after_its_receipt_and_later_cut_does_not_fill_twice(account):
    engine, replay = setup(account)
    value = capture(account)
    replay.advance(value, action_id="match-cut", through_at=dt("2026-08-03T09:30:30"), through_sequence=5)
    assert account[0].balances(at=dt("2026-08-03T09:30:30"), observed=True)["securities"] == {}
    assert account[0].balances(at=dt("2026-08-03T09:30:31"), observed=True)["securities"]["SH600000"]["total"] == 100
    value["positions"] = []  # No remaining live simulated order at the later cut.
    replay.advance(value, action_id="finish-window")
    assert results(engine, "finish-window")["fills"] == []
    assert next(iter(engine._state()["value"]["liquidity"].values()))["used"] == 100


def test_continuation_requires_exact_snapshot_of_prior_historical_end_book(account):
    engine, replay = setup(account)
    value = capture(account, ahead=10000)
    replay.advance(value, action_id="first-window")
    value["capture"].update(start_at=dt("2026-08-03T09:31:00"), end_at=dt("2026-08-03T09:32:00"), start_sequence=4, end_sequence=3)
    value["snapshot"]["last_sequence"] = 3
    value["events"] = []
    value["volumes"][0].update(start=dt("2026-08-03T09:31:00"), end=dt("2026-08-03T09:32:00"), shares=0)
    value["availability"] = availability(dt("2026-08-03T09:32:00"))
    with pytest.raises(ExecutionEvidenceMissing, match="continuous historical book"):
        replay.advance(value, action_id="invented-ahead-queue")


def test_prefix_event_references_full_host_proof_without_inlining_future_market_events(account):
    engine, replay = setup(account)
    value = capture(account)
    event = replay.advance(value, action_id="prefix-proof", through_at=dt("2026-08-03T09:30:10"), through_sequence=3)
    request = event["value"]["payload"]["request"]
    assert "evidence" not in request and "events" not in request
    assert request["evidence_artifact_hash"] == digest(value)
    raw = account[0].records.artifacts.read_json(request["evidence_artifact_hash"])
    assert len(raw["events"]) == 3
    assert [item["sequence"] for item in results(engine, "prefix-proof")["historical_audit"]] == [3]
