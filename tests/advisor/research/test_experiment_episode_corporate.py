from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest

from advisor.research.experiments.account import SimulatedAccount
from advisor.research.experiments.episode import EpisodeReplay
from advisor.research.experiments.records import Fenced
from advisor.research.experiments.repository import Conflict
from tests.advisor.research.test_experiment_account import account, dt, close_quote
from tests.advisor.research.test_experiment_contracts import SESSIONS
from tests.advisor.research.test_experiment_corporate import terms as corporate_terms
from tests.advisor.research.test_experiment_episode import episode, registry, bundle, program, run, trade
from tests.advisor.research.test_experiment_settlement import terms as settlement_terms


def buy_and_hold(replay, fixture, phase):
    if phase.trading_date == replay.account.task.trading_dates[0]:
        trade(replay, fixture, phase)
    replay.close_phase(phase.phase_id, "completed")


def insert_points(value, points):
    # Issuer effects at the official close follow market events and precede the
    # close checkpoint. Equal-time issuer effects retain their supplied order.
    for point in reversed(points):
        index = next((i for i, old in enumerate(value["points"])
                      if old["at"] > point["at"] or
                      (old["at"] == point["at"] and old["kind"] not in {"minute", "queue", "workflow"})),
                     len(value["points"]))
        value["points"].insert(index, point)
    return value


def dividend_program(episode, *, kind="dividend", ratio="1", late_payment=False):
    value = program(episode)
    terms = corporate_terms(episode[1], kind, ratio).model_dump(mode="python")
    if late_payment:
        terms["payment_at"] = dt("2026-08-10T08:00:00")
        terms["action"]["payment_date"] = terms["payment_at"].date()
    points = [{"kind": "corporate_register", "at": terms["record_at"], "terms": terms}]
    if kind != "rights":
        points.append({"kind": "corporate_apply", "at": terms["effective_at"], "corporate_id": "action-one"})
    if kind == "dividend" and not late_payment:
        points.append({"kind": "corporate_pay", "at": terms["payment_at"], "corporate_id": "action-one"})
    for point in value["points"]:
        if point["kind"] == "valuation" and point["purpose"] != "initial":
            day = str(point["trade_date"])
            adjusted = day >= "2026-08-04"
            point["closes"] = [close_quote(day=day, price=("5" if kind in {"bonus", "split"} else "9")
                                          if adjusted else "10", actions=("action-one",) if adjusted else ())]
    return insert_points(value, points)


def marks(replay):
    return {key: replay.records.read(identity)["value"]
            for key, identity in replay._state()["value"]["valuations"].items()}


def blocked_command(replay):
    row = replay.records.db.execute("SELECT event_sequence FROM lagent_events WHERE test_id=? "
        "AND kind='episode_evidence_blocked' ORDER BY event_sequence DESC LIMIT 1", (replay.lease.test_id,)).fetchone()
    return replay.records.event(row[0])["value"]["payload"]["pending"]["command"]["kind"]


@pytest.mark.parametrize("sell", [False, True])
def test_dividend_replay_preserves_entitlement_tax_and_payment_nav(episode, sell):
    replay, fixture = episode
    replay.create(dividend_program(episode))
    observed = {}

    def research(replay, fixture, phase):
        if phase.kind == "postauction":
            observed[str(phase.trading_date)] = replay.account.balances(at=phase.boundary.snapshot_at, observed=True)
        if sell:
            trade(replay, fixture, phase)
            replay.close_phase(phase.phase_id, "completed")
        else:
            buy_and_hold(replay, fixture, phase)

    result, _ = run(replay, fixture, on_phase=research)
    assert result == {"kind": "ready_for_evaluation", "formal_ready": False}
    nav = marks(replay)
    assert Decimal(nav["daily:2026-08-03"]["nav"]) == Decimal("199974.99")
    assert Decimal(nav["daily:2026-08-04"]["qualified_receivables"]) == 100
    assert nav["daily:2026-08-04"]["nav"] == nav["daily:2026-08-05"]["nav"]
    assert nav["daily:2026-08-05"]["qualified_receivables"] == "0"
    state = replay.account._state()["value"]
    dividend = next(lot for lot in state["cash_lots"] if lot["lot_id"] == "dividend:action-one")
    assert Decimal(dividend["amount"]) == (80 if sell else 100)
    assert Decimal(state["dividend_tax_reserve"]) == (0 if sell else 20)
    assert Decimal(state["fees_assessed"].get("dividend_tax", "0")) == (20 if sell else 0)
    assert Decimal(state["payables"]) == 0
    assert Decimal(observed["2026-08-05"]["cash"]) - Decimal(nav["daily:2026-08-04"]["cash"]) == (80 if sell else 100)
    assert state["corporate_actions"]["action-one"]["status"] == "paid"


@pytest.mark.parametrize("kind,ratio,sellable", [("bonus", "1", 100), ("split", "2", 0)])
def test_share_replay_keeps_basis_nav_and_delayed_tradability(episode, kind, ratio, sellable):
    replay, fixture = episode
    replay.create(dividend_program(episode, kind=kind, ratio=ratio))
    positions = {}

    def research(replay, fixture, phase):
        if phase.kind == "postauction" and str(phase.trading_date) in {"2026-08-04", "2026-08-06"}:
            positions[str(phase.trading_date)] = replay.account.balances(at=phase.boundary.snapshot_at, observed=True)["securities"]["SH600000"]
        buy_and_hold(replay, fixture, phase)

    assert run(replay, fixture, on_phase=research)[0]["kind"] == "ready_for_evaluation"
    assert positions["2026-08-04"] == {"total": 200, "sellable": sellable, "frozen": 0}
    assert positions["2026-08-06"] == {"total": 200, "sellable": 200, "frozen": 0}
    nav = marks(replay)
    assert nav["daily:2026-08-03"]["nav"] == nav["terminal:2026-08-07"]["nav"]
    lots = replay.account._state()["value"]["stock_lots"].values()
    assert sum(Decimal(lot["cost_basis"]) * lot["quantity"] for lot in lots) == Decimal("1005.01")


def test_rights_replay_retains_nonparticipation_without_cash_or_shares(episode):
    replay, fixture = episode
    replay.create(dividend_program(episode, kind="rights"))
    assert run(replay, fixture, on_phase=buy_and_hold)[0]["kind"] == "ready_for_evaluation"
    state = replay.account._state()["value"]
    assert state["corporate_actions"]["action-one"]["status"] == "not_participating"
    assert state["receivables"] == {} and len(state["stock_lots"]) == 1
    assert marks(replay)["terminal:2026-08-07"]["market_value"] == "900"


def test_payment_beyond_original_endpoint_remains_a_receivable(episode):
    replay, fixture = episode
    replay.create(dividend_program(episode, late_payment=True))
    assert run(replay, fixture, on_phase=buy_and_hold)[0]["kind"] == "ready_for_evaluation"
    nav = marks(replay)["terminal:2026-08-07"]
    assert Decimal(nav["qualified_receivables"]) == 100
    assert Decimal(nav["nav"]) == Decimal("199974.99")
    state = replay.account._state()["value"]
    assert state["corporate_actions"]["action-one"]["distributed"] is False
    assert all(lot["lot_id"] != "dividend:action-one" for lot in state["cash_lots"])


@pytest.mark.parametrize("bad", ["missing_apply", "missing_pay", "duplicate", "wrong_time", "orphan",
                                  "checkpoint_first", "market_after", "duplicate_registration"])
def test_bad_corporate_lifecycle_is_rejected_before_replay_creation(episode, bad):
    replay, _ = episode
    value = dividend_program(episode)
    points = value["points"]
    registration = next(i for i, p in enumerate(points) if p["kind"] == "corporate_register")
    apply = next(i for i, p in enumerate(points) if p["kind"] == "corporate_apply")
    if bad == "missing_apply": points.pop(apply)
    elif bad == "missing_pay": points[:] = [p for p in points if p["kind"] != "corporate_pay"]
    elif bad == "duplicate": points.insert(apply, deepcopy(points[apply]))
    elif bad == "wrong_time": points[apply]["at"] += timedelta(seconds=1)
    elif bad == "orphan": points[apply]["corporate_id"] = "unknown"
    elif bad == "checkpoint_first": points[registration:registration + 2] = reversed(points[registration:registration + 2])
    elif bad == "market_after":
        points.insert(registration + 1, {"kind": "workflow", "at": points[registration]["at"], "evidence": []})
    else: points.insert(registration, deepcopy(points[registration]))
    with pytest.raises(Conflict): replay.create(value)
    assert replay.records.projection(replay.lease.test_id, "episode") is None
    assert replay.records.projection(replay.lease.test_id, "account") is None


@pytest.mark.parametrize("failure", ["fractional", "unavailable"])
def test_unsupported_corporate_evidence_blocks_without_partial_financial_effect(episode, failure):
    replay, fixture = episode
    value = dividend_program(episode, kind="bonus", ratio=".001") if failure == "fractional" else dividend_program(episode)
    if failure == "unavailable":
        terms = next(p["terms"] for p in value["points"] if p["kind"] == "corporate_register")
        terms["availability"]["available_at"] = dt("2026-08-04T15:00:00")
    replay.create(value)
    assert run(replay, fixture, on_phase=buy_and_hold)[0]["kind"] == "blocked"
    state = replay.account._state()["value"]
    assert len(state["stock_lots"]) == 1 and next(iter(state["stock_lots"].values()))["quantity"] == 100
    assert (not state["corporate_actions"] if failure == "unavailable" else
            state["corporate_actions"]["action-one"]["applied"] is False)
    assert replay._state()["value"]["pending"] is None
    assert blocked_command(replay) == ("corporate_register" if failure == "unavailable" else "corporate_apply")
    assert not any(key.startswith("terminal:") for key in marks(replay))
    before = replay.account._state()
    assert replay.step()["kind"] == "blocked" and replay.account._state() == before


INITIAL = {"initial_positions": [{"security": "SH600000", "quantity": 100,
                                  "acquired_on": "2026-07-01", "cost_basis": "8"}]}


def retirement_episode(account, *, shares=0, late_payment=False, treatment="no_outstanding_obligations"):
    api, now, rule, fees = account
    rule = deepcopy(rule)
    rule["continuous"][0]["end"] = "11:30:00"
    fixture = (api, now, rule, fees)
    replay = EpisodeReplay(api, main_actor_id="main")
    value = program((replay, fixture))
    value["points"][0]["trade_date"] = "2026-07-31"
    value["points"][0]["closes"] = [close_quote(day="2026-07-31", price="8")]
    terms = settlement_terms(fixture, cash="0" if shares else "800", shares=shares, treatment=treatment).model_dump(mode="python")
    if late_payment:
        terms["cash_payment_at"] = dt("2026-08-10T08:30:00")
    effects = [{"kind": "special_settlement", "at": terms["effective_at"], "terms": terms}]
    if terms["cash_payment_at"] and not late_payment:
        effects.append({"kind": "special_settlement_pay", "at": terms["cash_payment_at"],
                        "settlement_id": terms["settlement_id"]})
    for point in value["points"]:
        if point["kind"] == "valuation" and point["purpose"] != "initial":
            day = str(point["trade_date"])
            if day >= "2026-08-05":
                point["closes"] = [{**close_quote(day=day, price="16"), "security": "SH600001"}] if shares else []
    return replay, fixture, insert_points(value, effects)


def no_research(replay, fixture, phase):
    replay.close_phase(phase.phase_id, "completed")


@pytest.mark.parametrize("account", [INITIAL], indirect=True)
@pytest.mark.parametrize("shares,late", [(0, False), (0, True), (50, False)])
def test_retirement_replay_preserves_cash_claim_or_replacement_and_original_endpoint(account, shares, late):
    replay, fixture, value = retirement_episode(account, shares=shares, late_payment=late)
    replay.create(value)
    positions = {}

    def research(replay, fixture, phase):
        if shares and phase.kind == "postauction" and str(phase.trading_date) in {"2026-08-05", "2026-08-07"}:
            positions[str(phase.trading_date)] = replay.account.balances(at=phase.boundary.snapshot_at, observed=True)["securities"]["SH600001"]
        no_research(replay, fixture, phase)

    assert run(replay, fixture, on_phase=research)[0]["kind"] == "ready_for_evaluation"
    nav = marks(replay)
    assert nav["daily:2026-08-05"]["nav"] == nav["terminal:2026-08-07"]["nav"] == "200800"
    assert nav["terminal:2026-08-07"]["qualified_receivables"] == ("800" if late else "0")
    state = replay.account._state()["value"]
    assert state["stock_lots"]["initial:SH600000"]["quantity"] == 0
    assert state["orders"] == {}
    if shares:
        assert positions["2026-08-05"] == {"total": 50, "sellable": 0, "frozen": 0}
        assert positions["2026-08-07"] == {"total": 50, "sellable": 50, "frozen": 0}
    else:
        assert sum(lot["lot_id"] == "settlement:synthetic-retirement" for lot in state["cash_lots"]) == (0 if late else 1)


@pytest.mark.parametrize("account", [INITIAL], indirect=True)
@pytest.mark.parametrize("bad", ["missing_pay", "wrong_time", "duplicate", "orphan", "allocation", "unavailable", "replacement_price"])
def test_retirement_replay_rejects_bad_lifecycle_or_blocks_missing_evidence(account, bad):
    replay, fixture, value = retirement_episode(account, shares=50 if bad == "replacement_price" else 0)
    if bad in {"missing_pay", "wrong_time", "duplicate", "orphan"}:
        index = next(i for i, p in enumerate(value["points"]) if p["kind"] == "special_settlement_pay")
        if bad == "missing_pay": value["points"].pop(index)
        elif bad == "wrong_time": value["points"][index]["at"] += timedelta(seconds=1)
        elif bad == "duplicate": value["points"].insert(index, deepcopy(value["points"][index]))
        else: value["points"][index]["settlement_id"] = "unknown"
        with pytest.raises(Conflict): replay.create(value)
        assert replay.records.projection(replay.lease.test_id, "episode") is None
        return
    terms = next(p["terms"] for p in value["points"] if p["kind"] == "special_settlement")
    if bad == "allocation": terms["allocations"][0]["old_quantity"] = 99
    elif bad == "unavailable": terms["availability"]["available_at"] = dt("2026-08-06T15:00:00")
    else:
        for point in value["points"]:
            if point["kind"] == "valuation" and str(point["trade_date"]) >= "2026-08-05": point["closes"] = []
    replay.create(value)
    assert run(replay, fixture, on_phase=no_research)[0]["kind"] == "blocked"
    assert blocked_command(replay) == ("valuation" if bad == "replacement_price" else "special_settlement")
    state = replay.account._state()["value"]
    if bad != "replacement_price":
        assert state["stock_lots"]["initial:SH600000"]["quantity"] == 100
        assert state["receivables"] == {} and not state.get("special_settlements")
    assert not any(key.startswith("terminal:") for key in marks(replay))


def recover_effect(replay, fixture, value, kind, research):
    replay.create(value)
    for _ in range(250):
        pending = replay._state()["value"]["pending"]
        if pending and pending["command"]["kind"] == kind: break
        result = replay.step()
        if result["kind"] == "research_required":
            research(replay, fixture, replay.clock.schedule[replay.clock._state()["value"]["index"]])
        else: assert result["kind"] == "progress"
    else: raise AssertionError("corporate boundary was not prepared")
    cursor = replay._state()["value"]["cursor"]
    def crash(_): raise RuntimeError("committed issuer effect")
    replay.fault = crash
    with pytest.raises(RuntimeError, match="committed issuer"): replay.step()
    account_before = replay.account._state()
    fixture[1][0] += timedelta(seconds=10001)
    lease = replay.records.claim(replay.lease.test_id, worker_id="corporate-recovery", lease_seconds=10000)
    api = SimulatedAccount(replay.records, lease, calendar_sessions=SESSIONS, calendar_hash=replay.account.calendar_hash)
    recovered = EpisodeReplay(api, main_actor_id="main")
    with pytest.raises(Fenced): replay.step()
    recovered.records.rebuild(lease)
    recovered.step()
    assert api._state() == account_before
    assert recovered._state()["value"]["cursor"] == cursor + 1
    assert run(recovered, (api, *fixture[1:]), on_phase=research)[0]["kind"] == "ready_for_evaluation"
    return recovered


@pytest.mark.parametrize("kind", ["corporate_register", "corporate_apply", "corporate_pay"])
def test_new_owner_recovers_original_corporate_effect_without_duplicate_entitlement_or_tax(episode, kind):
    replay = recover_effect(*episode, dividend_program(episode), kind, buy_and_hold)
    assert marks(replay)["terminal:2026-08-07"]["nav"] == "199974.99"
    state = replay.account._state()["value"]
    assert len(state["corporate_actions"]) == len(state["dividend_obligations"]) == 1
    assert sum(lot["lot_id"] == "dividend:action-one" for lot in state["cash_lots"]) == 1


@pytest.mark.parametrize("account", [INITIAL], indirect=True)
@pytest.mark.parametrize("kind", ["special_settlement", "special_settlement_pay"])
def test_new_owner_recovers_original_retirement_or_payment_without_double_cash(account, kind):
    replay = recover_effect(*retirement_episode(account), kind, no_research)
    assert marks(replay)["terminal:2026-08-07"]["nav"] == "200800"
    state = replay.account._state()["value"]
    assert len(state["special_settlements"]) == 1
    assert sum(lot["lot_id"] == "settlement:synthetic-retirement" for lot in state["cash_lots"]) == 1


@pytest.mark.parametrize("account", [INITIAL], indirect=True)
@pytest.mark.parametrize("treatment,shares", [("dispose", 0), ("carry", 50)])
def test_dividend_and_retirement_replay_keep_tax_through_disposal_or_conversion(account, treatment, shares):
    replay, fixture, value = retirement_episode(account, treatment=treatment, shares=shares)
    dividends = dividend_program((replay, fixture))
    effects = [point for point in dividends["points"] if point["kind"].startswith("corporate_")]
    for point in value["points"]:
        if point["kind"] == "valuation" and point["purpose"] != "initial" and str(point["trade_date"]) < "2026-08-05":
            day = str(point["trade_date"])
            point["closes"] = [close_quote(day=day, price="9" if day == "2026-08-04" else "10",
                                          actions=("action-one",) if day == "2026-08-04" else ())]
    replay.create(insert_points(value, effects))
    assert run(replay, fixture, on_phase=no_research)[0]["kind"] == "ready_for_evaluation"
    state = replay.account._state()["value"]
    assert Decimal(marks(replay)["terminal:2026-08-07"]["nav"]) == 200890
    obligation = next(iter(state["dividend_obligations"].values()))
    if treatment == "dispose":
        assert Decimal(state["fees_assessed"]["dividend_tax"]) == 10
        assert Decimal(state["dividend_tax_reserve"]) == 0
        assert obligation["shares_remaining"] == 0
        assert Decimal(next(lot["amount"] for lot in state["cash_lots"]
                            if lot["lot_id"] == "settlement:synthetic-retirement")) == 790
    else:
        assert obligation["shares_remaining"] == 50
        assert Decimal(obligation["basis_remaining"]) == 100
        assert Decimal(state["dividend_tax_reserve"]) == 10
        assert obligation["lot_id"] == "settlement:synthetic-retirement:initial:SH600000"


def test_cancel_after_committed_detachment_reconciles_only_that_effect(episode):
    replay, fixture = episode
    replay.create(dividend_program(episode))
    for _ in range(150):
        pending = replay._state()["value"]["pending"]
        if pending and pending["command"]["kind"] == "corporate_apply": break
        result = replay.step()
        if result["kind"] == "research_required":
            buy_and_hold(replay, fixture, replay.clock.schedule[replay.clock._state()["value"]["index"]])
        else: assert result["kind"] == "progress"
    else: raise AssertionError("detachment was not prepared")
    def crash(_): raise RuntimeError("detachment committed")
    replay.fault = crash
    with pytest.raises(RuntimeError): replay.step()
    before, cursor = replay.account._state(), replay._state()["value"]["cursor"]
    replay.request_stop("cancelled")
    replay.fault = lambda _: None
    assert run(replay, fixture, on_phase=buy_and_hold)[0]["kind"] == "cancelled"
    assert replay.account._state() == before
    assert replay._state()["value"]["cursor"] == cursor + 1
    assert before["value"]["corporate_actions"]["action-one"]["distributed"] is False
    assert not any(key.startswith("terminal:") for key in marks(replay))


def test_same_time_distribution_requires_detachment_first(episode):
    replay, fixture = episode
    value = dividend_program(episode)
    terms = next(p["terms"] for p in value["points"] if p["kind"] == "corporate_register")
    terms["payment_at"] = terms["effective_at"]
    terms["action"]["payment_date"] = terms["effective_at"].date()
    payment = next(p for p in value["points"] if p["kind"] == "corporate_pay")
    value["points"].remove(payment)
    index = next(i for i, p in enumerate(value["points"]) if p["kind"] == "corporate_apply")
    payment["at"] = terms["effective_at"]
    value["points"].insert(index, payment)
    with pytest.raises(Conflict, match="detachment"): replay.create(value)
    value["points"][index:index + 2] = reversed(value["points"][index:index + 2])
    replay.create(value)
    assert run(replay, fixture, on_phase=buy_and_hold)[0]["kind"] == "ready_for_evaluation"
    assert marks(replay)["daily:2026-08-04"]["qualified_receivables"] == "0"
