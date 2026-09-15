"""Phase-bound plans, fixed ingress and durable cancellation/replacement flows."""
from datetime import datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import Field

from .account import AccountUnavailable, Reservation, dec
from .clock import PhaseClock
from .contracts import Contract, Name, PositiveDecimal, PositiveInt, numeric_field
from .data.contracts import MarketRule
from .data.temporal import Boundary, aware
from .execution import AcceptanceEvidence, ExchangeRejected, ExecutionEngine, ExecutionEvidenceMissing, _acceptance, _bounds_proven
from .records import Fenced, ProjectionUpdate
from .repository import Conflict
from .resolution import digest
from .timing import submission_timing


class NewOrder(Contract):
    kind: Literal["new"]
    order: Reservation


class CancelOrder(Contract):
    kind: Literal["cancel"]
    old_order_id: Name


class ReplaceOrder(Contract):
    kind: Literal["replace"]
    old_order_id: Name
    new_order_id: Name
    target_total: PositiveInt = numeric_field("old_plus_new_filled_share")
    limit_price: PositiveDecimal = numeric_field("CNY_per_share")


Instruction = Annotated[NewOrder | CancelOrder | ReplaceOrder, Field(discriminator="kind")]


class StagePlan(Contract):
    plan_id: Name
    instructions: tuple[Instruction, ...]


class MarketCursor(Contract):
    stream_id: Name
    after_sequence: int = numeric_field("historical_exchange_sequence", ge=0, strict=True)
    source_ref: Name


class MarketActionEvidence(Contract):
    market: AcceptanceEvidence
    cursor: MarketCursor | None


def _proofs(values, expected):
    supplied = tuple(AcceptanceEvidence.model_validate(value) for value in values)
    indexed = {value.security: value for value in supplied}
    if len(indexed) != len(supplied) or set(indexed) != set(expected):
        raise ExecutionEvidenceMissing("plan needs unique market evidence for exactly its securities")
    return indexed


def _static_price(proof, rule, price, at, zone):
    if proof.at != at or not proof.availability.visible(Boundary(event_cutoff=at, snapshot_at=at, market_timezone=str(zone))):
        raise ExecutionEvidenceMissing("plan validation evidence is not available at fixed reception")
    _bounds_proven(proof.price_bounds, rule)
    if price % rule.tick or not proof.price_bounds.contains(price):
        raise ExchangeRejected("planned price violates historical tick or daily bounds")


class StagePlans:
    def __init__(self, account, clock, *, main_actor_id):
        if not isinstance(clock, PhaseClock) or clock.lease != account.lease or clock.records is not account.records:
            raise Conflict("plan clock/account ownership mismatch")
        self.account, self.clock, self.main_actor_id = account, clock, main_actor_id
        self.engine = ExecutionEngine(account)
        self.records, self.zone = account.records, ZoneInfo(account.spec.clock.timezone)

    def _state(self):
        current = self.records.projection(self.account.lease.test_id, "plans")
        return current or {"sequence": None, "value": {"phases": {}, "plans": {}, "workflows": {}, "next_priority": 0, "expired_days": []}}

    def _authorize(self, session, *, active=True):
        if (session.records is not self.records or session.scope.test_id != self.account.lease.test_id
                or session.actor_id != self.main_actor_id or session.is_child):
            raise Fenced("only the host-designated main phase session can submit plans")
        if active:
            self.clock.assert_scope(session.scope)

    def _commit(self, action_id, request, plans, execution, stage, at, *, guard=None):
        return self.account._commit(action_id, {**request, "effects": stage.effects}, stage.value, stage.expected_sequence,
            at=at, visible_at=max(at, datetime.fromisoformat(stage.value["visible_at"])), entries=stage.entries,
            extra_updates=(ProjectionUpdate("execution", execution["sequence"], execution["value"]),
                           ProjectionUpdate("plans", plans["sequence"], plans["value"])), guard=guard)

    def submit(self, session, plan, market_evidence, *, action_id):
        self._authorize(session, active=False)
        plan = StagePlan.model_validate(plan)
        phase = next((item for item in self.clock.schedule if item.phase_id == session.scope.phase_id), None)
        if phase is None or phase.plan_received_at is None:
            raise ExchangeRejected("phase does not accept trading plans")
        at = phase.plan_received_at
        proofs = tuple(AcceptanceEvidence.model_validate(value) for value in market_evidence)
        request = {"operation": "plan_submit", "phase_id": phase.phase_id, "plan": plan.model_dump(mode="json"),
                   "market_evidence": [proof.model_dump(mode="json") for proof in proofs], "actor_id": session.actor_id}
        prior = self.engine._prior(action_id, request)
        if prior is not None:
            return prior
        plans = self._state()
        accepted_plan = plans["value"]["plans"].get(plan.plan_id)
        if accepted_plan is not None:
            if accepted_plan["request_hash"] != digest(request):
                raise Conflict("plan identity has different content")
            return self.engine._prior(accepted_plan["submission_action_id"], request)
        self._authorize(session)
        execution, stage = self.engine._state(), self.account.stage()
        state = plans["value"]
        if phase.trading_date.isoformat() in state["expired_days"]:
            raise ExchangeRejected("trading day is already expired")
        if phase.phase_id in state["phases"]:
            raise Conflict("phase already accepted its one successful plan")
        if plan.plan_id in state["plans"]:
            raise Conflict("plan identity already exists")
        references, new_ids, securities, sides = set(), set(), set(), {}
        for item in plan.instructions:
            if item.kind == "new":
                identity, security, side = item.order.order_id, item.order.security, item.order.side
            else:
                if item.old_order_id in references:
                    raise ExchangeRejected("multiple cancellation/replacement instructions reference one old order")
                references.add(item.old_order_id)
                old = stage.value["orders"].get(item.old_order_id)
                if old is None or old["status"] not in {"reserved", "partial"} or old["trade_date"] != phase.trading_date.isoformat():
                    raise ExchangeRejected("referenced old order is not a live same-day order")
                security, side = old["security"], old["side"]
                identity = item.new_order_id if item.kind == "replace" else None
                if item.kind == "replace" and item.target_total < old["filled_quantity"]:
                    raise ExchangeRejected("replacement target is below already filled old quantity")
            if identity is not None:
                if identity in new_ids or identity in stage.value["orders"] or identity in execution["value"].get("reserved_order_ids", {}):
                    raise ExchangeRejected("new order identity is duplicated or already used")
                new_ids.add(identity)
            securities.add(security)
            sides.setdefault(security, set()).add(side)
        if references & new_ids or any(len(values) > 1 for values in sides.values()):
            raise ExchangeRejected("plan has dependent/self-conflicting new orders")
        by_security = _proofs(proofs, securities)
        new_orders, prepared = [], []
        for index, item in enumerate(plan.instructions):
            old = None if item.kind == "new" else stage.value["orders"][item.old_order_id]
            rule = item.order.market_rule if old is None else MarketRule.model_validate(old["market_rule"])
            security = item.order.security if old is None else old["security"]
            proof = by_security[security]
            price = item.order.limit_price if old is None else item.limit_price if item.kind == "replace" else dec(old["limit_price"])
            _static_price(proof, rule, price, at, self.zone)
            if item.kind == "new":
                _acceptance(item.order, proof, stage.value, at, self.zone, exchange=False)
                new_orders.append(item.order)
            timing = submission_timing(phase, rule, order_latency_ms=self.account.spec.execution.order_latency_ms if old is None else self.account.spec.execution.cancel_latency_ms,
                                       receipt_latency_ms=self.account.spec.execution.receipt_latency_ms, operation="submit" if old is None else "cancel")
            if timing["status"] == "blocked":
                raise ExecutionEvidenceMissing(timing["code"])
            effective = timing["exchange_received_at"]
            prepared.append({"workflow_id": plan.plan_id + ":" + str(index), "plan_id": plan.plan_id, "instruction": item.model_dump(mode="json"),
                "phase_id": phase.phase_id, "trade_date": phase.trading_date.isoformat(), "priority": state["next_priority"] + index,
                "operation": "new" if old is None else "cancel", "order_id": item.order.order_id if old is None else item.old_order_id,
                "state": "waiting_market", "next_at": effective, "timing": timing, "reason": None})
        if new_orders:
            stage.reserve_batch(new_orders, at=at, action_id=action_id + ":reserve")
            for order in new_orders:
                stage.value["orders"][order.order_id]["status"] = "pending_acceptance"
        for workflow in prepared:
            if workflow["operation"] == "cancel":
                stage.cancel_request(workflow["order_id"], at=at, action_id=workflow["workflow_id"] + ":request")
                stage.value["orders"][workflow["order_id"]]["cancel_effective_at"] = workflow["next_at"]
            state["workflows"][workflow["workflow_id"]] = workflow
        execution["value"].setdefault("reserved_order_ids", {}).update({identity: plan.plan_id for identity in new_ids})
        state["next_priority"] += len(prepared)
        state["phases"][phase.phase_id] = plan.plan_id
        state["plans"][plan.plan_id] = {"plan": plan.model_dump(mode="json"), "phase_id": phase.phase_id,
                                      "accepted_at": at.isoformat(), "status": "accepted", "formal_ready": False,
                                      "request_hash": digest(request), "submission_action_id": action_id}
        return self._commit(action_id, request, plans, execution, stage, at, guard=lambda: self._authorize(session))

    def _ordering(self, proof, rule, execution, at):
        if proof.market.at != at or not proof.market.availability.visible(Boundary(event_cutoff=at, snapshot_at=at, market_timezone=str(self.zone))):
            raise ExecutionEvidenceMissing("market action evidence is not available at its fixed time")
        local = at.astimezone(self.zone).time().replace(tzinfo=None)
        matching = any(span.start <= local <= span.end for span in (*rule.continuous, rule.opening_auction, rule.closing_auction))
        if not matching:
            return
        cursor = proof.cursor
        if cursor is None or cursor.source_ref not in proof.market.source_refs:
            raise ExecutionEvidenceMissing("market action needs proven ordering against possible matches")
        capture = execution["value"].get("queues", {}).get(proof.market.security + ":" + cursor.stream_id)
        if capture is None or capture["last_sequence"] != cursor.after_sequence or datetime.fromisoformat(capture["processed_at"]) != at:
            raise ExecutionEvidenceMissing("market replay has not reached the exact proven action cursor")

    def _order_state(self, stage, order_id, status, at, *, visible_at, reason):
        state = stage._state()
        order = state["value"]["orders"][order_id]
        order["status"] = status
        return stage._commit("state:" + digest([order_id, status, at, reason]), {"operation": "order_market_state", "order_id": order_id,
            "status": status, "reason": reason}, state["value"], state["sequence"], at=at, visible_at=visible_at, entries=[])

    def advance(self, *, at, evidence, action_id):
        at = aware(at).astimezone(self.zone)
        proofs = tuple(MarketActionEvidence.model_validate(value) for value in evidence)
        request = {"operation": "plan_advance", "at": at.isoformat(), "evidence": [value.model_dump(mode="json") for value in proofs]}
        prior = self.engine._prior(action_id, request)
        if prior is not None:
            return prior
        plans, execution, stage = self._state(), self.engine._state(), self.account.stage()
        flows = plans["value"]["workflows"].values()
        if any(flow["next_at"] is not None and datetime.fromisoformat(flow["next_at"]) < at for flow in flows):
            raise ExecutionEvidenceMissing("an earlier workflow event must be replayed first")
        due = sorted((flow for flow in flows if flow["next_at"] is not None and datetime.fromisoformat(flow["next_at"]) == at), key=lambda flow: flow["priority"])
        needed = {stage.value["orders"][flow["order_id"]]["security"] for flow in due}
        by_security = {value.market.security: value for value in proofs}
        if len(by_security) != len(proofs) or set(by_security) != needed or not due:
            raise ExecutionEvidenceMissing("workflow advancement needs exact evidence for the due securities")
        history = []
        for flow in due:
            while flow["next_at"] is not None and datetime.fromisoformat(flow["next_at"]) == at:
                order = stage.value["orders"][flow["order_id"]]
                rule = MarketRule.model_validate(order["market_rule"])
                proof = by_security[order["security"]]
                self._ordering(proof, rule, execution, at)
                receipt = at + timedelta(milliseconds=self.account.spec.execution.receipt_latency_ms)
                if flow["state"] == "waiting_market":
                    if flow["operation"] == "new":
                        try:
                            _acceptance(Reservation.model_validate({key: order[key] for key in Reservation.model_fields}), proof.market, stage.value, at, self.zone)
                        except ExchangeRejected as error:
                            self._order_state(stage, flow["order_id"], "rejected_pending_receipt", at, visible_at=receipt, reason=str(error))
                            flow.update(state="waiting_receipt", outcome="rejected", reason=str(error), next_at=receipt.isoformat())
                        else:
                            self._order_state(stage, flow["order_id"], "reserved", at, visible_at=receipt, reason="exchange_accepted")
                            execution["value"]["orders"][flow["order_id"]] = {"accepted_at": at.isoformat(), "priority": execution["value"]["next_priority"],
                                "acceptance_source_hash": proof.market.source_hash, "receipt_available_at": receipt.isoformat()}
                            execution["value"]["next_priority"] += 1
                            flow.update(state="exchange_accepted", next_at=None)
                    elif order["status"] == "filled":
                        flow.update(state="cancel_too_late", next_at=None, reason="old_order_already_filled")
                    elif flow["timing"]["status"] == "rejected":
                        self._order_state(stage, flow["order_id"], "partial" if order["filled_quantity"] else "reserved", at, visible_at=receipt,
                                          reason=flow["timing"]["code"])
                        flow.update(state="cancel_rejected", next_at=None, reason=flow["timing"]["code"])
                    else:
                        self._order_state(stage, flow["order_id"], "cancel_effective", at, visible_at=receipt, reason="cancellation_effective")
                        flow.update(state="waiting_receipt", outcome="cancel_confirmed", next_at=receipt.isoformat())
                else:
                    stage.release(flow["order_id"], at=at, action_id=flow["workflow_id"] + ":receipt", reason=flow["outcome"])
                    flow.update(state=flow["outcome"], next_at=None)
                    if flow["outcome"] == "cancel_confirmed" and flow["instruction"]["kind"] == "replace":
                        instruction = ReplaceOrder.model_validate(flow["instruction"])
                        old = stage.value["orders"][flow["order_id"]]
                        remaining = max(0, instruction.target_total - old["filled_quantity"])
                        flow["replacement_quantity"] = remaining
                        if not remaining:
                            flow["state"] = "target_satisfied"
                        else:
                            phase = next(item for item in self.clock.schedule if item.phase_id == flow["phase_id"])
                            replacement_phase = phase.model_copy(update={"plan_received_at": at})
                            timing = submission_timing(replacement_phase, rule, order_latency_ms=self.account.spec.execution.order_latency_ms,
                                receipt_latency_ms=self.account.spec.execution.receipt_latency_ms, operation="submit")
                            if at.date().isoformat() != flow["trade_date"] or timing["status"] != "accepted":
                                flow.update(state="replacement_expired", reason="no_same_day_acceptance_window")
                            else:
                                new = Reservation.model_validate({**{key: old[key] for key in Reservation.model_fields},
                                    "order_id": instruction.new_order_id, "quantity": remaining, "limit_price": instruction.limit_price})
                                try:
                                    _acceptance(new, proof.market, stage.value, at, self.zone, exchange=False)
                                    stage.reserve_batch([new], at=at, action_id=flow["workflow_id"] + ":replacement")
                                except (ExchangeRejected, AccountUnavailable) as error:
                                    if isinstance(error, ExecutionEvidenceMissing):
                                        raise
                                    flow.update(state="replacement_rejected", reason=str(error))
                                else:
                                    stage.value["orders"][new.order_id]["status"] = "pending_acceptance"
                                    flow.update(state="waiting_market", operation="new", order_id=new.order_id, timing=timing,
                                                next_at=timing["exchange_received_at"])
                history.append({"workflow_id": flow["workflow_id"], "state": flow["state"], "at": at.isoformat(),
                                "reason": flow["reason"], "replacement_quantity": flow.get("replacement_quantity")})
        plans["value"].setdefault("history", {})[action_id] = history
        return self._commit(action_id, request, plans, execution, stage, at)

    def expire_day(self, day, cursors, *, at, market_rules, action_id):
        at = aware(at).astimezone(self.zone)
        rules = tuple(MarketRule.model_validate(value) for value in market_rules)
        supplied = {security: MarketCursor.model_validate(value) for security, value in cursors.items()}
        request = {"operation": "plan_day_expiry", "day": day.isoformat(), "at": at.isoformat(),
                   "cursors": {key: value.model_dump(mode="json") for key, value in supplied.items()},
                   "market_rules": [rule.model_dump(mode="json") for rule in rules]}
        prior = self.engine._prior(action_id, request)
        if prior is not None:
            return prior
        plans, execution, stage = self._state(), self.engine._state(), self.account.stage()
        if (day not in self.account.task.trading_dates or not rules or any(not rule.effective_from <= day <= rule.effective_to for rule in rules)
                or at != max(datetime.combine(day, rule.closing_auction.end, self.zone) for rule in rules)
                         + timedelta(milliseconds=self.account.spec.execution.receipt_latency_ms)):
            raise ExecutionEvidenceMissing("DAY expiry needs the fixed effective market close and receipt boundary")
        if any(flow["trade_date"] == day.isoformat() and flow["next_at"] is not None and datetime.fromisoformat(flow["next_at"]) < at
               for flow in plans["value"]["workflows"].values()):
            raise ExecutionEvidenceMissing("earlier workflow events cannot be skipped by DAY expiry")
        if day.isoformat() in plans["value"]["expired_days"]:
            raise Conflict("DAY expiry already processed")
        live = {key: order for key, order in stage.value["orders"].items() if order["trade_date"] == day.isoformat()
                and order["status"] not in {"filled", "cancel_confirmed", "day_expired", "rejected"}}
        if set(supplied) != {order["security"] for order in live.values()}:
            raise ExecutionEvidenceMissing("DAY expiry needs a complete closing cursor for every live security")
        for order in live.values():
            rule = MarketRule.model_validate(order["market_rule"])
            if not any(digest(rule) == digest(proven) for proven in rules):
                raise ExecutionEvidenceMissing("DAY expiry rule differs from the live order rule")
            close = datetime.combine(day, rule.closing_auction.end, self.zone)
            cursor = supplied[order["security"]]
            capture = execution["value"].get("queues", {}).get(order["security"] + ":" + cursor.stream_id)
            if (at != close + timedelta(milliseconds=self.account.spec.execution.receipt_latency_ms) or capture is None
                    or capture.get("session") != "close" or capture["last_sequence"] != cursor.after_sequence
                    or capture["last_sequence"] != capture.get("capture_end_sequence")
                    or datetime.fromisoformat(capture["processed_at"]) != close):
                raise ExecutionEvidenceMissing("DAY expiry lacks complete closing-auction execution")
        for identity in live:
            stage.release(identity, at=at, action_id=action_id + ":" + identity, reason="day_expired")
        for flow in plans["value"]["workflows"].values():
            if flow["trade_date"] == day.isoformat() and flow["next_at"] is not None:
                flow.update(state="day_expired", next_at=None, reason="DAY_expiry")
        plans["value"]["expired_days"].append(day.isoformat())
        return self._commit(action_id, request, plans, execution, stage, at)
