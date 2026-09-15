from decimal import Decimal

import pytest

from advisor.research.experiments.execution import ExecutionEngine, ExecutionEvidenceMissing, ExchangeRejected
from advisor.research.experiments.repository import Conflict
from advisor.research.experiments.records import Fenced
from advisor.research.experiments.resolution import digest
from tests.advisor.research.test_experiment_account import account, registry, bundle, dt, order


@pytest.fixture(autouse=True)
def extended_synthetic_session(account):
    # The bundle coverage fixture intentionally has only two minutes. Execution
    # scenarios declare a longer synthetic interval to exercise consecutive bars.
    account[2]["continuous"][0]["end"] = "11:30:00"


def availability(at):
    return {"event_at": at, "available_at": at, "fetched_at": at, "version_public_at": at,
            "time_quality": "historical_publication_declared"}


def proof(fixture, at, *, security="SH600000"):
    rule = fixture[2]
    cage = rule["price_cage_fraction"] is not None or rule["price_cage_ticks"] is not None
    return {"security": security, "at": at, "availability": availability(at), "source_refs": ["synthetic-acceptance"],
            "source_hash": digest("synthetic-acceptance"), "price_bounds": {"lower": "9", "upper": "11"},
            "buy_cage_upper": "11" if cage else None, "sell_cage_lower": "9" if cage else None,
            "sell_minimum": 100, "sell_increment": 100, "sell_remainder_policy": "entire_available_remainder"}


def minute(fixture, *, start="2026-08-03T09:31:00", end="2026-08-03T09:32:00", volume=10000, high="9.99", low="9.90", price="9.95"):
    return {"security": "SH600000", "market_rule": fixture[2], "bar": {
        "open": price, "high": high, "low": low, "close": price, "volume": volume, "volume_unit": "share", "adjustment": "raw",
        "interval_start": dt(start), "interval_end": dt(end)}, "price_bounds": {"lower": "9", "upper": "11"},
        "availability": availability(dt(end)), "source_refs": ["synthetic-minute"], "source_hash": digest("synthetic-minute"),
        "trading": "active", "corporate_actions_complete": True, "queue_required_reasons": []}


def accept(fixture, orders=None, at="2026-08-03T09:30:00"):
    engine = ExecutionEngine(fixture[0])
    engine.accept_batch(orders or [order(fixture)], [proof(fixture, dt(at))], at=dt(at), action_id="accept")
    return engine


def result(engine):
    return list(engine._state()["value"]["minutes"].values())[-1]["results"]


def test_complete_minute_uses_adverse_high_plus_tick_and_real_receipt_gating(account):
    api = account[0]
    engine = accept(account)
    first = engine.advance_minute(minute(account), action_id="minute")
    assert engine.advance_minute(minute(account), action_id="minute") == first
    assert result(engine)[0] == {"order_id": "buy", "status": "filled", "reason": None, "quantity": 100,
        "price": "10.00", "model": "conservative_minute_v1", "accepted_at": dt("2026-08-03T09:30:00").isoformat(), "priority": 0}
    assert api.balances(at=dt("2026-08-03T09:32:00"), observed=True)["securities"] == {}
    assert api.balances(at=dt("2026-08-03T09:32:01"), observed=True)["securities"]["SH600000"]["total"] == 100
    assert Decimal(api._state()["value"]["cash_lots"][0]["amount"]) == Decimal("198994.99")
    assert len(first["value"]["updates"]) == 2


@pytest.mark.parametrize("at", ["2026-08-03T09:31:00", "2026-08-03T09:31:30"])
def test_submission_minute_cannot_be_backfilled_even_at_the_minute_boundary(account, at):
    engine = accept(account, at=at)
    engine.advance_minute(minute(account), action_id="incomplete")
    assert result(engine)[0]["reason"] == "incomplete_submission_minute"
    assert account[0]._state()["value"]["stock_lots"] == {}
    engine.advance_minute(minute(account, start="2026-08-03T09:32:00", end="2026-08-03T09:33:00"), action_id="complete")
    assert result(engine)[0]["quantity"] == 100


def test_touching_or_crossing_limit_is_model_no_fill_without_forgiving_missing_evidence(account):
    engine = accept(account)
    engine.advance_minute(minute(account, high="10.00"), action_id="touch")
    assert result(engine)[0]["reason"] == "limit_or_market_bound"
    assert result(engine)[0]["status"] == "model_no_fill"
    assert account[0]._state()["value"]["stock_lots"] == {}


def test_capacity_is_shared_by_all_orders_and_acceptance_order_survives_json_sorting(account):
    engine = accept(account, [order(account, "z-first"), order(account, "a-second")])
    value = minute(account, volume=15099)
    engine.advance_minute(value, action_id="capacity")
    assert [(item["order_id"], item["quantity"]) for item in result(engine)] == [("z-first", 100), ("a-second", 50)]
    capacity = next(iter(engine._state()["value"]["liquidity"].values()))
    assert capacity["total"] == capacity["used"] == 150
    before = account[0]._state()
    with pytest.raises(Conflict, match="already consumed"):
        engine.advance_minute(value, action_id="another-model-or-identity")
    assert account[0]._state() == before
    assert Decimal(before["value"]["fees_assessed"]["commission"]) == 10


def test_partial_order_preserves_fee_reserve_and_next_minute_does_not_charge_minimum_again(account):
    engine = accept(account, [order(account, quantity=200)])
    engine.advance_minute(minute(account), action_id="first")
    assert result(engine)[0]["status"] == "partial"
    assert Decimal(account[0]._state()["value"]["orders"]["buy"]["cash_reserved"]) == Decimal("1000.01")
    engine.advance_minute(minute(account, start="2026-08-03T09:32:00", end="2026-08-03T09:33:00"), action_id="second")
    assert result(engine)[0]["status"] == "filled"
    assert Decimal(account[0]._state()["value"]["fees_assessed"]["commission"]) == 5


@pytest.mark.parametrize("bad", ["limit-queue", "partial-halt", "auction", "queue-flag", "future", "corporate", "wrong-tick", "cancel-race"])
def test_required_evidence_failure_leaves_capacity_and_all_account_effects_unchanged(account, bad):
    engine = accept(account)
    value = minute(account)
    if bad == "limit-queue": value["bar"]["high"] = "11"
    elif bad == "partial-halt": value["trading"] = "partial_halt"
    elif bad == "auction":
        value = minute(account, start="2026-08-03T14:59:00", end="2026-08-03T15:00:00")
    elif bad == "queue-flag": value["queue_required_reasons"] = ["resume"]
    elif bad == "future": value["availability"]["available_at"] = dt("2026-08-03T09:33:00")
    elif bad == "corporate": value["corporate_actions_complete"] = False
    elif bad == "wrong-tick": value["bar"]["high"] = "9.999"
    elif bad == "cancel-race": account[0].cancel_request("buy", at=dt("2026-08-03T09:30:30"), action_id="cancel")
    before, execution = account[0]._state(), engine._state()
    with pytest.raises(ExecutionEvidenceMissing): engine.advance_minute(value, action_id="bad-evidence")
    assert account[0]._state() == before and engine._state() == execution


def test_suspension_with_zero_volume_is_explained_no_fill(account):
    engine = accept(account)
    value = minute(account, volume=0)
    value["trading"] = "suspended"
    engine.advance_minute(value, action_id="suspended")
    assert result(engine)[0]["reason"] == "suspended"
    assert result(engine)[0]["quantity"] == 0


@pytest.mark.parametrize("bad", ["buy-lot", "tick", "bound", "cage", "missing-cage", "outside-session", "future-source"])
def test_exchange_validation_rejects_whole_batch_before_reserving_cash(account, bad):
    at = dt("2026-08-03T09:30:00")
    orders = [order(account, "good"), order(account, "bad")]
    evidence = proof(account, at)
    expected = ExchangeRejected
    if bad == "buy-lot": orders[1]["quantity"] = 50
    elif bad == "tick": orders[1]["limit_price"] = "10.001"
    elif bad == "bound": orders[1]["limit_price"] = "12"
    elif bad in {"cage", "missing-cage"}:
        for value in orders:
            value["market_rule"] = {**value["market_rule"], "price_cage_fraction": ".02"}
        evidence["buy_cage_upper"] = "9.99" if bad == "cage" else None
        if bad == "missing-cage": expected = ExecutionEvidenceMissing
    elif bad == "outside-session":
        at = dt("2026-08-03T16:00:00")
        evidence = proof(account, at)
    elif bad == "future-source":
        evidence["availability"]["available_at"] = dt("2026-08-03T09:31:00")
        expected = ExecutionEvidenceMissing
    before = account[0]._state()
    with pytest.raises(expected): ExecutionEngine(account[0]).accept_batch(orders, [evidence], at=at, action_id="invalid")
    assert account[0]._state() == before


@pytest.mark.parametrize("point", ["before_event", "after_event", "after_projection", "before_commit", "after_commit"])
def test_multi_fill_account_and_shared_capacity_commit_or_recover_together(account, point):
    api = account[0]
    engine = accept(account, [order(account, "first"), order(account, "second")])
    value = minute(account, volume=15000)
    def fault(actual):
        if actual == point: raise KeyboardInterrupt("fixture crash")
    api.records.fault = fault
    with pytest.raises(KeyboardInterrupt): engine.advance_minute(value, action_id="advance")
    api.records.fault = lambda _: None
    first = engine.advance_minute(value, action_id="advance")
    assert engine.advance_minute(value, action_id="advance") == first
    before, execution = api._state()["value"], engine._state()["value"]
    assert sum(lot["quantity"] for lot in before["stock_lots"].values()) == 150
    assert next(iter(execution["liquidity"].values()))["used"] == 150
    api.records.rebuild(api.lease)
    assert api._state()["value"] == before and engine._state()["value"] == execution


@pytest.mark.parametrize("account", [{"initial_positions": [{"security": "SH600000", "quantity": 150,
    "acquired_on": "2026-07-01", "cost_basis": "8"}]}], indirect=True)
def test_whole_odd_remainder_sell_uses_low_minus_tick_and_net_proceeds(account):
    engine = accept(account, [order(account, "sell", side="sell", quantity=150, price="9")])
    engine.advance_minute(minute(account, volume=15000), action_id="sell-minute")
    assert result(engine)[0]["quantity"] == 150 and result(engine)[0]["price"] == "9.89"
    state = account[0]._state()["value"]
    assert Decimal(state["cash_lots"][-1]["amount"]) == Decimal("1477.75")


def test_stale_worker_cannot_publish_prepared_execution(account):
    api, real, *_ = account
    engine = accept(account)
    before, execution = api._state(), engine._state()
    from datetime import timedelta
    real[0] += timedelta(seconds=10001)
    with pytest.raises(Fenced): engine.advance_minute(minute(account), action_id="stale")
    assert api._state() == before and engine._state() == execution


def test_acceptance_retry_is_idempotent_but_changed_source_or_order_conflicts(account):
    engine = accept(account)
    at = dt("2026-08-03T09:30:00")
    before, execution = account[0]._state(), engine._state()
    engine.accept_batch([order(account)], [proof(account, at)], at=at, action_id="accept")
    assert account[0]._state() == before and engine._state() == execution
    with pytest.raises(Conflict):
        engine.accept_batch([order(account, quantity=200)], [proof(account, at)], at=at, action_id="accept")
    changed = minute(account)
    engine.advance_minute(changed, action_id="minute")
    changed["source_hash"] = digest("different-source")
    with pytest.raises(Conflict): engine.advance_minute(changed, action_id="minute")


def test_batch_cannot_publish_first_order_when_later_order_has_insufficient_cash(account):
    api = account[0]
    engine = ExecutionEngine(api)
    at = dt("2026-08-03T09:30:00")
    before = api._state()
    from advisor.research.experiments.account import AccountUnavailable
    with pytest.raises(AccountUnavailable):
        engine.accept_batch([order(account, "first"), order(account, "too-much", quantity=30000)],
                            [proof(account, at)], at=at, action_id="no-funds")
    assert api._state() == before and engine._state()["sequence"] is None


@pytest.mark.parametrize("account", [{"initial_positions": [{"security": "SH600000", "quantity": 100,
    "acquired_on": "2026-07-01", "cost_basis": "8"}]}], indirect=True)
def test_later_batch_cannot_introduce_opposing_live_orders(account):
    engine = accept(account)
    at = dt("2026-08-03T09:30:30")
    before = account[0]._state()
    with pytest.raises(ExchangeRejected, match="opposing live"):
        engine.accept_batch([order(account, "sell", side="sell")], [proof(account, at)], at=at, action_id="self-cross")
    assert account[0]._state() == before


def test_shared_capacity_uses_source_share_precision_and_can_explain_zero_fill(account):
    account[2]["fill_increment"] = 10
    engine = accept(account, [order(account, "first"), order(account, "second")])
    engine.advance_minute(minute(account, volume=10999), action_id="rounded-cap")
    assert [item["quantity"] for item in result(engine)] == [100, 0]
    assert result(engine)[1]["reason"] == "shared_capacity_exhausted"


def test_successful_staging_does_not_escape_before_durable_commit_and_stale_stage_fails(account):
    api = account[0]
    stage = api.stage()
    at = dt("2026-08-03T09:30:00")
    before = api._state()
    stage.reserve_batch([order(account)], at=at, action_id="staged")
    assert api._state() == before and stage.value["orders"]["buy"]["cash_reserved"] == "1005.01"
    api.reserve_batch([order(account, "another")], at=at, action_id="concurrent")
    after = api._state()
    with pytest.raises(Conflict, match="revision changed"):
        ExecutionEngine(api)._commit("stale-stage", {"operation": "test-stale-stage"},
                                    ExecutionEngine(api)._state(), stage, at)
    assert api._state() == after and ExecutionEngine(api)._state()["sequence"] is None
