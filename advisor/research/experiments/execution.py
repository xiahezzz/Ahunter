"""Fixed-path execution reducers with shared durable historical liquidity.

This host-only market executor is paired with QueueReplay; stage-plan ingress
and replacement workflows remain separate work. Minute evidence cannot substitute for
required queue evidence, and nothing here establishes formal Episode readiness.
"""
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from .account import Reservation, AccountUnavailable, dec, _stock_eligible, _stock_frozen
from .contracts import Contract, Hash, Name, PositiveDecimal, PositiveInt, numeric_field
from .data.contracts import MarketRule, PricePayload
from .data.temporal import Availability, Boundary, aware
from .records import ProjectionUpdate
from .repository import Conflict
from .resolution import digest


class ExecutionEvidenceMissing(AccountUnavailable):
    code = "execution_evidence_missing"


class ExchangeRejected(AccountUnavailable):
    code = "exchange_rejected"


class PriceBounds(Contract):
    lower: PositiveDecimal | None = numeric_field("CNY_per_share", null="no daily price bound")
    upper: PositiveDecimal | None = numeric_field("CNY_per_share", null="no daily price bound")

    @model_validator(mode="after")
    def ordered(self):
        if (self.lower is None) != (self.upper is None) or (self.lower is not None and self.lower > self.upper):
            raise ValueError("daily price bounds must be a complete ordered pair")
        return self

    def contains(self, price):
        return self.lower is None or self.lower <= price <= self.upper


class AcceptanceEvidence(Contract):
    security: Name
    at: datetime
    availability: Availability
    source_refs: tuple[Name, ...] = Field(min_length=1)
    source_hash: Hash
    price_bounds: PriceBounds
    buy_cage_upper: PositiveDecimal | None = numeric_field("CNY_per_share", null="no applicable buy price cage")
    sell_cage_lower: PositiveDecimal | None = numeric_field("CNY_per_share", null="no applicable sell price cage")
    sell_minimum: PositiveInt = numeric_field("share")
    sell_increment: PositiveInt = numeric_field("share")
    sell_remainder_policy: Literal["no_exception", "entire_available_remainder"]


class MinuteEvidence(Contract):
    security: Name
    market_rule: MarketRule
    bar: PricePayload
    price_bounds: PriceBounds
    availability: Availability
    source_refs: tuple[Name, ...] = Field(min_length=1)
    source_hash: Hash
    trading: Literal["active", "suspended", "partial_halt"]
    corporate_actions_complete: bool = Field(strict=True)
    queue_required_reasons: tuple[Literal["auction", "resume", "cancel_match_ordering", "limit_queue", "status_uncertain"], ...]


def _bounds_proven(bounds, rule):
    if (rule.price_limit_fraction is None) != (bounds.lower is None):
        raise ExecutionEvidenceMissing("daily price bound evidence contradicts the effective market rule")
    if bounds.lower is not None and any(price % rule.tick for price in (bounds.lower, bounds.upper)):
        raise ExecutionEvidenceMissing("price bounds are not aligned to the effective tick")


def _acceptance(order, proof, state, at, zone, *, exchange=True):
    rule = order.market_rule
    if any(old["security"] == order.security and old["side"] != order.side
           and old["status"] in {"reserved", "partial", "cancel_pending", "pending_acceptance"}
           for old in state["orders"].values()):
        raise ExchangeRejected("opposing live orders on the same security conflict")
    local = at.astimezone(zone)
    if proof.security != order.security or aware(proof.at) != at:
        raise ExecutionEvidenceMissing("acceptance evidence has a different security or effective instant")
    if not proof.availability.visible(Boundary(event_cutoff=at, snapshot_at=at, market_timezone=str(zone))):
        raise ExecutionEvidenceMissing("acceptance evidence was unavailable")
    if not rule.effective_from <= local.date() <= rule.effective_to:
        raise ExecutionEvidenceMissing("market rule does not cover acceptance")
    if exchange and not any(span.start <= local.time().replace(tzinfo=None) < span.end for span in rule.accept_orders):
        raise ExchangeRejected("market does not accept orders at this instant")
    _bounds_proven(proof.price_bounds, rule)
    if order.limit_price % rule.tick or not proof.price_bounds.contains(order.limit_price):
        raise ExchangeRejected("limit price violates tick or daily bounds")
    cage_required = rule.price_cage_fraction is not None or rule.price_cage_ticks is not None
    cage = proof.buy_cage_upper if order.side == "buy" else proof.sell_cage_lower
    if exchange and cage_required and cage is None:
        raise ExecutionEvidenceMissing("applicable price cage needs its historical reference bound")
    if exchange and not cage_required and (proof.buy_cage_upper is not None or proof.sell_cage_lower is not None):
        raise ExecutionEvidenceMissing("unexpected price cage evidence")
    if exchange and cage is not None and ((order.side == "buy" and order.limit_price > cage) or (order.side == "sell" and order.limit_price < cage)):
        raise ExchangeRejected("limit price violates the historical price cage")
    if order.quantity > rule.maximum_order_quantity or order.quantity % rule.fill_increment:
        raise ExchangeRejected("order quantity violates maximum or executable share precision")
    if order.side == "buy":
        if order.quantity < rule.buy_minimum or (order.quantity - rule.buy_minimum) % rule.buy_increment:
            raise ExchangeRejected("buy quantity violates the effective minimum/increment")
    elif order.quantity < proof.sell_minimum or order.quantity % proof.sell_increment:
        available = sum(lot["quantity"] - _stock_frozen(state, key) for key, lot in state["stock_lots"].items()
                        if lot["security"] == order.security and _stock_eligible(lot, at))
        if proof.sell_remainder_policy != "entire_available_remainder" or order.quantity != available:
            raise ExchangeRejected("sell quantity lacks a valid entire-remainder exception")


class ExecutionEngine:
    def __init__(self, account):
        self.account, self.records = account, account.records
        self.zone = ZoneInfo(account.spec.clock.timezone)

    def _state(self):
        current = self.records.projection(self.account.lease.test_id, "execution")
        return current or {"sequence": None, "value": {"orders": {}, "next_priority": 0, "liquidity": {}, "minutes": {}}}

    def _commit(self, action_id, request, current, stage, at):
        # Account remains the first projection so existing as-of NAV/receipt reads
        # replay this combined event without a second independent ledger update.
        return self.account._commit(action_id, {**request, "effects": stage.effects}, stage.value, stage.expected_sequence,
            at=at, visible_at=max(at, datetime.fromisoformat(stage.value["visible_at"])), entries=stage.entries,
            extra_updates=(ProjectionUpdate("execution", current["sequence"], current["value"]),))

    def _prior(self, action_id, request):
        row = self.records.db.execute("SELECT event_sequence FROM lagent_events WHERE test_id=? AND phase_id='account' AND action_id=? AND attempt=0",
                                      (self.account.lease.test_id, action_id)).fetchone()
        if row is None:
            return None
        event = self.records.event(row[0])
        stored = dict(event["value"]["payload"]["request"])
        stored.pop("effects", None)
        if digest(stored) != digest(request):
            raise Conflict("execution action identity has different input")
        return event

    def accept_batch(self, orders, evidence, *, at, action_id):
        """Host-proven exchange acceptance; phase-plan ingress is a separate layer."""
        at = aware(at).astimezone(self.zone)
        orders = tuple(Reservation.model_validate(order) for order in orders)
        proofs = tuple(AcceptanceEvidence.model_validate(value) for value in evidence)
        request = {"operation": "execution_accept", "orders": [order.model_dump(mode="json") for order in orders],
                   "evidence": [proof.model_dump(mode="json") for proof in proofs], "at": at.isoformat()}
        prior = self._prior(action_id, request)
        if prior is not None:
            return prior
        if len({proof.security for proof in proofs}) != len(proofs) or {proof.security for proof in proofs} != {order.security for order in orders}:
            raise ExecutionEvidenceMissing("acceptance needs unambiguous evidence for exactly the submitted securities")
        by_security = {proof.security: proof for proof in proofs}
        current, stage = self._state(), self.account.stage()
        for order in orders:
            if order.order_id in current["value"].get("reserved_order_ids", {}):
                raise Conflict("order identity is reserved by an accepted phase plan")
            _acceptance(order, by_security[order.security], stage.value, at, self.zone)
        stage.reserve_batch(orders, at=at, action_id=action_id + ":reserve")
        for order in orders:
            current["value"]["orders"][order.order_id] = {"accepted_at": at.isoformat(), "priority": current["value"]["next_priority"],
                "acceptance_source_hash": by_security[order.security].source_hash}
            current["value"]["next_priority"] += 1
        return self._commit(action_id, request, current, stage, at)

    def advance_minute(self, evidence, *, action_id):
        evidence = MinuteEvidence.model_validate(evidence)
        bar, rule = evidence.bar, evidence.market_rule
        start, end = bar.interval_start.astimezone(self.zone), bar.interval_end.astimezone(self.zone)
        request = {"operation": "execution_minute", "evidence": evidence.model_dump(mode="json")}
        prior = self._prior(action_id, request)
        if prior is not None:
            return prior
        if (end - start != timedelta(seconds=rule.minute_seconds) or start.second or start.microsecond
                or start.date() != end.date() or not rule.effective_from <= start.date() <= rule.effective_to
                or start.date() not in self.account.sessions or evidence.security[:2] != rule.exchange):
            raise ExecutionEvidenceMissing("minute interval, market identity or historical rule is invalid")
        if not any(datetime.combine(start.date(), span.start, self.zone) <= start < end <= datetime.combine(start.date(), span.end, self.zone)
                   for span in rule.continuous):
            raise ExecutionEvidenceMissing("non-continuous minute requires queue/auction execution evidence")
        boundary = Boundary(event_cutoff=end, snapshot_at=end, market_timezone=str(self.zone))
        if not evidence.availability.visible(boundary) or evidence.availability.event_at != end or not evidence.corporate_actions_complete:
            raise ExecutionEvidenceMissing("complete minute/status/corporate evidence is unavailable")
        _bounds_proven(evidence.price_bounds, rule)
        if any(price % rule.tick or not evidence.price_bounds.contains(price) for price in (bar.open, bar.high, bar.low, bar.close)):
            raise ExecutionEvidenceMissing("historical prices contradict tick or daily price bounds")
        if evidence.trading == "suspended" and bar.volume:
            raise ExecutionEvidenceMissing("suspension evidence contradicts nonzero traded volume")
        if evidence.trading == "partial_halt" or evidence.queue_required_reasons:
            raise ExecutionEvidenceMissing("scenario requires complete queue/event-order replay")
        current, stage = self._state(), self.account.stage()
        state = current["value"]
        key = evidence.security + ":" + start.isoformat()
        if key in state["minutes"]:
            raise Conflict("minute already consumed; retries must retain their original action identity")
        orders = []
        for identity, order in stage.value["orders"].items():
            if order["security"] != evidence.security or order["status"] not in {"reserved", "partial", "cancel_pending"}:
                continue
            accepted = state["orders"].get(identity)
            if accepted is None:
                raise ExecutionEvidenceMissing("live order lacks a proven exchange acceptance/priority")
            if order["trade_date"] != start.date().isoformat():
                raise ExecutionEvidenceMissing("prior DAY order has not been expired")
            if digest(order["market_rule"]) != digest(rule.model_dump(mode="json")):
                raise ExecutionEvidenceMissing("minute rule differs from the order rule")
            eligible = datetime.fromisoformat(accepted["accepted_at"]) < start
            if order["status"] == "cancel_pending":
                raise ExecutionEvidenceMissing("cancel/fill competition requires proven event order")
            if eligible and evidence.trading != "suspended" and ((order["side"] == "buy" and evidence.price_bounds.upper is not None and bar.high >= evidence.price_bounds.upper)
                    or (order["side"] == "sell" and evidence.price_bounds.lower is not None and bar.low <= evidence.price_bounds.lower)):
                raise ExecutionEvidenceMissing("price-limit scenario requires queue evidence")
            orders.append((identity, order, accepted, eligible))
        key, capacity = capacity_for(state, evidence.security, start, bar.volume, rule,
                                     self.account.spec.execution.participation_rate, evidence.source_hash)
        if state.setdefault("routes", {}).get(key, "minute") != "minute":
            raise Conflict("minute liquidity is already routed through queue replay")
        state["routes"][key] = "minute"
        results = []
        for identity, order, accepted, eligible in sorted(orders, key=lambda item: (datetime.fromisoformat(item[2]["accepted_at"]), item[2]["priority"])):
            price = bar.high + rule.tick * self.account.spec.execution.minute_slippage_ticks if order["side"] == "buy" else bar.low - rule.tick * self.account.spec.execution.minute_slippage_ticks
            remaining = order["quantity"] - order["filled_quantity"]
            quantity = min(remaining, capacity["total"] - capacity["used"])
            quantity = quantity // rule.fill_increment * rule.fill_increment
            reason = ("incomplete_submission_minute" if not eligible else "suspended" if evidence.trading == "suspended"
                      else "limit_or_market_bound" if price <= 0 or not evidence.price_bounds.contains(price)
                        or (order["side"] == "buy" and price > dec(order["limit_price"]))
                        or (order["side"] == "sell" and price < dec(order["limit_price"]))
                      else "shared_capacity_exhausted" if not quantity else None)
            if reason is None:
                stage.fill(identity, quantity=quantity, price=price, at=end, action_id=action_id + ":" + identity)
                capacity["used"] += quantity
            results.append({"order_id": identity, "status": "model_no_fill" if reason else "filled" if quantity == remaining else "partial",
                            "reason": reason, "quantity": 0 if reason else quantity, "price": None if reason else str(price),
                            "model": self.account.spec.execution.minute_model_ref, "accepted_at": accepted["accepted_at"], "priority": accepted["priority"]})
        state["minutes"][key] = {"evidence_hash": digest(evidence), "action_id": action_id, "results": results, "formal_ready": False}
        # No-fill is still a replay event and closes off backdating more orders.
        return self._commit(action_id, request, current, stage, end)


def capacity_for(state, security, start, volume, rule, rate, source_hash):
    """One historical share budget shared by every executor in this Test."""
    key = security + ":" + start.isoformat()
    total = int((Decimal(volume) * rate / rule.fill_increment).to_integral_value(rounding=ROUND_FLOOR)) * rule.fill_increment
    capacity = state["liquidity"].setdefault(key, {"historical_volume": volume, "total": total, "used": 0,
                                                 "market_rule_hash": digest(rule), "source_hash": source_hash})
    if (capacity["historical_volume"] != volume or capacity["total"] != total or capacity["market_rule_hash"] != digest(rule)
            or capacity["source_hash"] != source_hash):
        raise ExecutionEvidenceMissing("shared minute capacity has conflicting historical evidence")
    return key, capacity
