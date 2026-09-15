"""Isolated cash-equity ledger. No production account tables or broker actions.

The host execution engine supplies accepted fills and confirmed cancellations.
Balances, reservations and receipts are durable events/projections of one Test.
"""
from datetime import date, datetime, timedelta
from copy import deepcopy
from decimal import Decimal
import re
from typing import Literal
from zoneinfo import ZoneInfo

from .contracts import Contract, Name, PositiveInt, PositiveDecimal, ResolvedSpecification
from .data.contracts import MarketRule
from .data.temporal import aware, Boundary
from .fees import OrderFees, rounded
from .records import Fenced, ProjectionUpdate
from .repository import Conflict
from .resolution import digest, verify_specification

ZERO = Decimal("0")
RESOURCE_HELD_ORDER_STATES = frozenset({"reserved", "partial", "cancel_pending", "pending_acceptance", "cancel_effective", "rejected_pending_receipt"})


class AccountUnavailable(ValueError):
    pass


class Reservation(Contract):
    order_id: Name
    security: Name
    side: Literal["buy", "sell"]
    quantity: PositiveInt
    limit_price: PositiveDecimal
    market_rule: MarketRule
    fee_schedule: dict


def dec(value):
    return Decimal(str(value))


def lagged_day(day, lag, sessions):
    if day not in sessions:
        raise AccountUnavailable("account event is outside the pinned trading calendar")
    index = sessions.index(day) + lag
    if index >= len(sessions):
        raise AccountUnavailable("settlement calendar coverage is insufficient")
    return sessions[index]


def _cash(state):
    return sum((dec(lot["amount"]) for lot in state["cash_lots"]), ZERO)


def _eligible_cash(lot, at, *, withdrawal=False):
    return (datetime.fromisoformat(lot["available_at"]) <= at
            and date.fromisoformat(lot["withdrawable_on" if withdrawal else "reusable_on"]) <= at.date())


def _frozen_cash(state, at):
    return (sum((dec(order["cash_reserved"]) for order in state["orders"].values()), ZERO)
            + sum((dec(hold["amount"]) for hold in state["cash_holds"] if datetime.fromisoformat(hold["until"]) > at), ZERO))


def _available_cash(state, at, *, withdrawal=False):
    liquid = sum((dec(lot["amount"]) for lot in state["cash_lots"] if _eligible_cash(lot, at, withdrawal=withdrawal)), ZERO)
    return max(ZERO, liquid - _frozen_cash(state, at) - dec(state["payables"]) - dec(state["liabilities"]))


def _stock_eligible(lot, at):
    return datetime.fromisoformat(lot["available_at"]) <= at and date.fromisoformat(lot["sellable_on"]) <= at.date()


def _stock_frozen(state, lot_id):
    return sum(order["stock_reserved"].get(lot_id, 0) for order in state["orders"].values())


def _spend_cash(state, amount, at):
    """Spend reusable lots; current-day non-withdrawable proceeds are netted first.

This allocation order is part of cash_equity_v1's simulation policy. It affects
withdrawable reporting, not total cash or trade NAV. No withdrawals are supported.
"""
    remaining = amount
    lots = sorted(state["cash_lots"], key=lambda lot: (_eligible_cash(lot, at, withdrawal=True), lot["available_at"], lot["lot_id"]))
    for lot in lots:
        if not _eligible_cash(lot, at):
            continue
        used = min(dec(lot["amount"]), remaining)
        lot["amount"] = str(dec(lot["amount"]) - used)
        remaining -= used
        if remaining == 0:
            break
    if remaining != 0:
        raise AccountUnavailable("confirmed reusable cash cannot fund the accepted execution")


class SimulatedAccount:
    def __init__(self, records, lease, *, calendar_sessions, calendar_hash):
        self.records, self.lease = records, lease
        test = records._test(lease.test_id)
        if test["value"].get("registration_version") != 1:
            raise Conflict("account requires a typed Test Record")
        definition = records.read(test["value"]["definition_id"])
        if definition["kind"] != "definition" or definition["experiment_id"] != test["experiment_id"]:
            raise Conflict("account definition mismatch")
        self.sealed = ResolvedSpecification.model_validate(definition["value"]["specification"])
        self.spec = verify_specification(self.sealed)
        self.task = next(task for task in self.sealed.tasks if task.task_id == test["value"]["task_id"])
        self.experiment_id = test["experiment_id"]
        self.sessions = tuple(calendar_sessions)
        if tuple(sorted(set(self.sessions))) != self.sessions or not set(self.task.trading_dates) <= set(self.sessions):
            raise AccountUnavailable("settlement requires the complete ordered task calendar")
        if calendar_hash != self.task.calendar_hash:
            raise Conflict("account calendar does not match the sealed task evidence")
        self.calendar_hash = calendar_hash
        if self.spec.account.policy_ref != "cash_equity_v1" or self.spec.account.available_credit != 0:
            raise AccountUnavailable("unsupported account policy or credit")
        if self.spec.account.cash_interest_rate != 0:
            raise AccountUnavailable("nonzero cash interest requires an explicit accrual policy")

    def _state(self):
        value = self.records.projection(self.lease.test_id, "account")
        if value is None:
            raise Conflict("simulated account has not been initialized")
        return value

    def _prior(self, action_id, request):
        row = self.records.db.execute("SELECT event_sequence FROM lagent_events WHERE test_id=? AND phase_id='account' AND action_id=? AND attempt=0",
                                      (self.lease.test_id, action_id)).fetchone()
        if row is None:
            return None
        event = self.records.event(row[0])
        if event["value"]["payload"]["request_hash"] != digest(request):
            raise Conflict("account action identity has different input")
        return event

    def stage(self):
        """Prepare several ledger mutations without publishing intermediate states."""
        return _StagedAccount(self)

    def _prepare_state(self, request, state, *, at, visible_at, entries):
        aware(at)
        aware(visible_at)
        at = at.astimezone(ZoneInfo(self.spec.clock.timezone))
        visible_at = visible_at.astimezone(ZoneInfo(self.spec.clock.timezone))
        if at < self.task.initial_as_of or visible_at < at:
            raise AccountUnavailable("account event time precedes its allowed boundary")
        sealed_through = state.get("sealed_through")
        if sealed_through is not None and at <= datetime.fromisoformat(sealed_through):
            raise AccountUnavailable("account replay interval is already sealed")
        if request["operation"] in {"initialize", "checkpoint"}:
            state["sealed_through"] = at.isoformat()
        prior_at = state.get("last_event_at")
        if prior_at is not None and datetime.fromisoformat(prior_at) > at:
            raise Conflict("account events must follow proven execution order")
        state["last_event_at"] = at.isoformat()
        state["visible_at"] = max(visible_at, datetime.fromisoformat(state.get("visible_at", visible_at.isoformat()))).isoformat()
        from .corporate import refresh_tax_reserves
        tax_entry = refresh_tax_reserves(state, at)
        entries = [*entries, tax_entry] if dec(tax_entry["amount"]) else entries
        self._validate(state, at)
        return entries

    def _commit(self, action_id, request, state, expected, *, at, visible_at, entries, extra_updates=(), guard=None):
        entries = self._prepare_state(request, state, at=at, visible_at=visible_at, entries=entries)
        return self.records.commit(self.lease, phase_id="account", action_id=action_id, attempt=0,
            kind="account_" + request["operation"], payload={"request_hash": digest(request), "request": request,
                "entries": entries, "visible_at": state["visible_at"]}, simulated_at=at,
            updates=(ProjectionUpdate("account", expected, state), *extra_updates), guard=guard)

    def _validate(self, state, at):
        if any(dec(lot["amount"]) < 0 for lot in state["cash_lots"]) or dec(state["payables"]) < 0 or dec(state["liabilities"]) < 0:
            raise Conflict("negative cash or invalid liability balance")
        if any(lot["quantity"] < _stock_frozen(state, key) for key, lot in state["stock_lots"].items()):
            raise Conflict("stock reservations exceed owned shares")
        if _frozen_cash(state, at) > _cash(state):
            raise Conflict("cash reservation exceeds owned cash")
        if any(dec(order["cash_reserved"]) < 0 for order in state["orders"].values()):
            raise Conflict("negative order reservation")

    def initialize(self, *, action_id):
        request = {"operation": "initialize", "specification_hash": self.sealed.specification_hash, "calendar_hash": self.calendar_hash}
        previous = self._prior(action_id, request)
        if previous is not None:
            return previous
        if self.records.projection(self.lease.test_id, "account") is not None:
            raise Conflict("account already exists")
        if self.records.status(self.lease.test_id) != "running":
            raise Fenced("account initialization requires a running test")
        at = self.task.initial_as_of
        cash = self.spec.account.initial_cash
        stocks = {}
        for position in self.spec.account.initial_positions:
            if position.acquired_on >= self.task.trading_dates[0]:
                raise AccountUnavailable("initial holdings require a proven earlier acquisition date")
            stocks["initial:" + position.security] = {"security": position.security, "quantity": position.quantity,
                "acquired_on": position.acquired_on.isoformat(), "sellable_on": self.task.trading_dates[0].isoformat(),
                "available_at": at.isoformat(), "cost_basis": str(position.cost_basis), "acquired_sequence": len(stocks)}
        state = {"policy_ref": "cash_equity_v1", "calendar_hash": self.calendar_hash,
                 "cash_lots": [{"lot_id": "initial", "amount": str(cash), "available_at": at.isoformat(),
                                "reusable_on": at.date().isoformat(), "withdrawable_on": at.date().isoformat()}],
                 "cash_holds": [], "stock_lots": stocks, "orders": {}, "receivables": {},
                 "payables": "0", "liabilities": "0", "fees_assessed": {}, "corporate_actions": {},
                 "next_lot_sequence": len(stocks), "dividend_obligations": {}, "dividend_tax_reserve": "0", "deferred_dividend_tax": "0"}
        return self._commit(action_id, request, state, None, at=at, visible_at=at,
                            entries=[{"account": "cash", "amount": str(cash), "reason": "initial_capital"}])

    def reserve_batch(self, orders, *, at, action_id):
        at = aware(at).astimezone(ZoneInfo(self.spec.clock.timezone))
        requests = tuple(Reservation.model_validate(order) for order in orders)
        if not requests or len({order.order_id for order in requests}) != len(requests):
            raise ValueError("nonempty batch with unique order identities required")
        if any(len({order.side for order in requests if order.security == security}) > 1 for security in {order.security for order in requests}):
            raise AccountUnavailable("conflicting same-security sides in one plan")
        request = {"operation": "reserve_batch", "orders": [order.model_dump(mode="json") for order in requests], "at": at.isoformat()}
        previous = self._prior(action_id, request)
        if previous is not None:
            return previous
        current = self._state()
        state = current["value"]
        entries = []
        for order in requests:
            if any(item["terms"]["security"] == order.security for item in state.get("special_settlements", {}).values()):
                raise AccountUnavailable("retired security cannot accept new reservations")
            if order.order_id in state["orders"]:
                raise Conflict("order identity already exists")
            rule = order.market_rule
            if not re.fullmatch(r"(?:SH|SZ)[0-9]{6}", order.security) or order.security[:2] != rule.exchange:
                raise AccountUnavailable("security identity and market rule disagree")
            if not rule.effective_from <= at.date() <= rule.effective_to or at.date() not in self.sessions:
                raise AccountUnavailable("market rule or trading calendar not effective")
            if rule.board not in self.spec.account.entitlements or (rule.state == "st" and "st" not in self.spec.account.entitlements):
                raise AccountUnavailable("account lacks the required security entitlement")
            fees = OrderFees(self.spec.execution, order.fee_schedule, trade_date=at.date(), exchange=rule.exchange)
            stored = {**order.model_dump(mode="json"), "trade_date": at.date().isoformat(), "filled_quantity": 0,
                      "turnover": "0", "cash_reserved": "0", "stock_reserved": {}, "fees": {}, "status": "reserved"}
            if order.side == "buy":
                reserve = rounded(order.limit_price * order.quantity, Decimal("0.01")) + fees.total("buy", order.limit_price * order.quantity)
                if reserve > _available_cash(state, at):
                    raise AccountUnavailable("batch exceeds confirmed available cash")
                stored["cash_reserved"] = str(reserve)
                entries.append({"account": "cash_frozen", "amount": str(reserve), "order_id": order.order_id})
            else:
                remaining = order.quantity
                for lot_id, lot in sorted(state["stock_lots"].items(), key=lambda item: (item[1]["acquired_on"], item[1].get("acquired_sequence", 0), item[0])):
                    if lot["security"] != order.security or not _stock_eligible(lot, at):
                        continue
                    quantity = min(remaining, lot["quantity"] - _stock_frozen(state, lot_id))
                    stored["stock_reserved"][lot_id] = quantity
                    remaining -= quantity
                    if not remaining:
                        break
                if remaining:
                    raise AccountUnavailable("batch exceeds confirmed sellable shares")
                entries.append({"account": "shares_frozen", "quantity": order.quantity, "security": order.security, "order_id": order.order_id})
            state["orders"][order.order_id] = stored
        return self._commit(action_id, request, state, current["sequence"], at=at, visible_at=at, entries=entries)

    def fill(self, order_id, *, quantity, price, at, action_id):
        at, price = aware(at).astimezone(ZoneInfo(self.spec.clock.timezone)), dec(price)
        if type(quantity) is not int or quantity <= 0 or not price.is_finite() or price <= 0:
            raise ValueError("positive integral fill quantity and finite price required")
        request = {"operation": "fill", "order_id": order_id, "quantity": quantity, "price": str(price), "at": at.isoformat()}
        previous = self._prior(action_id, request)
        if previous is not None:
            return previous
        current = self._state()
        state = current["value"]
        order = state["orders"].get(order_id)
        if order is None or order["status"] not in {"reserved", "partial", "cancel_pending"}:
            raise AccountUnavailable("order is not executable")
        if at.date().isoformat() != order["trade_date"] or quantity > order["quantity"] - order["filled_quantity"]:
            raise AccountUnavailable("fill is outside the order date or remaining quantity")
        side, limit = order["side"], dec(order["limit_price"])
        if (side == "buy" and price > limit) or (side == "sell" and price < limit):
            raise AccountUnavailable("fill violates the limit price")
        rule = MarketRule.model_validate(order["market_rule"])
        fees = OrderFees(self.spec.execution, order["fee_schedule"], trade_date=at.date(), exchange=rule.exchange)
        gross = rounded(price * quantity, Decimal("0.01"))
        before, after = dec(order["turnover"]), dec(order["turnover"]) + gross
        charges = fees.incremental(side, before, after)
        charge = sum(charges.values(), ZERO)
        receipt = at + timedelta(milliseconds=self.spec.execution.receipt_latency_ms)
        remaining = order["quantity"] - order["filled_quantity"] - quantity
        entries = []
        if side == "buy":
            old_reserve = dec(order["cash_reserved"])
            new_reserve = (rounded(limit * remaining, Decimal("0.01")) + fees.total(side, after + limit * remaining) - fees.total(side, after)) if remaining else ZERO
            released = old_reserve - new_reserve - gross - charge
            if released < 0:
                raise Conflict("fill exceeds its maximum reserved cost")
            _spend_cash(state, gross + charge, at)
            order["cash_reserved"] = str(new_reserve)
            state["cash_holds"].append({"amount": str(released), "until": receipt.isoformat(), "reason": "unconfirmed_price_improvement"})
            sellable = lagged_day(at.date(), rule.bought_shares_sell_lag, self.sessions)
            state["stock_lots"][action_id] = {"security": order["security"], "quantity": quantity,
                "acquired_on": at.date().isoformat(), "sellable_on": sellable.isoformat(), "available_at": receipt.isoformat(),
                "cost_basis": str((gross + charge) / quantity), "acquired_sequence": state.get("next_lot_sequence", 0)}
            state["next_lot_sequence"] = state.get("next_lot_sequence", 0) + 1
            entries.extend([{"account": "cash", "amount": str(-gross - charge)}, {"account": "shares", "security": order["security"], "quantity": quantity}])
        else:
            from .corporate import assess_disposal
            _reallocate_sell_fifo(state, order_id, at)
            pending, dividend_tax = quantity, ZERO
            for lot_id, reserved in order["stock_reserved"].items():
                used = min(pending, reserved)
                tax, tax_entries = assess_disposal(state, lot_id, used, at)
                dividend_tax += tax
                entries.extend(tax_entries)
                state["stock_lots"][lot_id]["quantity"] -= used
                order["stock_reserved"][lot_id] -= used
                pending -= used
                if not pending:
                    break
            if pending:
                raise Conflict("fill exceeds reserved shares")
            net = gross - charge - dividend_tax
            cash_change = net
            if net < 0:
                # A tiny first partial can owe more minimum commission than its
                # proceeds. Accrue the shortfall; never invent negative cash or credit.
                payable = -net
                free = _available_cash(state, at)
                paid = min(payable, free)
                _spend_cash(state, paid, at)
                state["payables"] = str(dec(state["payables"]) + payable - paid)
                entries.append({"account": "payables", "amount": str(payable - paid)})
                cash_change = -paid
                net = ZERO
            if net:
                state["cash_lots"].append({"lot_id": action_id, "amount": str(net), "available_at": receipt.isoformat(),
                    "reusable_on": lagged_day(at.date(), rule.sold_cash_reuse_lag, self.sessions).isoformat(),
                    "withdrawable_on": lagged_day(at.date(), rule.sold_cash_withdraw_lag, self.sessions).isoformat()})
            entries.extend([{"account": "cash", "amount": str(cash_change)}, {"account": "shares", "security": order["security"], "quantity": -quantity}])
        order["filled_quantity"] += quantity
        order["turnover"], order["fees"] = str(after), {key: str(value) for key, value in fees.cumulative(side, after).items()}
        if not remaining:
            order["status"] = "filled"
        elif order["status"] != "cancel_pending":
            order["status"] = "partial"
        for key, value in charges.items():
            state["fees_assessed"][key] = str(dec(state["fees_assessed"].get(key, "0")) + value)
            entries.append({"account": "fee_expense:" + key, "amount": str(value), "order_id": order_id})
        return self._commit(action_id, request, state, current["sequence"], at=at, visible_at=receipt, entries=entries)

    def cancel_request(self, order_id, *, at, action_id):
        at = aware(at).astimezone(ZoneInfo(self.spec.clock.timezone))
        request = {"operation": "cancel_request", "order_id": order_id, "at": at.isoformat()}
        previous = self._prior(action_id, request)
        if previous is not None:
            return previous
        current = self._state()
        order = current["value"]["orders"].get(order_id)
        if order is None or order["status"] not in {"reserved", "partial"}:
            raise AccountUnavailable("order cannot request cancellation")
        order["status"] = "cancel_pending"
        return self._commit(action_id, request, current["value"], current["sequence"], at=at, visible_at=at, entries=[])

    def release(self, order_id, *, at, action_id, reason):
        at = aware(at).astimezone(ZoneInfo(self.spec.clock.timezone))
        if reason not in {"cancel_confirmed", "day_expired", "rejected"}:
            raise ValueError("resource release requires a confirmed terminal reason")
        request = {"operation": "release", "order_id": order_id, "reason": reason, "at": at.isoformat()}
        previous = self._prior(action_id, request)
        if previous is not None:
            return previous
        current = self._state()
        order = current["value"]["orders"].get(order_id)
        if order is None or order["status"] in {"filled", "cancel_confirmed", "day_expired", "rejected"}:
            raise AccountUnavailable("order has no releasable resources")
        if reason == "cancel_confirmed" and order["status"] not in {"cancel_pending", "cancel_effective"}:
            raise AccountUnavailable("cancellation has not been requested")
        order["cash_reserved"], order["stock_reserved"], order["status"] = "0", {}, reason
        return self._commit(action_id, request, current["value"], current["sequence"], at=at, visible_at=at, entries=[{"account": "resources_released", "order_id": order_id, "reason": reason}])

    def balances(self, *, at, observed=False, snapshot_at=None):
        at = aware(at).astimezone(ZoneInfo(self.spec.clock.timezone))
        snapshot_at = aware(snapshot_at).astimezone(ZoneInfo(self.spec.clock.timezone)) if snapshot_at is not None else at
        if snapshot_at < at:
            raise ValueError("account snapshot precedes its event cutoff")
        if observed:
            state = None
            for row in self.records.db.execute("SELECT event_sequence FROM lagent_events WHERE test_id=? AND phase_id='account' ORDER BY event_sequence", (self.lease.test_id,)):
                event = self.records.event(row[0])
                if datetime.fromisoformat(event["simulated_at"]) <= at and datetime.fromisoformat(event["value"]["payload"]["visible_at"]) <= snapshot_at:
                    state = event["value"]["updates"][0]["value"]
            if state is None:
                raise AccountUnavailable("account has no confirmed snapshot at the boundary")
        else:
            state = self._state()["value"]
            if datetime.fromisoformat(state["last_event_at"]) > at:
                raise AccountUnavailable("current account cannot be read at an earlier event time")
        if observed:
            at = snapshot_at
        securities = {}
        for lot_id, lot in state["stock_lots"].items():
            item = securities.setdefault(lot["security"], {"total": 0, "sellable": 0, "frozen": 0})
            item["total"] += lot["quantity"]
            frozen = _stock_frozen(state, lot_id)
            item["frozen"] += frozen
            if _stock_eligible(lot, at):
                item["sellable"] += lot["quantity"] - frozen
        return {"cash": str(_cash(state)), "available_cash": str(_available_cash(state, at)),
                "frozen_cash": str(_frozen_cash(state, at)), "withdrawable_cash": str(_available_cash(state, at, withdrawal=True)),
                "securities": securities, "receivables": state["receivables"], "payables": state["payables"],
                "liabilities": state["liabilities"], "fees_assessed": state["fees_assessed"], "policy_ref": state["policy_ref"]}


    def observe(self, boundary):
        boundary = Boundary.model_validate(boundary)
        if boundary.market_timezone != self.spec.clock.timezone:
            raise Conflict("account observation timezone differs from the specification")
        return self.balances(at=boundary.event_cutoff, observed=True, snapshot_at=boundary.snapshot_at)


    def checkpoint(self, *, at, action_id):
        """Host replay engine seals a fully processed interval before NAV marking."""
        at = aware(at).astimezone(ZoneInfo(self.spec.clock.timezone))
        request = {"operation": "checkpoint", "at": at.isoformat()}
        previous = self._prior(action_id, request)
        if previous is not None:
            return previous
        current = self._state()
        return self._commit(action_id, request, current["value"], current["sequence"], at=at, visible_at=at, entries=[])

    def settle_payables(self, *, at, action_id):
        at = aware(at).astimezone(ZoneInfo(self.spec.clock.timezone))
        request = {"operation": "settle_payables", "at": at.isoformat()}
        previous = self._prior(action_id, request)
        if previous is not None:
            return previous
        current = self._state()
        state = current["value"]
        liquid = sum((dec(lot["amount"]) for lot in state["cash_lots"] if _eligible_cash(lot, at)), ZERO)
        amount = min(dec(state["payables"]) - dec(state.get("deferred_dividend_tax", "0")),
                     max(ZERO, liquid - _frozen_cash(state, at) - dec(state["liabilities"])))
        _spend_cash(state, amount, at)
        state["payables"] = str(dec(state["payables"]) - amount)
        return self._commit(action_id, request, state, current["sequence"], at=at, visible_at=at,
                            entries=[{"account": "cash", "amount": str(-amount)}, {"account": "payables", "amount": str(-amount)}])



def _reallocate_sell_fifo(state, executing_id, at):
    """Reservations freeze fungible shares; actual disposal always consumes FIFO.

An earlier order's provisional lot assignment must not let a later-filled order
choose a newer tax lot. Reassign reservations without changing any frozen total.
"""
    executing = state["orders"][executing_id]
    orders = [order for order in state["orders"].values()
              if order["security"] == executing["security"] and order["side"] == "sell"
              and order["status"] in RESOURCE_HELD_ORDER_STATES]
    orders = [executing, *(order for order in orders if order is not executing)]
    for order in orders:
        order["stock_reserved"] = {}
    for order in orders:
        remaining = order["quantity"] - order["filled_quantity"]
        for lot_id, lot in sorted(state["stock_lots"].items(), key=lambda item: (item[1]["acquired_on"], item[1].get("acquired_sequence", 0), item[0])):
            if lot["security"] != order["security"] or not _stock_eligible(lot, at):
                continue
            quantity = min(remaining, lot["quantity"] - _stock_frozen(state, lot_id))
            if quantity:
                order["stock_reserved"][lot_id] = quantity
                remaining -= quantity
            if not remaining:
                break
        if remaining:
            raise AccountUnavailable("FIFO disposal cannot preserve existing share reservations")


class _StagedAccount(SimulatedAccount):
    """In-memory account reducer used by atomic execution/plan commits."""
    def __init__(self, account):
        for name in ("records", "lease", "sealed", "spec", "task", "experiment_id", "sessions", "calendar_hash"):
            setattr(self, name, getattr(account, name))
        initial = account._state()
        self.expected_sequence, self.value = initial["sequence"], initial["value"]
        self.effects = []
        self.entries = []
        self._actions = {}

    def _state(self):
        return {"sequence": self.expected_sequence, "value": deepcopy(self.value)}

    def _prior(self, action_id, request):
        prior = self._actions.get(action_id)
        if prior is not None:
            if prior["request_hash"] != digest(request):
                raise Conflict("staged account action identity has different input")
            return prior
        return None

    def _commit(self, action_id, request, state, expected, *, at, visible_at, entries):
        if expected != self.expected_sequence:
            raise Conflict("staged account revision mismatch")
        entries = self._prepare_state(request, state, at=at, visible_at=visible_at, entries=entries)
        effect = {"action_id": action_id, "request_hash": digest(request), "request": request,
                  "at": at.isoformat(), "visible_at": state["visible_at"], "entries": entries}
        self.value = state
        self.effects.append(effect)
        self.entries.extend(entries)
        self._actions[action_id] = effect
        return effect
