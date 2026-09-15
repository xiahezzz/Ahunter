"""Candidate JSON tool boundary. Keep this object exclusively in the host.

The isolated phase-process adapter sends JSON frames to a dispatcher bound to one
PhaseSession. Candidate code never receives records, provider callbacks, account
objects, source file handles or this Python object. OS/process containment and
metered model/delegation execution are separate prerequisites.
"""
from typing import Annotated, Literal
import json

from pydantic import Field, TypeAdapter

from .contracts import Contract, Name, PositiveDecimal, PositiveInt, numeric_field
from .candidates import read_candidate
from .data.access import DataRequest, HistoricalQueries
from .execution import ExchangeRejected, ExecutionEvidenceMissing
from .account import AccountUnavailable
from .orders import CancelOrder, ReplaceOrder, StagePlan
from .records import Fenced, ProjectionUpdate
from .registration import record_identity
from .repository import Conflict
from .resolution import digest, encode


class TradeIntent(Contract):
    order_id: Name
    security: Name
    side: Literal["buy", "sell"]
    quantity: PositiveInt = numeric_field("share")
    limit_price: PositiveDecimal = numeric_field("CNY_per_share")


class NewIntent(Contract):
    kind: Literal["new"]
    order: TradeIntent


class PlanIntent(Contract):
    plan_id: Name
    instructions: tuple[Annotated[NewIntent | CancelOrder | ReplaceOrder, Field(discriminator="kind")], ...]


class WorkingMemory(Contract):
    summary: str
    decisions: tuple[str, ...]
    next_questions: tuple[str, ...]


class ToolFrame(Contract):
    action_id: Name
    tool: Literal["observe", "data_catalog", "query", "submit_plan", "save_memory", "load_memory"]
    arguments: dict


def _public_plan(plan):
    return {"plan_id": plan["plan_id"], "instructions": [
        {"kind": "new", "order": {key: instruction["order"][key] for key in TradeIntent.model_fields}}
        if instruction["kind"] == "new" else instruction for instruction in plan["instructions"]]}


class InvalidArguments(ValueError):
    pass


def _arguments(contract, value):
    try:
        return contract.model_validate(value)
    except (TypeError, ValueError):
        raise InvalidArguments() from None


def _error(code):
    return {"status": "error", "code": code}


class CandidateGateway:
    def __init__(self, session, clock, account, plans, *, products, plan_context, attempt_id=None):
        if (session.records is not account.records or clock.records is not account.records
                or plans.account is not account or plans.clock is not clock
                or session.scope.test_id != account.lease.test_id):
            raise Conflict("candidate gateway objects belong to different Tests")
        self.session, self.clock, self.account, self.plans = session, clock, account, plans
        self.records, self.plan_context = account.records, plan_context
        self.attempt_id = TypeAdapter(Name).validate_python(attempt_id) if attempt_id is not None else None
        self.queries = HistoricalQueries(session, tuple(products))
        candidates = [link["target_id"] for link in self.records.related(account.lease.test_id) if link["relation"] == "candidate"]
        if len(candidates) != 1:
            raise Conflict("candidate gateway needs one Test-pinned candidate")
        candidate = self.records.read(candidates[0])
        self.package_hash = candidate["value"]["proposal"]["package_hash"]
        read_candidate(self.package_hash, artifacts=self.records.artifacts)

    def child(self, actor_id):
        """Host child setup only; it starts no process and spends no model budget."""
        return CandidateGateway(self.session.child(actor_id), self.clock, self.account, self.plans,
                                products=tuple(self.queries._products.values()), plan_context=self.plan_context, attempt_id=self.attempt_id)

    def _key(self, action_id):
        identity = [self.session.scope.phase_id, self.session.actor_id, self.session.is_child, action_id]
        if self.attempt_id is not None:
            identity.append(self.attempt_id)
        return "candidate-tool:" + digest(identity)

    def _prior(self, key, request):
        row = self.records.db.execute("SELECT event_sequence FROM lagent_events WHERE test_id=? AND phase_id=? AND action_id=? AND attempt=0",
            (self.account.lease.test_id, self.session.scope.phase_id, key)).fetchone()
        if row is None:
            if request["tool"] != "query" and self._query_record(key) is not None:
                raise Conflict("candidate tool identity was used for a query")
            return None
        event = self.records.event(row[0])
        if event["value"]["payload"]["request_hash"] != digest(request):
            raise Conflict("candidate tool identity has different input")
        return event["value"]["payload"]["response"]

    def _record(self, key, request, response, *, updates=(), active=True):
        self.records.commit(self.account.lease, phase_id=self.session.scope.phase_id, action_id=key, attempt=0,
            kind="candidate_tool", payload={"actor_id": self.session.actor_id, "is_child": self.session.is_child,
                "package_hash": self.package_hash, "request_hash": digest(request), "tool": request.get("tool"), "response": response,
                **({"attempt_id": self.attempt_id} if self.attempt_id is not None else {})},
            simulated_at=self.session.scope.boundary.snapshot_at, updates=updates,
            guard=self.session.check if active else None)
        return response

    def _main(self):
        if self.session.is_child or self.session.actor_id != self.plans.main_actor_id:
            raise Fenced("candidate tool needs the designated root main")

    def call_json(self, payload):
        try:
            raw = json.loads(payload, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, TypeError):
            raw = {"invalid_frame_hash": digest(payload) if isinstance(payload, str) else digest(type(payload).__name__)}
        return json.loads(encode(self.call(raw)))

    def call(self, raw):
        """Only JSON-safe allowlisted arguments enter; no exception text leaves."""
        try:
            raw = json.loads(encode(raw))
        except (TypeError, ValueError):
            return _error("invalid_tool_request")
        try:
            frame = ToolFrame.model_validate(raw)
        except (TypeError, ValueError):
            request = {"tool": "invalid", "request_hash": digest(raw)}
            try:
                denied = self._key("invalid:" + digest(request)) if self.attempt_id is not None else "candidate-denied:" + digest(request)
                return self._record(denied, request, _error("invalid_tool_request"), active=False)
            except Fenced:
                return _error("phase_closed")
            except Exception:
                return _error("platform_failure")
        request = frame.model_dump(mode="json")
        key = self._key(frame.action_id)
        try:
            if frame.tool in {"save_memory", "submit_plan"}:
                self._main()
            else:
                self.session.check()
            prior = self._prior(key, request)
            if prior is not None:
                return prior
            if frame.tool == "submit_plan":
                return self._submit(frame, key, request)
            self.session.check()
            if frame.tool == "query":
                return self._query(frame, key)
            if frame.tool == "save_memory":
                value = _arguments(WorkingMemory, frame.arguments).model_dump(mode="json")
                current = self.records.projection(self.account.lease.test_id, "candidate_memory")
                self._check_memory(current)
                revision = current["value"]["revision"] + 1 if current else 1
                memory = {"package_hash": self.package_hash, "phase_id": self.session.scope.phase_id,
                          "revision": revision, "memory": value}
                return self._record(key, request, {"status": "committed", "revision": revision},
                    updates=(ProjectionUpdate("candidate_memory", current["sequence"] if current else None, memory),))
            if frame.arguments:
                raise InvalidArguments()
            if frame.tool == "load_memory":
                current = self.records.projection(self.account.lease.test_id, "candidate_memory")
                self._check_memory(current)
                response = {"status": "available", "revision": current["value"]["revision"] if current else 0,
                            "memory": current["value"]["memory"] if current else None}
            elif frame.tool == "data_catalog":
                response = {"status": "available", "products": self.queries.catalog()}
            else:
                observed = self.clock.observe(self.session.scope)
                phase = observed["phase"]
                account = self.account.observe(self.session.scope.boundary)
                # No source paths, execution projections, queue artifacts, hidden
                # test definitions or future scheduled action results are returned.
                response = {"status": "available", "phase": {name: phase[name] for name in ("kind", "trading_date", "boundary")},
                    "snapshot": [{"item_id": item["item_id"], "product": item["product_ref"], "value": item["value"]} for item in observed["snapshot"]["items"]],
                    "account": {name: account[name] for name in ("cash", "available_cash", "frozen_cash", "withdrawable_cash", "securities", "payables", "liabilities")}}
            return self._record(key, request, response)
        except Fenced:
            response = _error("tool_not_permitted" if self.session.is_child and frame.tool in {"submit_plan", "save_memory"} else "phase_closed")
        except Conflict:
            response = _error("conflict")
        except ExecutionEvidenceMissing:
            response = _error("execution_evidence_missing")
        except (ExchangeRejected, AccountUnavailable):
            response = _error("plan_rejected" if frame.tool == "submit_plan" else "account_unavailable")
        except InvalidArguments:
            response = _error("invalid_arguments")
        except Exception:
            response = _error("platform_failure")
        # Hash-only rejection evidence, even when a phase closes during a call.
        # A stale owner cannot write; the outer runtime must audit its late result.
        try:
            denied = "candidate-denied:" + digest([key, request, response])
            return self._record(denied, request, response, active=False)
        except Exception:
            return response

    def _check_memory(self, current):
        if current is not None and current["value"]["package_hash"] != self.package_hash:
            raise Conflict("memory belongs to a different sealed candidate")

    def _query_record(self, key):
        identity = [self.session.scope.test_id, self.session.scope.phase_id, self.session.scope.generation, self.session.actor_id, key]
        record_id = record_identity(self.session.scope.experiment_id, "query", identity)
        exists = self.records.db.execute("SELECT 1 FROM lagent_records WHERE record_id=?", (record_id,)).fetchone()
        return self.records.read(record_id) if exists else None

    def _query(self, frame, key):
        arguments = _arguments(DataRequest, frame.arguments).model_dump(mode="json")
        record = self._query_record(key)
        if record is not None:
            if record["value"]["request"] not in (arguments, {"rejected_request_hash": digest(arguments)}):
                raise Conflict("query identity has different input")
            self.session.check()
            response = self.records.artifacts.read_json(record["value"]["response_hash"])
        # HistoricalQueries owns filtered-result persistence; do not duplicate
        # provider content inside a second permanent candidate-tool event.
        else:
            response = self.queries.query(arguments, action_id=key)
        if self.attempt_id is not None:
            self._record(key + ":status", {"tool": "query", "arguments": arguments},
                {"status": response["status"], "code": response.get("code"), "response_hash": digest(response)})
        return response

    def _submit(self, frame, key, request):
        intent = _arguments(PlanIntent, frame.arguments)
        known = self.plans._state()["value"]["plans"].get(intent.plan_id)
        if known is not None:
            if known["phase_id"] != self.session.scope.phase_id or digest(_public_plan(known["plan"])) != digest(intent):
                raise Conflict("plan identity has different public intent")
            return self._record(key, request, {"status": "accepted", "plan_id": intent.plan_id}, active=False)
        self.session.check()
        phase = next(item for item in self.clock.schedule if item.phase_id == self.session.scope.phase_id)
        if phase.plan_received_at is None:
            raise ExchangeRejected("this research phase has no trading ingress")
        securities = set()
        state = self.account._state()["value"]
        for instruction in intent.instructions:
            if instruction.kind == "new":
                securities.add(instruction.order.security)
            else:
                old = state["orders"].get(instruction.old_order_id)
                if old is None:
                    raise ExchangeRejected("unknown old order")
                securities.add(old["security"])
        contexts, evidence = self.plan_context(frozenset(securities), phase)
        if set(contexts) != securities:
            raise ExecutionEvidenceMissing("host plan context is incomplete")
        instructions = []
        for instruction in intent.instructions:
            value = instruction.model_dump(mode="json")
            if instruction.kind == "new":
                context = contexts[instruction.order.security]
                value["order"].update(market_rule=context["market_rule"], fee_schedule=context["fee_schedule"])
            instructions.append(value)
        plan = StagePlan(plan_id=intent.plan_id, instructions=instructions)
        self.plans.submit(self.session, plan, evidence, action_id=key + ":plan")
        return self._record(key, request, {"status": "accepted", "plan_id": intent.plan_id}, active=False)
