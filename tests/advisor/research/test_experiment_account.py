from datetime import date, datetime, timedelta
from decimal import Decimal
import json

import pytest

from advisor.research.experiments.account import SimulatedAccount, AccountUnavailable
from advisor.research.experiments.contracts import original_case
from advisor.research.experiments.data.temporal import Boundary
from advisor.research.experiments.fees import OrderFees, FeeUnavailable
from advisor.research.experiments.repository import Conflict
from advisor.research.experiments.records import Fenced
from advisor.research.experiments.resolution import resolve
from tests.advisor.research.test_experiment_contracts import calendar, SESSIONS
from tests.advisor.research.test_experiment_registration import registry, candidate, plan_input
from tests.advisor.research.test_experiment_bundles import bundle


def dt(value):
    return datetime.fromisoformat(value + "+08:00")


@pytest.fixture
def account(registry, bundle, request):
    service, experiment, _, package, real = registry
    raw = original_case()
    raw["account"].update(getattr(request, "param", {}))
    sealed = resolve(raw, calendar=calendar).specification
    definition = service.definition(experiment, sealed, submission_identity="account-definition")
    candidate(service, experiment, package)
    plan = service.test_plan(experiment, definition["record_id"], plan_input(definition, plan_id="account-plan"),
                             submission_identity="account-plan", purpose="tuning")
    test_id = plan["tests"][0]["record_id"]
    records = service.records
    records.transition(test_id, "preflight", action_id="fixture-preflight")
    records.transition(test_id, "queued", action_id="fixture-queued")
    lease = records.claim(test_id, worker_id="account-fixture", lease_seconds=10000)
    records.transition(test_id, "running", action_id="fixture-running", lease=lease)
    api = SimulatedAccount(records, lease, calendar_sessions=SESSIONS, calendar_hash=sealed.tasks[0].calendar_hash)
    api.initialize(action_id="initialize")
    yield api, real, bundle[4]["rules"][0]["payload"], bundle[4]["fees"][0]["payload"]


def order(fixture, identity="buy", *, side="buy", quantity=100, price="10", security="SH600000"):
    _, _, rule, fees = fixture
    return {"order_id": identity, "security": security, "side": side, "quantity": quantity, "limit_price": price,
            "market_rule": rule, "fee_schedule": fees}


def reserve_and_buy(fixture, *, quantity=100, price="10"):
    api, *_ = fixture
    api.reserve_batch([order(fixture, quantity=quantity, price=price)], at=dt("2026-08-03T09:24:30"), action_id="reserve")
    api.fill("buy", quantity=quantity, price=price, at=dt("2026-08-03T09:25:00"), action_id="buy-fill")


def test_cumulative_minimum_fee_is_charged_once_and_new_orders_are_independent(account):
    _, _, _, schedule = account
    fees = OrderFees(original_case()["execution"], schedule, trade_date=date(2026, 8, 3), exchange="SH")
    assert fees.total("buy", "0") == 0
    first, second = fees.incremental("buy", "0", "1000"), fees.incremental("buy", "1000", "2000")
    assert first["commission"] == 5 and second["commission"] == 0
    assert sum(first.values()) + sum(second.values()) == fees.total("buy", "2000")
    assert fees.cumulative("sell", "1000")["stamp_duty"] == Decimal("0.50")  # synthetic fixture rate only
    assert fees.total("buy", "1000") * 2 > fees.total("buy", "2000")
    with pytest.raises(FeeUnavailable): OrderFees(original_case()["execution"], schedule, trade_date=date(2026, 9, 1), exchange="SH")


def test_fee_package_rejects_missing_or_double_charged_items(account):
    _, _, _, schedule = account
    bad = {**schedule, "items": schedule["items"][:-1]}
    with pytest.raises(ValueError): OrderFees(original_case()["execution"], bad, trade_date=date(2026, 8, 3), exchange="SH")
    included = {**schedule["items"][1], "fee_id": "exchange_fee", "included_in_commission": True}
    adjusted = {**schedule, "items": [*schedule["items"], included]}
    fees = OrderFees(original_case()["execution"], adjusted, trade_date=date(2026, 8, 3), exchange="SH")
    assert fees.cumulative("buy", "1000")["exchange_fee"] == 0
    adjusted["items"][-1] = {**included, "included_in_commission": False}
    with pytest.raises(FeeUnavailable): OrderFees(original_case()["execution"], adjusted, trade_date=date(2026, 8, 3), exchange="SH")


def test_batch_reservations_are_atomic_and_never_spend_expected_sale_proceeds(account):
    api, *_ = account
    before = api._state()
    with pytest.raises(AccountUnavailable):
        api.reserve_batch([order(account, "first"), order(account, "too-large", quantity=30000)], at=dt("2026-08-03T09:24:30"), action_id="bad-batch")
    assert api._state() == before
    with pytest.raises(AccountUnavailable):
        api.reserve_batch([order(account, "sell", side="sell"), order(account, "buy")], at=dt("2026-08-03T09:24:30"), action_id="crossing")
    assert api._state() == before
    with pytest.raises(AccountUnavailable):
        api.reserve_batch([order(account, "sell-unowned", side="sell")], at=dt("2026-08-03T09:24:30"), action_id="short")


def test_partial_buy_releases_only_confirmed_improvement_and_reserves_incremental_fees(account):
    api, *_ = account
    at = dt("2026-08-03T09:24:30")
    api.reserve_batch([order(account, quantity=200)], at=at, action_id="reserve")
    reserved = api.balances(at=at)
    assert reserved["frozen_cash"] == "2005.02"
    fill_at = dt("2026-08-03T09:25:00")
    api.fill("buy", quantity=100, price="9", at=fill_at, action_id="fill-1")
    actual = api.balances(at=fill_at)
    assert actual["available_cash"] == reserved["available_cash"]
    unseen = api.balances(at=fill_at, observed=True)
    assert unseen["cash"] == "200000" and unseen["securities"] == {}
    confirmed = api.observe(Boundary(event_cutoff=fill_at, snapshot_at=fill_at + timedelta(seconds=5), market_timezone="Asia/Shanghai"))
    assert confirmed["cash"] == "199094.99" and confirmed["available_cash"] == "198094.98"
    assert confirmed["securities"]["SH600000"] == {"total": 100, "sellable": 0, "frozen": 0}
    api.fill("buy", quantity=100, price="10", at=dt("2026-08-03T09:31:00"), action_id="fill-2")
    after = api.balances(at=dt("2026-08-03T09:31:01"))
    assert after["fees_assessed"]["commission"] == "5.00" and after["frozen_cash"] == "0"
    assert after["cash"] == "198094.98"


def test_sellable_date_and_confirmed_net_proceeds_differ_from_withdrawable_cash(account):
    api, *_ = account
    reserve_and_buy(account)
    with pytest.raises(AccountUnavailable):
        api.reserve_batch([order(account, "same-day-sell", side="sell")], at=dt("2026-08-03T09:29:00"), action_id="t0")
    api.reserve_batch([order(account, "sell", side="sell")], at=dt("2026-08-04T09:24:30"), action_id="sell-reserve")
    before = api.balances(at=dt("2026-08-04T09:24:30"))
    api.fill("sell", quantity=50, price="10", at=dt("2026-08-04T09:25:00"), action_id="sell-fill")
    early = api.balances(at=dt("2026-08-04T09:25:00"))
    confirmed = api.balances(at=dt("2026-08-04T09:25:01"))
    assert early["available_cash"] == before["available_cash"]
    assert Decimal(confirmed["available_cash"]) - Decimal(before["available_cash"]) == Decimal("494.74")
    assert confirmed["withdrawable_cash"] == before["withdrawable_cash"]
    assert confirmed["securities"]["SH600000"] == {"total": 50, "sellable": 0, "frozen": 50}
    later = api.balances(at=dt("2026-08-05T09:00:00"))
    assert later["withdrawable_cash"] == later["available_cash"]


def test_cancellation_keeps_resources_and_old_order_can_fill_until_confirmation(account):
    api, *_ = account
    api.reserve_batch([order(account)], at=dt("2026-08-03T09:24:30"), action_id="reserve")
    api.cancel_request("buy", at=dt("2026-08-03T09:29:00"), action_id="cancel-request")
    assert api.balances(at=dt("2026-08-03T09:29:00"))["frozen_cash"] == "1005.01"
    api.fill("buy", quantity=50, price="10", at=dt("2026-08-03T09:30:00"), action_id="racing-fill")
    api.release("buy", at=dt("2026-08-03T09:30:01"), action_id="cancel-confirm", reason="cancel_confirmed")
    assert api.balances(at=dt("2026-08-03T09:30:01"))["frozen_cash"] == "0"
    assert api._state()["value"]["orders"]["buy"]["filled_quantity"] == 50
    api.reserve_batch([order(account, "replacement", quantity=50)], at=dt("2026-08-03T09:30:02"), action_id="replace-reserve")
    api.fill("replacement", quantity=50, price="10", at=dt("2026-08-03T09:31:00"), action_id="replace-fill")
    assert api.balances(at=dt("2026-08-03T09:31:01"))["fees_assessed"]["commission"] == "10.00"


def test_unfilled_order_release_has_no_fee_and_retries_do_not_repeat_balances(account):
    api, *_ = account
    at = dt("2026-08-03T09:24:30")
    first = api.reserve_batch([order(account)], at=at, action_id="reserve")
    assert api.reserve_batch([order(account)], at=at, action_id="reserve") == first
    with pytest.raises(Conflict): api.reserve_batch([order(account, quantity=200)], at=at, action_id="reserve")
    api.release("buy", at=dt("2026-08-03T15:00:01"), action_id="expiry", reason="day_expired")
    result = api.balances(at=dt("2026-08-03T15:00:01"))
    assert result["cash"] == "200000" and result["fees_assessed"] == {} and result["frozen_cash"] == "0"


@pytest.mark.parametrize("point", ["before_event", "after_event", "after_projection", "before_commit", "after_commit"])
def test_cash_side_effects_survive_transaction_faults_once(account, point):
    api, *_ = account
    api.reserve_batch([order(account)], at=dt("2026-08-03T09:24:30"), action_id="reserve")
    def fail(actual):
        if actual == point: raise KeyboardInterrupt("fixture fault")
    api.records.fault = fail
    with pytest.raises(KeyboardInterrupt): api.fill("buy", quantity=100, price="10", at=dt("2026-08-03T09:25:00"), action_id="fill")
    api.records.fault = lambda _: None
    first = api.fill("buy", quantity=100, price="10", at=dt("2026-08-03T09:25:00"), action_id="fill")
    assert api.fill("buy", quantity=100, price="10", at=dt("2026-08-03T09:25:00"), action_id="fill") == first
    assert api.balances(at=dt("2026-08-03T09:25:01"))["cash"] == "198994.99"
    api.records.rebuild(api.lease)
    assert api.balances(at=dt("2026-08-03T09:25:01"))["cash"] == "198994.99"


@pytest.mark.parametrize("account", [{"initial_cash": "1005.01"}], indirect=True)
def test_tiny_sale_minimum_fee_shortfall_is_a_payable_not_invented_credit(account):
    api, *_ = account
    reserve_and_buy(account)
    api.reserve_batch([order(account, "sell", side="sell", price="1")], at=dt("2026-08-04T09:24:30"), action_id="sell")
    api.fill("sell", quantity=1, price="1", at=dt("2026-08-04T09:25:00"), action_id="tiny-fill")
    result = api.balances(at=dt("2026-08-04T09:25:01"))
    assert result["cash"] == "0.00" and Decimal(result["payables"]) == 4 and Decimal(result["available_cash"]) == 0
    with pytest.raises(AccountUnavailable): api.reserve_batch([order(account, "unfunded")], at=dt("2026-08-04T09:29:00"), action_id="unfunded")


def test_actual_and_observed_snapshots_do_not_expose_later_account_events(account):
    api, *_ = account
    reserve_and_buy(account)
    result = api.observe(Boundary(event_cutoff=dt("2026-08-03T09:24:00"), snapshot_at=dt("2026-08-03T09:25:05"), market_timezone="Asia/Shanghai"))
    assert result["cash"] == "200000" and result["frozen_cash"] == "0" and result["securities"] == {}
    with pytest.raises(AccountUnavailable): api.balances(at=dt("2026-08-03T09:24:00"))
    api.records.db.execute("DELETE FROM lagent_projections WHERE test_id=? AND name='account'", (api.lease.test_id,))
    api.records.db.commit()
    api.records.rebuild(api.lease)
    assert api.balances(at=dt("2026-08-03T09:25:01"))["cash"] == "198994.99"


def close_quote(*, price="11", day="2026-08-07", status="active", actions=()):
    from advisor.research.experiments.data.temporal import Availability
    at = dt(day + "T15:00:00")
    return {"security": "SH600000", "trade_date": day, "price": price, "adjustment": "raw", "status": status,
            "availability": Availability(event_at=at, available_at=at, fetched_at=at, version_public_at=at,
                                         time_quality="historical_publication_declared").model_dump(mode="json"),
            "source_ref": "explicit-fixture-close", "corporate_actions_complete": True, "corporate_action_ids": actions}


def test_raw_close_nav_is_sealed_and_research_cost_never_debits_principal(account):
    from advisor.research.experiments.valuation import AccountValuations, ValuationUnsupported
    api, _, rule, _ = account
    marks = AccountValuations(api)
    initial = marks.mark(purpose="initial", trade_date=date(2026, 8, 2), snapshot_at=dt("2026-08-02T23:05:00"), closes=[], market_rules=[rule], action_id="initial-nav")
    reserve_and_buy(account)
    args = dict(purpose="terminal", trade_date=date(2026, 8, 7), snapshot_at=dt("2026-08-07T23:05:00"),
                closes=[close_quote()], market_rules=[rule], action_id="end-nav")
    with pytest.raises(ValuationUnsupported, match="sealed"): marks.mark(**args)
    api.checkpoint(at=dt("2026-08-07T15:00:00"), action_id="replay-complete")
    terminal = marks.mark(**args)
    assert marks.mark(**args) == terminal
    assert Decimal(terminal["value"]["nav"]) == Decimal("200094.99")
    result = marks.net_return(initial["record_id"], terminal["record_id"])
    assert Decimal(result["net_return"]) == Decimal("0.00047495") and not result["formal_ready"]
    api.records.put(experiment_id=api.experiment_id, kind="cost", record_id="research-cost-fixture", submission_identity="research-cost-fixture",
                    value={"amount_usd": "123", "scope": "research"}, links=(("test", api.lease.test_id),))
    assert marks.mark(**args) == terminal
    assert terminal["value"]["forced_liquidation"] is False
    api.records.rebuild(api.lease)
    assert marks.mark(**args) == terminal
    for table in ("ledger_transactions", "positions"):
        assert api.records.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("account", [{"initial_cash": "300000"}], indirect=True)
def test_configurable_capital_and_idle_cash_have_zero_terminal_return(account):
    from advisor.research.experiments.valuation import AccountValuations
    api, _, rule, _ = account
    marks = AccountValuations(api)
    start = marks.mark(purpose="initial", trade_date=date(2026, 8, 2), snapshot_at=dt("2026-08-02T23:05:00"), closes=[], market_rules=[rule], action_id="start")
    api.checkpoint(at=dt("2026-08-07T15:00:00"), action_id="close")
    end = marks.mark(purpose="terminal", trade_date=date(2026, 8, 7), snapshot_at=dt("2026-08-07T23:05:00"), closes=[], market_rules=[rule], action_id="end")
    assert marks.net_return(start["record_id"], end["record_id"])["net_return"] == "0"
    assert Decimal(end["value"]["nav"]) == 300000


@pytest.mark.parametrize("bad", ["missing", "adjusted", "future", "corporate", "delisting", "not_close"])
def test_unsupported_valuation_never_defaults_to_zero_or_stale_invalid_price(account, bad):
    from advisor.research.experiments.valuation import AccountValuations, ValuationUnsupported
    api, _, rule, _ = account
    reserve_and_buy(account)
    api.checkpoint(at=dt("2026-08-07T15:00:00"), action_id="end-checkpoint")
    quote = close_quote()
    if bad == "adjusted": quote["adjustment"] = "qfq"
    if bad == "future": quote = close_quote(day="2026-08-10")
    if bad == "corporate": quote["corporate_action_ids"] = ["unimplemented-dividend"]
    if bad == "delisting": quote["status"] = "delisting"
    if bad == "not_close": quote["availability"]["event_at"] = dt("2026-08-07T14:00:00").isoformat()
    with pytest.raises(ValueError):
        AccountValuations(api).mark(purpose="terminal", trade_date=date(2026, 8, 7), snapshot_at=dt("2026-08-07T23:05:00"),
                                   closes=[] if bad == "missing" else [quote], market_rules=[rule], action_id="invalid-nav")
    assert api.records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='valuation'").fetchone()[0] == 0


def test_suspended_security_uses_qualified_carry_price_without_forced_liquidation(account):
    from advisor.research.experiments.valuation import AccountValuations
    api, _, rule, _ = account
    reserve_and_buy(account)
    api.checkpoint(at=dt("2026-08-07T15:00:00"), action_id="end-checkpoint")
    result = AccountValuations(api).mark(purpose="terminal", trade_date=date(2026, 8, 7), snapshot_at=dt("2026-08-07T23:05:00"),
                                       closes=[close_quote(day="2026-08-06", status="suspended")], market_rules=[rule], action_id="stale")
    assert result["value"]["marks"][0]["stale_valuation"]
    assert result["value"]["marks"][0]["quantity"] == 100 and result["value"]["forced_liquidation"] is False


def test_replay_checkpoint_prevents_late_insertions_into_a_valued_interval(account):
    api, *_ = account
    api.reserve_batch([order(account)], at=dt("2026-08-03T09:24:30"), action_id="reserve")
    api.checkpoint(at=dt("2026-08-03T15:00:00"), action_id="sealed")
    with pytest.raises(AccountUnavailable): api.fill("buy", quantity=100, price="10", at=dt("2026-08-03T15:00:00"), action_id="late-fill")
    assert api.balances(at=dt("2026-08-03T15:00:00"))["cash"] == "200000"


@pytest.mark.parametrize("account", [{"initial_cash": "1005.01"}], indirect=True)
def test_payable_settlement_conserves_net_assets_and_cash_journal_replays(account):
    api, *_ = account
    reserve_and_buy(account)
    api.reserve_batch([order(account, "sell", side="sell", price="1")], at=dt("2026-08-04T09:24:30"), action_id="sell")
    api.fill("sell", quantity=1, price="1", at=dt("2026-08-04T09:25:00"), action_id="tiny")
    api.fill("sell", quantity=10, price="1", at=dt("2026-08-04T09:31:00"), action_id="later")
    at = dt("2026-08-04T09:31:01")
    before = api.balances(at=at)
    api.settle_payables(at=at, action_id="settlement")
    after = api.balances(at=at)
    assert Decimal(after["payables"]) == 0 and after["available_cash"] == before["available_cash"]
    assert Decimal(after["cash"]) == Decimal(before["cash"]) - Decimal(before["payables"])
    journal = Decimal("0")
    for sequence, in api.records.db.execute("SELECT event_sequence FROM lagent_events WHERE test_id=? AND phase_id='account' ORDER BY event_sequence", (api.lease.test_id,)):
        for entry in api.records.event(sequence)["value"]["payload"]["entries"]:
            if entry["account"] == "cash": journal += Decimal(entry["amount"])
    assert journal == Decimal(after["cash"])


@pytest.mark.parametrize("account", [{"initial_cash": "10000", "initial_positions": [{"security": "SH600000", "quantity": 100,
                                                                                          "acquired_on": "2026-07-01", "cost_basis": "8"}]}], indirect=True)
def test_initial_holdings_nav_uses_qualified_raw_price_not_historical_cost_basis(account):
    from advisor.research.experiments.valuation import AccountValuations
    api, _, rule, _ = account
    mark = AccountValuations(api).mark(purpose="initial", trade_date=date(2026, 7, 31), snapshot_at=dt("2026-08-02T23:05:00"),
                                      closes=[close_quote(price="10", day="2026-07-31")], market_rules=[rule], action_id="initial-nav")
    assert Decimal(mark["value"]["nav"]) == 11000
    assert api.balances(at=dt("2026-08-03T09:24:00"))["securities"]["SH600000"]["sellable"] == 100
