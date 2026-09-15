from datetime import date
from decimal import Decimal

import pytest

from advisor.research.experiments.settlement import SpecialSettlements, SettlementTerms, SettlementUnsupported, entitlement_snapshot
from advisor.research.experiments.valuation import AccountValuations, ValuationUnsupported
from advisor.research.experiments.account import AccountUnavailable
from advisor.research.experiments.resolution import digest
from tests.advisor.research.test_experiment_account import account, registry, bundle, dt, order, reserve_and_buy, close_quote
from tests.advisor.research.test_experiment_corporate import register


def terms(fixture, *, cash="800", shares=0, treatment="no_outstanding_obligations"):
    api = fixture[0]
    owned = entitlement_snapshot(api._state()["value"], "SH600000")
    return SettlementTerms.model_validate({
        "settlement_id": "synthetic-retirement", "kind": "merger" if shares else "delisting", "security": "SH600000",
        "effective_at": dt("2026-08-05T08:30:00"), "source_refs": ["synthetic-terms", "synthetic-allocation"],
        "source_hash": digest("synthetic-retirement-source"), "availability": close_quote(day="2026-08-04")["availability"],
        "entitlement_snapshot_hash": digest(owned),
        "allocations": [{"lot_id": key, "old_quantity": lot["quantity"], "cash_amount": cash,
            "replacement_quantity": shares, "allocation_source_ref": "synthetic-allocation",
            "replacement_cost_basis": str(Decimal(lot["cost_basis"]) * lot["quantity"] / shares) if shares else None} for key, lot in owned.items()],
        "cash_payment_at": dt("2026-08-06T08:30:00") if Decimal(cash) else None,
        "cash_valuation": "unconditional_fixed_claim", "replacement_security": "SH600001" if shares else None,
        "replacement_tradable_at": dt("2026-08-06T09:30:00") if shares else None,
        "holding_start": "inherit", "dividend_tax_treatment": treatment})


def mark(fixture, day, quotes):
    api = fixture[0]
    api.checkpoint(at=dt(day + "T15:00:00"), action_id="checkpoint-" + day)
    return AccountValuations(api).mark(purpose="daily", trade_date=date.fromisoformat(day),
        snapshot_at=dt(day + "T23:05:00"), closes=quotes, market_rules=[fixture[2]], action_id="nav-" + day)["value"]


def test_cash_retirement_values_fixed_claim_and_payment_preserves_nav(account):
    api = account[0]
    reserve_and_buy(account)
    value = terms(account)
    service = SpecialSettlements(api)
    first = service.apply(value, at=value.effective_at, action_id="retire")
    assert service.apply(value, at=value.effective_at, action_id="retire") == first
    balance = api.balances(at=value.effective_at)
    assert Decimal(balance["cash"]) == Decimal("198994.99")
    assert balance["securities"]["SH600000"]["total"] == 0
    result = mark(account, "2026-08-05", [])
    assert Decimal(result["qualified_receivables"]) == 800
    assert Decimal(result["nav"]) == Decimal("199794.99")
    paid = service.pay(value.settlement_id, at=value.cash_payment_at, action_id="cash")
    assert service.pay(value.settlement_id, at=value.cash_payment_at, action_id="cash") == paid
    assert Decimal(api.balances(at=value.cash_payment_at)["available_cash"]) == Decimal("199794.99")
    assert mark(account, "2026-08-06", [])["nav"] == result["nav"]


def test_conversion_uses_proven_allocated_shares_and_new_price_without_old_price_carry(account):
    api = account[0]
    reserve_and_buy(account)
    value = terms(account, cash="0", shares=51)
    SpecialSettlements(api).apply(value, at=value.effective_at, action_id="convert")
    position = api.balances(at=value.effective_at)["securities"]["SH600001"]
    assert position == {"total": 51, "sellable": 0, "frozen": 0}
    quote = {**close_quote(day="2026-08-05", price="20"), "security": "SH600001"}
    assert Decimal(mark(account, "2026-08-05", [quote])["nav"]) == Decimal("200014.99")
    assert api.balances(at=dt("2026-08-06T09:29:59"))["securities"]["SH600001"]["sellable"] == 0
    assert api.balances(at=value.replacement_tradable_at)["securities"]["SH600001"]["sellable"] == 51


def test_replacement_requires_its_own_qualified_price(account):
    reserve_and_buy(account)
    value = terms(account, cash="0", shares=50)
    SpecialSettlements(account[0]).apply(value, at=value.effective_at, action_id="convert")
    with pytest.raises(ValuationUnsupported, match="missing"):
        mark(account, "2026-08-05", [close_quote(day="2026-08-05")])


@pytest.mark.parametrize("bad", ["hash", "quantity", "unavailable", "time", "open-order", "pending-corporate"])
def test_bad_settlement_cannot_partially_retire_or_credit(account, bad):
    api = account[0]
    if bad == "pending-corporate":
        register(account)
    else:
        reserve_and_buy(account)
    value = terms(account).model_dump(mode="json")
    if bad == "hash": value["entitlement_snapshot_hash"] = digest("wrong")
    if bad == "quantity": value["allocations"][0]["old_quantity"] += 1
    if bad == "unavailable": value["availability"]["available_at"] = dt("2026-08-06T15:00:00").isoformat()
    if bad == "open-order":
        api.reserve_batch([order(account, "sell", side="sell")], at=dt("2026-08-05T08:00:00"), action_id="reserve-sell")
    before = api._state()
    with pytest.raises(SettlementUnsupported):
        SpecialSettlements(api).apply(value, at=dt("2026-08-05T09:00:00") if bad == "time" else dt("2026-08-05T08:30:00"), action_id="bad")
    assert api._state() == before


def test_tax_disposal_is_net_of_fixed_cash_claim_and_does_not_erase_tax(account):
    api = account[0]
    actions, dividend = register(account)
    actions.apply("action-one", at=dividend.effective_at, action_id="detach")
    actions.pay("action-one", at=dividend.payment_at, action_id="dividend-pay")
    service = SpecialSettlements(api)
    no_tax = terms(account)
    before = api._state()
    with pytest.raises(SettlementUnsupported, match="tax"):
        service.apply(no_tax, at=no_tax.effective_at, action_id="cannot-forgive-tax")
    assert api._state() == before
    value = terms(account, treatment="dispose")
    service.apply(value, at=value.effective_at, action_id="retire")
    state = api._state()["value"]
    assert Decimal(state["fees_assessed"]["dividend_tax"]) == 20
    assert Decimal(state["dividend_tax_reserve"]) == 0
    assert Decimal(state["receivables"]["settlement:synthetic-retirement"]["amount"]) == 780


def test_share_conversion_carries_tax_basis_and_original_holding_period(account):
    api = account[0]
    actions, dividend = register(account)
    actions.apply("action-one", at=dividend.effective_at, action_id="detach")
    actions.pay("action-one", at=dividend.payment_at, action_id="dividend-pay")
    value = terms(account, cash="0", shares=50, treatment="carry")
    SpecialSettlements(api).apply(value, at=value.effective_at, action_id="convert")
    obligation = next(iter(api._state()["value"]["dividend_obligations"].values()))
    assert obligation["shares_remaining"] == 50 and Decimal(obligation["basis_remaining"]) == 100
    assert obligation["acquired_on"] == "2026-08-03"
    api.reserve_batch([order(account, "sell-new", side="sell", security="SH600001", quantity=50, price="20")],
        at=dt("2026-08-06T09:30:00"), action_id="reserve-new")
    api.fill("sell-new", quantity=25, price="20", at=dt("2026-08-06T09:31:00"), action_id="sell-new-fill")
    assert Decimal(api._state()["value"]["fees_assessed"]["dividend_tax"]) == 10
    assert Decimal(api._state()["value"]["dividend_tax_reserve"]) == 10


@pytest.mark.parametrize("point", ["before_event", "after_event", "after_projection", "before_commit", "after_commit"])
def test_atomic_retirement_and_payment_rebuild_once(account, point):
    api = account[0]
    reserve_and_buy(account)
    value = terms(account)
    service = SpecialSettlements(api)
    def fault(actual):
        if actual == point: raise KeyboardInterrupt("fixture fault")
    api.records.fault = fault
    with pytest.raises(KeyboardInterrupt): service.apply(value, at=value.effective_at, action_id="retire")
    api.records.fault = lambda _: None
    service.apply(value, at=value.effective_at, action_id="retire")
    api.records.fault = fault
    with pytest.raises(KeyboardInterrupt): service.pay(value.settlement_id, at=value.cash_payment_at, action_id="pay")
    api.records.fault = lambda _: None
    service.pay(value.settlement_id, at=value.cash_payment_at, action_id="pay")
    before = api._state()["value"]
    api.records.rebuild(api.lease)
    assert api._state()["value"] == before
    assert sum(Decimal(lot["amount"]) for lot in before["cash_lots"]) == Decimal("199794.99")


def test_retired_security_cannot_be_bought_again_using_a_stale_market_rule(account):
    api = account[0]
    reserve_and_buy(account)
    value = terms(account)
    SpecialSettlements(api).apply(value, at=value.effective_at, action_id="retire")
    before = api._state()
    with pytest.raises(AccountUnavailable, match="retired"):
        api.reserve_batch([order(account, "buy-retired")], at=dt("2026-08-05T09:00:00"), action_id="stale-rule")
    assert api._state() == before


def test_complete_lot_allocation_is_required_not_only_a_valid_snapshot_hash(account):
    api = account[0]
    reserve_and_buy(account)
    api.reserve_batch([order(account, "buy-more")], at=dt("2026-08-04T09:24:30"), action_id="buy-more-reserve")
    api.fill("buy-more", quantity=100, price="10", at=dt("2026-08-04T09:25:00"), action_id="buy-more-fill")
    value = terms(account).model_dump(mode="json")
    value["allocations"] = value["allocations"][:1]
    before = api._state()
    with pytest.raises(SettlementUnsupported, match="every owned lot"):
        SpecialSettlements(api).apply(value, at=dt("2026-08-05T08:30:00"), action_id="partial-issuer-allocation")
    assert api._state() == before


@pytest.mark.parametrize("bad", ["fractional-cash", "missing-allocation-source", "mixed-tax-carry", "missing-share-basis", "same-security"])
def test_ambiguous_conversion_terms_are_not_silently_rounded_or_inferred(account, bad):
    reserve_and_buy(account)
    value = terms(account, cash="0", shares=51).model_dump(mode="json")
    if bad == "fractional-cash":
        value["allocations"][0]["cash_amount"] = "0.001"
        value["cash_payment_at"] = dt("2026-08-06T08:30:00").isoformat()
    elif bad == "missing-allocation-source": value["source_refs"] = ["synthetic-terms"]
    elif bad == "mixed-tax-carry":
        value["allocations"][0]["cash_amount"] = "100"
        value["cash_payment_at"] = dt("2026-08-06T08:30:00").isoformat()
        value["dividend_tax_treatment"] = "carry"
    elif bad == "missing-share-basis": value["allocations"][0]["replacement_cost_basis"] = None
    elif bad == "same-security": value["replacement_security"] = "SH600000"
    with pytest.raises(ValueError): SettlementTerms.model_validate(value)


def test_pure_share_conversion_cannot_silently_erase_the_carried_cost_basis(account):
    reserve_and_buy(account)
    value = terms(account, cash="0", shares=50).model_dump(mode="json")
    value["allocations"][0]["replacement_cost_basis"] = "0"
    before = account[0]._state()
    with pytest.raises(SettlementUnsupported, match="carried investment basis"):
        SpecialSettlements(account[0]).apply(value, at=dt("2026-08-05T08:30:00"), action_id="lost-basis")
    assert account[0]._state() == before
