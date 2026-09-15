"""Complete fixed-path price/time queue replay; historical books stay immutable.

A host can commit an explicit sequence/time prefix before its next simulated
acceptance or cancellation. Market proof is complete for the enclosing capture,
while account effects are published only through that cut. No model chooses cuts
or insertion positions; host ingress must supply source-proven ordering.
"""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import Field

from .account import dec
from .contracts import Contract, Hash, Name, NonnegativeInt, numeric_field
from .data.contracts import AuctionPayload, MarketRule, QueueEvent, QueueRange, QueueSnapshot
from .data.temporal import Availability, Boundary, aware
from .execution import ExecutionEngine, ExecutionEvidenceMissing, PriceBounds, _bounds_proven, capacity_for
from .repository import Conflict
from .resolution import digest


class QueueTick(Contract):
    at: datetime
    event: QueueEvent
    capacity_start: datetime | None


class QueuePosition(Contract):
    order_id: Name
    after_sequence: NonnegativeInt = numeric_field("historical_exchange_sequence")
    source_ref: Name


class QueueVolume(Contract):
    start: datetime
    end: datetime
    shares: NonnegativeInt = numeric_field("historical_share")
    source_hash: Hash
    source_ref: Name


class QueueEvidence(Contract):
    security: Name
    stream_id: Name
    market_rule: MarketRule
    price_bounds: PriceBounds
    capture: QueueRange
    snapshot: QueueSnapshot
    events: tuple[QueueTick, ...]
    volumes: tuple[QueueVolume, ...] = Field(min_length=1)
    positions: tuple[QueuePosition, ...]
    auction: AuctionPayload | None
    route_reason: Literal["opening_auction", "closing_auction", "resumption_auction", "limit_buy", "limit_sell", "cancel_match_ordering"]
    availability: Availability
    source_refs: tuple[Name, ...] = Field(min_length=1)
    source_hash: Hash
    corporate_actions_complete: bool = Field(strict=True)


def _rank(order, *, virtual=False, stable=0):
    price = dec(order["price"])
    return (-price if order["side"] == "buy" else price, order["priority"], 1 if virtual else 0, stable)


def _market_hash(evidence):
    value = evidence.model_dump(mode="json")
    value.pop("positions")
    return digest(value)


def _validate(evidence, zone):
    span, snapshot, rule = evidence.capture, evidence.snapshot, evidence.market_rule
    start, end = aware(span.start_at).astimezone(zone), aware(span.end_at).astimezone(zone)
    if (not span.complete or not snapshot.complete or snapshot.session != span.session or not evidence.corporate_actions_complete
            or snapshot.last_sequence != span.start_sequence - 1 or start.date() != end.date()
            or evidence.security[:2] != rule.exchange or not rule.effective_from <= start.date() <= rule.effective_to):
        raise ExecutionEvidenceMissing("complete queue snapshot, range, market and corporate coverage required")
    boundary = Boundary(event_cutoff=end, snapshot_at=end, market_timezone=str(zone))
    if not evidence.availability.visible(boundary) or evidence.availability.event_at != end:
        raise ExecutionEvidenceMissing("complete queue capture is unavailable at its stated end")
    _bounds_proven(evidence.price_bounds, rule)
    if span.session == "continuous":
        if evidence.auction is not None or evidence.route_reason not in {"limit_buy", "limit_sell", "cancel_match_ordering"}:
            raise ExecutionEvidenceMissing("continuous queue needs its fixed price-limit route")
        if not any(datetime.combine(start.date(), interval.start, zone) <= start < end <= datetime.combine(start.date(), interval.end, zone)
                   for interval in rule.continuous):
            raise ExecutionEvidenceMissing("queue range leaves the continuous session")
        bound = evidence.price_bounds.upper if evidence.route_reason == "limit_buy" else evidence.price_bounds.lower
        if bound is None and evidence.route_reason != "cancel_match_ordering":
            raise ExecutionEvidenceMissing("price-limit routing lacks a market limit")
    else:
        expected = {"open": "opening_auction", "close": "closing_auction", "resume": "resumption_auction"}[span.session]
        if evidence.route_reason != expected or evidence.auction is None or evidence.auction.session != span.session:
            raise ExecutionEvidenceMissing("auction route needs its fixed historical match result")
        if span.session in {"open", "close"}:
            interval = rule.opening_auction if span.session == "open" else rule.closing_auction
            if start != datetime.combine(start.date(), interval.start, zone) or end != datetime.combine(start.date(), interval.end, zone):
                raise ExecutionEvidenceMissing("auction capture does not span the whole matching session")
    if len({order.order_id for order in snapshot.orders}) != len(snapshot.orders) or len({order.priority for order in snapshot.orders}) != len(snapshot.orders):
        raise ExecutionEvidenceMissing("initial queue order identity/priority is ambiguous")
    if any(order.priority > snapshot.last_sequence or order.price % rule.tick or not evidence.price_bounds.contains(order.price) for order in snapshot.orders):
        raise ExecutionEvidenceMissing("snapshot priority, tick or daily bound is inconsistent")
    if [tick.event.sequence for tick in evidence.events] != list(range(span.start_sequence, span.end_sequence + 1)):
        raise ExecutionEvidenceMissing("queue event sequence has a gap or duplicate")
    volumes = {}
    for volume in evidence.volumes:
        left, right = aware(volume.start).astimezone(zone), aware(volume.end).astimezone(zone)
        if (right - left != timedelta(seconds=60) or left.second or left.microsecond or left < start or right > end
                or left in volumes or volume.source_ref not in evidence.source_refs):
            raise ExecutionEvidenceMissing("queue capacity needs complete source-bound minute windows")
        volumes[left] = volume
    previous, counts = start, {key: 0 for key in volumes}
    for tick in evidence.events:
        at, event = aware(tick.at).astimezone(zone), tick.event
        if at < previous or at > end or event.session != span.session or event.price % rule.tick or not evidence.price_bounds.contains(event.price):
            raise ExecutionEvidenceMissing("queue event time/session/price is inconsistent")
        previous = at
        if event.event_type == "match":
            key = aware(tick.capacity_start).astimezone(zone) if tick.capacity_start is not None else None
            if key not in volumes or not key <= at <= volumes[key].end:
                raise ExecutionEvidenceMissing("historical match has no proven shared-capacity window")
            counts[key] += event.quantity
            if evidence.auction is not None and (at != end or event.price != evidence.auction.price):
                raise ExecutionEvidenceMissing("auction match contradicts fixed historical price/time")
        elif tick.capacity_start is not None or event.contra_order_id is not None:
            raise ExecutionEvidenceMissing("non-match event has unexpected match/capacity fields")
    if any(counts[key] != volume.shares for key, volume in volumes.items()):
        raise ExecutionEvidenceMissing("queue matches do not reconcile to complete historical minute volume")
    if span.session == "continuous" and evidence.route_reason != "cancel_match_ordering":
        matches = [tick.event for tick in evidence.events if tick.event.event_type == "match"]
        side = "buy" if evidence.route_reason == "limit_buy" else "sell"
        if not any(event.price == bound for event in matches) and not (not matches and any(order.side == side and order.price == bound for order in snapshot.orders)):
            raise ExecutionEvidenceMissing("historical capture does not establish the pinned price-limit route")
    if evidence.auction is not None and sum(counts.values()) != evidence.auction.volume:
        raise ExecutionEvidenceMissing("auction match volume does not reconcile")
    return start, end, volumes


def _historical_event(book, seen, event):
    identity, amount = event.order_id, event.quantity
    if event.event_type == "add":
        if identity in seen:
            raise ExecutionEvidenceMissing("historical queue identity was reused")
        seen.add(identity)
        book[identity] = {"order_id": identity, "side": event.side, "price": str(event.price), "quantity": amount, "priority": event.sequence}
        return None
    old = book.get(identity)
    if old is None or old["side"] != event.side or old["quantity"] < amount:
        raise ExecutionEvidenceMissing("historical queue consumption has no adequate matching order")
    matched = None
    if event.event_type == "cancel":
        if dec(old["price"]) != event.price:
            raise ExecutionEvidenceMissing("historical cancellation price differs from its order")
    else:
        contra = book.get(event.contra_order_id)
        if contra is None or contra["side"] == event.side or contra["quantity"] < amount:
            raise ExecutionEvidenceMissing("historical match counterparty is missing or insufficient")
        matched = {old["side"]: deepcopy(old), contra["side"]: deepcopy(contra)}
        if not dec(matched["sell"]["price"]) <= event.price <= dec(matched["buy"]["price"]):
            raise ExecutionEvidenceMissing("historical match violates its participants' limit prices")
        for side, participant in matched.items():
            eligible = [order for order in book.values() if order["side"] == side and
                        (dec(order["price"]) >= event.price if side == "buy" else dec(order["price"]) <= event.price)]
            if any(_rank(order) < _rank(participant) for order in eligible):
                raise ExecutionEvidenceMissing("historical matching violates proven price/time priority")
        contra["quantity"] -= amount
        if not contra["quantity"]:
            del book[event.contra_order_id]
    old["quantity"] -= amount
    if not old["quantity"]:
        del book[identity]
    return matched


class QueueReplay:
    def __init__(self, engine):
        self.engine = engine if isinstance(engine, ExecutionEngine) else ExecutionEngine(engine)
        self.account, self.zone = self.engine.account, self.engine.zone

    def advance(self, evidence, *, action_id, through_at=None, through_sequence=None):
        evidence = QueueEvidence.model_validate(evidence)
        span, rule = evidence.capture, evidence.market_rule
        start, end, volumes = _validate(evidence, self.zone)
        at = aware(through_at).astimezone(self.zone) if through_at is not None else end
        sequence = span.end_sequence if through_sequence is None else through_sequence
        request = {"operation": "execution_queue", "evidence_artifact_hash": digest(evidence),
                   "through_at": at.isoformat(), "through_sequence": sequence}
        prior = self.engine._prior(action_id, request)
        if prior is not None:
            return prior
        if (type(sequence) is not int or not span.start_sequence - 1 <= sequence <= span.end_sequence or not start <= at <= end
                or start.date() not in self.account.sessions):
            raise ExecutionEvidenceMissing("invalid queue replay prefix")
        if any((tick.event.sequence <= sequence and tick.at > at) or (tick.event.sequence > sequence and tick.at < at) for tick in evidence.events):
            raise ExecutionEvidenceMissing("queue prefix time and exchange sequence disagree")
        if self.account.spec.execution.queue_slippage_ticks != 0:
            raise ExecutionEvidenceMissing("fixed historical queue prices require zero extra queue slippage")
        current, stage = self.engine._state(), self.account.stage()
        state, stream_key = current["value"], evidence.security + ":" + evidence.stream_id
        queues = state.setdefault("queues", {})
        old = queues.get(stream_key)
        market_hash = _market_hash(evidence)
        original = {entry.order_id: entry.model_dump(mode="json") for entry in evidence.snapshot.orders}
        if old is not None and old["market_hash"] == market_hash:
            if sequence < old["last_sequence"] or at < datetime.fromisoformat(old["processed_at"]):
                raise Conflict("queue prefix regressed")
            if sequence == old["last_sequence"] and at == datetime.fromisoformat(old["processed_at"]):
                raise Conflict("queue prefix already processed; preserve its action identity")
            last, book, seen = old["last_sequence"], deepcopy(old["book"]), set(old["seen"])
        else:
            if old is not None and (datetime.fromisoformat(old["processed_at"]) != start or old["last_sequence"] != span.start_sequence - 1
                                    or digest(old["book"]) != digest(original)):
                raise ExecutionEvidenceMissing("next queue capture lacks continuous historical book/sequence coverage")
            last, book = span.start_sequence - 1, deepcopy(original)
            seen = set(old["seen"]) if old else set(book)
        # Validate the whole proof before allocating any prefix. Bad later source
        # events cannot be hidden by asking to stop before the corruption.
        verify_book, verify_seen = deepcopy(original), set(original)
        for tick in evidence.events:
            _historical_event(verify_book, verify_seen, tick.event)
        if evidence.auction is not None:
            buys = [dec(order["price"]) for order in verify_book.values() if order["side"] == "buy"]
            sells = [dec(order["price"]) for order in verify_book.values() if order["side"] == "sell"]
            price = evidence.auction.price
            if buys and sells and ((price is None and max(buys) >= min(sells)) or
                                   (price is not None and max(buys) >= price >= min(sells))):
                raise ExecutionEvidenceMissing("auction capture leaves unconsumed executable historical orders")
        positions = {position.order_id: position for position in evidence.positions}
        if len(positions) != len(evidence.positions) or any(position.source_ref not in evidence.source_refs for position in evidence.positions):
            raise ExecutionEvidenceMissing("queue insertion ordering lacks unique source provenance")
        active = {identity: order for identity, order in stage.value["orders"].items()
                  if order["security"] == evidence.security and order["status"] in {"reserved", "partial", "cancel_pending"}}
        if set(positions) != set(active) or len({order["side"] for order in active.values()}) > 1:
            raise ExecutionEvidenceMissing("queue insertion proof must cover exactly the live, nonconflicting orders")
        if evidence.route_reason == "cancel_match_ordering" and not (any(order["security"] == evidence.security and order["status"] in {"cancel_pending", "cancel_effective"} for order in stage.value["orders"].values()) or (old is not None and old["market_hash"] == market_hash)):
            raise ExecutionEvidenceMissing("cancel/match route requires an actual pending simulated cancellation")
        metadata = {}
        for identity, order in active.items():
            accepted, position = state["orders"].get(identity), positions[identity]
            if accepted is None or order["trade_date"] != start.date().isoformat() or digest(order["market_rule"]) != digest(rule.model_dump(mode="json")):
                raise ExecutionEvidenceMissing("queue order lacks same-day acceptance and matching rules")
            if order["status"] == "cancel_pending" and (order.get("cancel_effective_at") is None or at > datetime.fromisoformat(order["cancel_effective_at"])):
                raise ExecutionEvidenceMissing("simulated cancel confirmation ordering must be supplied by the plan executor")
            accepted_at = datetime.fromisoformat(accepted["accepted_at"])
            if position.after_sequence > span.end_sequence or any(
                (tick.event.sequence <= position.after_sequence and tick.at > accepted_at) or
                (tick.event.sequence > position.after_sequence and tick.at < accepted_at) for tick in evidence.events):
                raise ExecutionEvidenceMissing("insertion position contradicts the order acceptance time")
            marker = {"after_sequence": position.after_sequence, "source_ref": position.source_ref}
            pinned = accepted.setdefault("queue_positions", {}).setdefault(stream_key, marker)
            if pinned != marker:
                raise Conflict("accepted order cannot improve or change its proven queue position")
            metadata[identity] = (accepted, position, order["filled_quantity"])
        capacities = {}
        for key, volume in volumes.items():
            if key > at:
                continue
            capacity_key, capacity = capacity_for(state, evidence.security, key, volume.shares, rule,
                                                  self.account.spec.execution.participation_rate, volume.source_hash)
            if state.setdefault("routes", {}).get(capacity_key, "queue") != "queue" or capacity_key in state["minutes"]:
                raise Conflict("historical minute already consumed by another execution route")
            state["routes"][capacity_key] = "queue"
            capacities[key] = capacity
        fills, audit = [], []
        for tick in evidence.events:
            event = tick.event
            if event.sequence <= last or event.sequence > sequence:
                continue
            matched = _historical_event(book, seen, event)
            if matched is None:
                audit.append({"sequence": event.sequence, "kind": event.event_type, "order_id": event.order_id, "quantity": event.quantity})
                continue
            capacity = capacities[tick.capacity_start.astimezone(self.zone)]
            candidates = []
            for identity, (accepted, position, _) in metadata.items():
                order = stage.value["orders"][identity]
                if order["status"] == "filled" or event.sequence <= position.after_sequence or tick.at < datetime.fromisoformat(accepted["accepted_at"]):
                    continue
                limit, side = dec(order["limit_price"]), order["side"]
                if (side == "buy" and event.price > limit) or (side == "sell" and event.price < limit):
                    continue
                virtual = {"price": str(limit), "side": side, "priority": position.after_sequence}
                rank = _rank(virtual, virtual=True, stable=accepted["priority"])
                if rank < _rank(matched[side]):
                    candidates.append((rank, identity))
            available = min(event.quantity, capacity["total"] - capacity["used"])
            for _, identity in sorted(candidates):
                order = stage.value["orders"][identity]
                quantity = min(available, order["quantity"] - order["filled_quantity"])
                quantity = quantity // rule.fill_increment * rule.fill_increment
                if not quantity:
                    continue
                stage.fill(identity, quantity=quantity, price=event.price, at=tick.at,
                           action_id="queue-fill:" + digest([action_id, event.sequence, identity]))
                available -= quantity
                capacity["used"] += quantity
                fills.append({"order_id": identity, "sequence": event.sequence, "at": tick.at.isoformat(), "price": str(event.price), "quantity": quantity})
            audit.append({"sequence": event.sequence, "kind": "match", "historical_quantity": event.quantity,
                          "eligible_order_ids": [identity for _, identity in sorted(candidates)]})
        results = []
        for identity, (_, _, before) in metadata.items():
            order = stage.value["orders"][identity]
            quantity = order["filled_quantity"] - before
            results.append({"order_id": identity, "quantity": quantity, "status": "filled" if order["status"] == "filled" else "partial" if quantity else "model_no_fill",
                            "reason": None if quantity else "proven_queue_or_capacity_not_executable", "model": self.account.spec.execution.queue_model_ref})
        queues[stream_key] = {"market_hash": market_hash, "source_hash": evidence.source_hash, "last_sequence": sequence,
                              "processed_at": at.isoformat(), "book": book, "seen": sorted(seen), "session": span.session,
                              "capture_end_sequence": span.end_sequence, "capture_end_at": end.isoformat()}
        state.setdefault("queue_results", {})[action_id] = {"fills": fills, "results": results, "historical_audit": audit,
                                                            "market_hash": market_hash, "formal_ready": False}
        # A prefix event must not inline market events beyond its cutoff. The
        # complete capture remains an immutable host evidence artifact; research
        # observations receive only cutoff-filtered execution/account results.
        self.account.records._assert_lease(self.account.lease)
        artifact = self.account.records.artifacts.put_json(evidence.model_dump(mode="json"))
        if artifact.content_hash != request["evidence_artifact_hash"]:
            raise Conflict("queue evidence canonical artifact hash mismatch")
        return self.engine._commit(action_id, request, current, stage, at)
