from datetime import date
from decimal import Decimal

import pytest

from advisor.research.experiments.corporate import CorporateActions, CorporateTerms, CorporateUnsupported, DividendTaxPolicy
from advisor.research.experiments.valuation import AccountValuations, ValuationUnsupported
from advisor.research.experiments.resolution import digest
from tests.advisor.research.test_experiment_account import account, registry, bundle, dt, order, reserve_and_buy, close_quote


def policy():
    return DividendTaxPolicy.model_validate({
        "policy_ref": "synthetic-tax-v1", "effective_from": "2026-01-01", "effective_to": "2026-12-31",
        "source_refs": ["synthetic-tax-source"], "source_hash": digest("synthetic-tax"),
        "availability": close_quote(day="2026-08-03")["availability"],
        "scope": "ordinary_public_a_share", "holding_period": "calendar_month_acquisition_through_day_before_disposal",
        "disposal_order": "fifo", "withholding": "distribution_or_later_disposal",
        "bands": [{"up_to_months": 1, "rate": "0.2"}, {"up_to_months": 12, "rate": "0.1"}, {"up_to_months": None, "rate": "0"}]})


def terms(fixture, kind="dividend", ratio="1"):
    return CorporateTerms.model_validate({"security": "SH600000", "action": {
        "action_id": "action-one", "kind": kind, "record_date": "2026-08-03", "effective_date": "2026-08-04",
        "payment_date": "2026-08-05" if kind == "dividend" else None,
        "tradable_date": "2026-08-06" if kind in {"bonus", "split"} else None,
        "cash_per_share": "1" if kind == "dividend" else None,
        "shares_ratio": ratio if kind in {"bonus", "split"} else None, "rule_ref": "synthetic-issuer-terms"},
        "source_refs": ["synthetic-issuer-source"], "source_hash": digest("synthetic-issuer"),
        "availability": close_quote(day="2026-08-03")["availability"], "market_rule": fixture[2],
        "record_at": dt("2026-08-03T15:00:00"), "effective_at": dt("2026-08-04T08:00:00"),
        "payment_at": dt("2026-08-05T08:00:00") if kind == "dividend" else None,
        "tradable_at": dt("2026-08-06T09:00:00") if kind in {"bonus", "split"} else None,
        "taxable_per_old_share": "1" if kind == "dividend" else "0",
        "tax_policy": policy() if kind == "dividend" else None,
        "new_share_holding_start": "inherit" if kind in {"bonus", "split"} else None,
        "fractional_allocation": "exact_integer_required"})


def register(fixture, kind="dividend", ratio="1"):
    api = fixture[0]
    reserve_and_buy(fixture)
    actions = CorporateActions(api)
    value = terms(fixture, kind, ratio)
    actions.register(value, at=value.record_at, action_id="register")
    return actions, value


def mark(fixture, day, price, *, price_day=None, actions=("action-one",)):
    api = fixture[0]
    api.checkpoint(at=dt(day + "T15:00:00"), action_id="checkpoint-" + day)
    quote = close_quote(price=price, day=price_day or day, status="suspended" if price_day else "active", actions=actions)
    return AccountValuations(api).mark(purpose="daily", trade_date=date.fromisoformat(day),
        snapshot_at=dt(day + "T23:05:00"), closes=[quote], market_rules=[fixture[2]], action_id="nav-" + day)["value"]


@pytest.mark.parametrize("acquired,disposed,rate", [
    ("2026-01-08", "2026-02-08", ".2"), ("2026-01-08", "2026-02-09", ".1"),
    ("2026-01-31", "2026-02-28", ".2"), ("2026-01-31", "2026-03-01", ".1"),
    ("2025-08-03", "2026-08-03", ".1"), ("2025-08-03", "2026-08-04", "0"),
])
def test_calendar_holding_period_boundaries(acquired, disposed, rate):
    assert policy().rate(date.fromisoformat(acquired), date.fromisoformat(disposed)) == Decimal(rate)


def test_dividend_nav_does_not_double_count_attached_claim_and_payment_is_neutral(account):
    actions, value = register(account)
    before = account[0]._state()
    actions.register(value, at=value.record_at, action_id="register")
    assert account[0]._state() == before
    registered = mark(account, "2026-08-03", "10")
    assert registered["qualified_receivables"] == "0"
    assert Decimal(registered["nav"]) == Decimal("199974.99")
    actions.apply("action-one", at=value.effective_at, action_id="detach")
    detached = mark(account, "2026-08-04", "9")
    assert Decimal(detached["qualified_receivables"]) == 100
    assert detached["nav"] == registered["nav"]
    actions.pay("action-one", at=value.payment_at, action_id="pay")
    paid = mark(account, "2026-08-05", "9")
    assert paid["nav"] == detached["nav"] and paid["qualified_receivables"] == "0"


@pytest.mark.parametrize("before_payment", [True, False])
def test_partial_sale_collects_tax_once_and_deferred_tax_waits_for_distribution(account, before_payment):
    api = account[0]
    actions, value = register(account)
    actions.apply("action-one", at=value.effective_at, action_id="detach")
    if not before_payment:
        actions.pay("action-one", at=value.payment_at, action_id="pay")
    day = "2026-08-04" if before_payment else "2026-08-05"
    api.reserve_batch([order(account, "sell", side="sell", price="9")], at=dt(day + "T09:24:30"), action_id="sell-reserve")
    kwargs = dict(quantity=50, price="9", at=dt(day + "T09:25:00"), action_id="sell-fill")
    api.fill("sell", **kwargs)
    state = api._state()
    api.fill("sell", **kwargs)
    assert api._state() == state
    assert Decimal(state["value"]["dividend_tax_reserve"]) == 10
    assert Decimal(state["value"]["fees_assessed"]["dividend_tax"]) == 10
    assert Decimal(state["value"]["payables"]) == (10 if before_payment else 0)
    if before_payment:
        api.settle_payables(at=dt(day + "T09:26:00"), action_id="settle")
        assert Decimal(api._state()["value"]["payables"]) == 10
        actions.pay("action-one", at=value.payment_at, action_id="pay")
        assert Decimal(api._state()["value"]["payables"]) == 0
        assert Decimal(api._state()["value"]["cash_lots"][-1]["amount"]) == 90


@pytest.mark.parametrize("kind,ratio,total", [("bonus", "1", 200), ("split", "2", 200)])
def test_share_adjustment_preserves_nav_and_enforces_new_share_tradability(account, kind, ratio, total):
    api = account[0]
    actions, value = register(account, kind, ratio)
    actions.apply("action-one", at=value.effective_at, action_id="shares")
    balances = api.balances(at=dt("2026-08-04T09:00:00"))["securities"]["SH600000"]
    assert balances["total"] == total
    assert balances["sellable"] == (100 if kind == "bonus" else 0)
    assert Decimal(mark(account, "2026-08-04", "5")["nav"]) == Decimal("199994.99")
    assert api.balances(at=value.tradable_at)["securities"]["SH600000"]["sellable"] == total


def test_suspended_raw_close_is_adjusted_only_for_actions_after_the_price_event(account):
    actions, value = register(account)
    actions.apply("action-one", at=value.effective_at, action_id="detach")
    result = mark(account, "2026-08-04", "10", price_day="2026-08-03")
    assert result["marks"][0]["valuation_price"] == "9"
    assert result["marks"][0]["stale_valuation"] is True
    assert Decimal(result["nav"]) == Decimal("199974.99")


@pytest.mark.parametrize("apply,listed", [(False, ("action-one",)), (True, ()), (True, ("unknown",))])
def test_missing_application_or_action_coverage_blocks_nav(account, apply, listed):
    actions, value = register(account)
    if apply:
        actions.apply("action-one", at=value.effective_at, action_id="detach")
    with pytest.raises(ValuationUnsupported, match="corporate"):
        mark(account, "2026-08-04", "9", actions=listed)


def test_fractional_distribution_fails_atomically(account):
    actions, value = register(account, "bonus", ".001")
    before = account[0]._state()
    with pytest.raises(CorporateUnsupported, match="fractional"):
        actions.apply("action-one", at=value.effective_at, action_id="shares")
    assert account[0]._state() == before


def test_rights_default_does_not_subscribe_or_invent_cash_flows(account):
    actions, value = register(account, "rights")
    state = account[0]._state()["value"]
    assert state["corporate_actions"]["action-one"]["status"] == "not_participating"
    assert state["receivables"] == {} and state["dividend_obligations"] == {}
    assert Decimal(mark(account, "2026-08-04", "9")["nav"]) == Decimal("199894.99")


@pytest.mark.parametrize("account", [{"initial_positions": [{"security": "SH600000", "quantity": 100,
    "acquired_on": "2026-07-04", "cost_basis": "8"}]}], indirect=True)
def test_reserve_uses_current_holding_period_without_anticipating_future_sale(account):
    api = account[0]
    value = terms(account)
    actions = CorporateActions(api)
    actions.register(value, at=value.record_at, action_id="register")
    assert Decimal(api._state()["value"]["dividend_tax_reserve"]) == 20
    actions.apply("action-one", at=value.effective_at, action_id="detach")
    assert Decimal(api._state()["value"]["dividend_tax_reserve"]) == 20
    actions.pay("action-one", at=value.payment_at, action_id="pay")
    assert Decimal(api._state()["value"]["dividend_tax_reserve"]) == 10


@pytest.mark.parametrize("account", [{"initial_positions": [{"security": "SH600000", "quantity": 100,
    "acquired_on": "2025-07-01", "cost_basis": "8"}]}], indirect=True)
def test_later_order_filling_first_consumes_oldest_tax_lot_and_preserves_other_reservation(account):
    api = account[0]
    actions, value = register(account)
    actions.apply("action-one", at=value.effective_at, action_id="detach")
    actions.pay("action-one", at=value.payment_at, action_id="pay")
    api.reserve_batch([order(account, "first", side="sell", price="9"), order(account, "second", side="sell", price="9")],
                      at=dt("2026-08-05T09:24:30"), action_id="reserve-sales")
    api.fill("second", quantity=100, price="9", at=dt("2026-08-05T09:25:00"), action_id="second-first")
    state = api._state()["value"]
    assert Decimal(state["fees_assessed"].get("dividend_tax", "0")) == 0
    assert state["stock_lots"]["buy-fill"]["quantity"] == 100
    assert sum(state["orders"]["first"]["stock_reserved"].values()) == 100
    api.fill("first", quantity=100, price="9", at=dt("2026-08-05T09:26:00"), action_id="first-last")
    assert Decimal(api._state()["value"]["fees_assessed"]["dividend_tax"]) == 20
    before = api._state()["value"]
    api.records.rebuild(api.lease)
    assert api._state()["value"] == before


@pytest.mark.parametrize("point", ["before_event", "after_event", "after_projection", "before_commit", "after_commit"])
def test_dividend_payment_and_tax_reservation_survive_transaction_faults_once(account, point):
    api = account[0]
    actions, value = register(account)
    actions.apply("action-one", at=value.effective_at, action_id="detach")
    def fault(actual):
        if actual == point:
            raise KeyboardInterrupt("fixture fault")
    api.records.fault = fault
    with pytest.raises(KeyboardInterrupt):
        actions.pay("action-one", at=value.payment_at, action_id="pay")
    api.records.fault = lambda _: None
    result = actions.pay("action-one", at=value.payment_at, action_id="pay")
    assert actions.pay("action-one", at=value.payment_at, action_id="pay") == result
    before = api._state()["value"]
    assert sum(Decimal(lot["amount"]) for lot in before["cash_lots"]) == Decimal("199094.99")
    assert Decimal(before["dividend_tax_reserve"]) == 20
    api.records.rebuild(api.lease)
    assert api._state()["value"] == before


@pytest.mark.parametrize("bad", ["unavailable", "expired", "taxable-bonus", "delisting"])
def test_unproven_policies_and_special_events_fail_before_registration(account, bad):
    api = account[0]
    reserve_and_buy(account)
    value = terms(account, "bonus" if bad == "taxable-bonus" else "delisting" if bad == "delisting" else "dividend").model_dump(mode="json")
    if bad == "unavailable":
        value["tax_policy"]["availability"]["available_at"] = dt("2026-08-04T15:00:00").isoformat()
    elif bad == "expired":
        value["tax_policy"]["effective_to"] = "2026-08-02"
    elif bad == "taxable-bonus":
        value.update(taxable_per_old_share="1", tax_policy=policy().model_dump(mode="json"))
    before = api._state()
    with pytest.raises(CorporateUnsupported):
        CorporateActions(api).register(value, at=dt("2026-08-03T15:00:00"), action_id="bad")
    assert api._state() == before


def test_bonus_does_not_guess_allocation_of_existing_dividend_tax_units(account):
    actions, dividend = register(account)
    value = terms(account, "bonus").model_dump(mode="json")
    value["action"]["action_id"] = "bonus-two"
    actions.register(value, at=dividend.record_at, action_id="register-bonus")
    actions.apply("action-one", at=dividend.effective_at, action_id="detach")
    before = account[0]._state()
    with pytest.raises(CorporateUnsupported, match="tax-unit allocation"):
        actions.apply("bonus-two", at=dividend.effective_at, action_id="bonus")
    assert account[0]._state() == before


def test_simultaneous_actions_do_not_choose_an_arbitrary_stale_price_adjustment_order(account):
    actions, first = register(account, "bonus", ".1")
    second = terms(account, "bonus", ".1").model_dump(mode="json")
    second["action"]["action_id"] = "second-bonus"
    actions.register(second, at=first.record_at, action_id="register-second")
    actions.apply("action-one", at=first.effective_at, action_id="apply-first")
    actions.apply("second-bonus", at=first.effective_at, action_id="apply-second")
    with pytest.raises(ValuationUnsupported, match="composite price rule"):
        mark(account, "2026-08-04", "10", price_day="2026-08-03", actions=("action-one", "second-bonus"))
