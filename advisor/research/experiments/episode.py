"""Durable fixture Episode replay, driven by the existing worker's bounded ticks.

No model, external adapter, queue, or background thread is started here. Research
is an explicit phase handoff. A prepared domain command survives the gap between
the domain transaction and the Episode cursor transaction.
"""
from datetime import date, datetime
import json
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .clock import PhaseClock, SnapshotInput
from .contracts import Contract, Hash, Name
from .corporate import CorporateActions, CorporateTerms, CorporateUnsupported
from .data.contracts import MarketRule
from .data.temporal import aware
from .execution import ExecutionEvidenceMissing, MinuteEvidence
from .orders import MarketActionEvidence, MarketCursor, StagePlans
from .queue import QueueEvidence, QueueReplay
from .records import Fenced, ProjectionUpdate
from .registration import record_identity
from .repository import Conflict
from .resolution import digest
from .settlement import SpecialSettlements, SettlementTerms, SettlementUnsupported
from .valuation import AccountValuations, RawClose, ValuationUnsupported


class ReplayPoint(Contract):
    at: datetime

    @model_validator(mode="after")
    def timestamp(self):
        aware(self.at)
        return self


class MinutePoint(ReplayPoint):
    kind: Literal["minute"]
    evidence: MinuteEvidence

    @model_validator(mode="after")
    def end(self):
        if self.at != self.evidence.bar.interval_end:
            raise ValueError("minute replay time must equal its interval end")
        return self


class QueuePoint(ReplayPoint):
    kind: Literal["queue"]
    evidence: QueueEvidence
    through_sequence: int = Field(ge=0, strict=True)


class WorkflowPoint(ReplayPoint):
    kind: Literal["workflow"]
    evidence: tuple[MarketActionEvidence, ...]

    @model_validator(mode="after")
    def proofs(self):
        if (len({item.market.security for item in self.evidence}) != len(self.evidence)
                or any(item.market.at != self.at for item in self.evidence)):
            raise ValueError("workflow proofs must be unique and at the replay time")
        return self


class DayExpiryPoint(ReplayPoint):
    kind: Literal["day_expiry"]
    day: date
    cursors: dict[Name, MarketCursor]
    market_rules: tuple[MarketRule, ...] = Field(min_length=1)


class AccountPoint(ReplayPoint):
    kind: Literal["checkpoint", "settle_payables"]


class ValuationPoint(ReplayPoint):
    kind: Literal["valuation"]
    purpose: Literal["initial", "daily", "terminal"]
    trade_date: date
    closes: tuple[RawClose, ...]
    market_rules: tuple[MarketRule, ...] = Field(min_length=1)


class CorporateRegistrationPoint(ReplayPoint):
    kind: Literal["corporate_register"]
    terms: CorporateTerms


class CorporateEffectPoint(ReplayPoint):
    kind: Literal["corporate_apply", "corporate_pay"]
    corporate_id: Name


class SettlementPoint(ReplayPoint):
    kind: Literal["special_settlement"]
    terms: SettlementTerms


class SettlementPaymentPoint(ReplayPoint):
    kind: Literal["special_settlement_pay"]
    settlement_id: Name


Point = Annotated[MinutePoint | QueuePoint | WorkflowPoint | DayExpiryPoint | AccountPoint | ValuationPoint
                  | CorporateRegistrationPoint | CorporateEffectPoint | SettlementPoint | SettlementPaymentPoint,
                  Field(discriminator="kind")]


class ReplayProgram(Contract):
    version: Literal[1]
    # Production bundle completeness and environment cost bounds are not wired.
    evidence_kind: Literal["fixture"]
    test_id: Name
    specification_hash: Hash
    snapshots: dict[Name, SnapshotInput]
    points: tuple[Point, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def ordered(self):
        if any(a.at > b.at for a, b in zip(self.points, self.points[1:])):
            raise ValueError("replay points must retain chronological source order")
        return self


class EpisodeReplay:
    def __init__(self, account, *, main_actor_id, fault=None):
        self.account, self.records, self.lease = account, account.records, account.lease
        self.clock = PhaseClock(self.records, self.lease)
        self.plans = StagePlans(account, self.clock, main_actor_id=main_actor_id)
        self.marks = AccountValuations(account)
        self.fault = fault or (lambda point: None)

    def _state(self):
        current = self.records.projection(self.lease.test_id, "episode")
        if current is None:
            raise Conflict("Episode replay has not been created")
        return current

    def _identity(self, *parts):
        return "episode:" + digest([self.lease.test_id, *parts])

    def _validate(self, program):
        if (program.test_id != self.lease.test_id or program.specification_hash != self.clock.sealed.specification_hash
                or set(program.snapshots) != {p.phase_id for p in self.clock.schedule}):
            raise Conflict("replay program must bind this Test and every exact sealed phase")
        task = self.account.task
        if program.points[0].at < task.initial_as_of or program.points[-1].at > self.clock.schedule[-1].boundary.snapshot_at:
            raise Conflict("replay program exceeds the declared Episode interval")
        marks = [p for p in program.points if p.kind == "valuation"]
        initial = [p for p in marks if p.purpose == "initial"]
        terminal = [p for p in marks if p.purpose == "terminal"]
        daily = [p.trade_date for p in marks if p.purpose == "daily"]
        expiry = [p.day for p in program.points if p.kind == "day_expiry"]
        if (len(initial) != 1 or program.points[0] != initial[0] or initial[0].at != task.initial_as_of
                or len(terminal) != 1 or terminal[0].trade_date != task.trading_dates[-1]
                or sorted(daily) != list(task.trading_dates) or sorted(expiry) != list(task.trading_dates)):
            raise Conflict("fixture Episode requires initial, every daily, terminal NAV and every DAY expiry")
        for point in marks:
            if point.purpose != "initial":
                close = max(datetime.combine(point.trade_date, rule.closing_auction.end, self.plans.zone)
                            for rule in point.market_rules)
                if not any(p.kind == "checkpoint" and p.at == close for p in program.points[:program.points.index(point)]):
                    raise Conflict("each close NAV requires an earlier explicit close checkpoint")
        self._validate_corporate_points(program)

    def _validate_corporate_points(self, program):
        """Every declared entitlement's in-period effects have one fixed slot.

        This validates the supplied lifecycle, not source coverage. A future
        payment may remain a receivable at the original Episode endpoint.
        """
        endpoint = self.clock.schedule[-1].boundary.snapshot_at
        expected, actual, registrations, settlements = {}, {}, set(), set()

        def expect(kind, identity, at):
            if at is not None and at <= endpoint:
                expected[(kind, identity)] = at

        for index, point in enumerate(program.points):
            if point.kind == "corporate_register":
                terms = point.terms
                identity = terms.action.action_id
                if identity in registrations or terms.action.kind not in {"dividend", "bonus", "split", "rights"}:
                    raise Conflict("corporate replay requires unique supported entitlement registrations")
                registrations.add(identity)
                expect(point.kind, identity, terms.record_at)
                if terms.action.kind != "rights":
                    expect("corporate_apply", identity, terms.effective_at)
                if terms.action.kind == "dividend":
                    expect("corporate_pay", identity, terms.payment_at)
                if any(p.at == point.at and p.kind in {"minute", "queue", "workflow"}
                       for p in program.points[index + 1:]):
                    raise Conflict("record-date entitlement must follow all same-time market effects")
            elif point.kind == "special_settlement":
                terms = point.terms
                identity = terms.settlement_id
                if identity in settlements:
                    raise Conflict("special settlement replay requires unique retirement identities")
                settlements.add(identity)
                expect(point.kind, identity, terms.effective_at)
                expect("special_settlement_pay", identity, terms.cash_payment_at)
            elif point.kind in {"corporate_apply", "corporate_pay"}:
                identity = point.corporate_id
                if identity not in registrations:
                    raise Conflict("corporate effect must follow its declared registration")
                if point.kind == "corporate_pay" and ("corporate_apply", identity) not in actual:
                    raise Conflict("corporate payment must follow detachment")
            elif point.kind == "special_settlement_pay":
                identity = point.settlement_id
                if identity not in settlements:
                    raise Conflict("settlement payment must follow its declared retirement")
            else:
                continue
            key = (point.kind, identity)
            if key in actual:
                raise Conflict("duplicate corporate or settlement replay effect")
            if point.at <= self.account.task.initial_as_of:
                raise Conflict("corporate replay effects must follow the sealed initial account boundary")
            if any(p.kind == "checkpoint" and p.at == point.at for p in program.points[:index]):
                raise Conflict("corporate effects must precede a same-time account checkpoint")
            actual[key] = point.at
        if actual != expected:
            raise Conflict("corporate replay must contain every in-period effect at its proven time")

    def create(self, supplied):
        program = ReplayProgram.model_validate(supplied)
        self.records._assert_lease(self.lease)
        self._validate(program)
        program_hash = digest(program)
        current = self.records.projection(self.lease.test_id, "episode")
        if current is not None:
            if current["value"]["program_hash"] != program_hash:
                raise Conflict("Episode already has a different sealed replay program")
            return current
        if self.records.status(self.lease.test_id) != "running" or self.clock._projection() is not None:
            raise Conflict("Episode creation requires a running Test before its first phase")
        account = self.records.projection(self.lease.test_id, "account")
        if account and datetime.fromisoformat(account["value"]["last_event_at"]) != self.account.task.initial_as_of:
            raise Conflict("Episode program must be sealed before financial replay")
        artifact = self.records.artifacts.put_json(program.model_dump(mode="json"))
        identity = self._identity("program")
        prepared = self.records.prepare(experiment_id=self.account.experiment_id, kind="replay_program",
            record_id=record_identity(self.account.experiment_id, "replay-program", self.lease.test_id),
            submission_identity=identity, value={"program_hash": program_hash, "artifact_hash": artifact.content_hash,
                "formal_ready": False}, links=(("test", self.lease.test_id),), artifact_hashes=(artifact.content_hash,))
        state = {"program_id": prepared["request"]["record_id"], "program_hash": program_hash,
                 "cursor": 0, "pending": None, "status": "running", "stop_reason": None, "valuations": {},
                 "formal_ready": False, "resource_accounting_complete": False}
        self.records.commit(self.lease, phase_id="episode", action_id=identity, attempt=0, kind="episode_created",
            payload={"program_hash": program_hash}, simulated_at=self.account.task.initial_as_of,
            updates=(ProjectionUpdate("episode", None, state),), guard=lambda: self.records._insert(prepared))
        return self._state()

    def _program(self, state):
        record = self.records.read(state["program_id"])
        if (record["kind"] != "replay_program" or record["experiment_id"] != self.account.experiment_id
                or record["value"]["program_hash"] != state["program_hash"]):
            raise Conflict("Episode program binding changed")
        program = ReplayProgram.model_validate(json.loads(self.records.artifacts.read_bytes(record["value"]["artifact_hash"])))
        if digest(program) != state["program_hash"]:
            raise Conflict("Episode program integrity check failed")
        self._validate(program)
        return program

    def _commit(self, current, state, label, *, at, payload=None):
        return self.records.commit(self.lease, phase_id="episode",
            action_id=self._identity(label, current["sequence"]), attempt=0, kind="episode_" + label,
            payload=payload or {}, simulated_at=at, updates=(ProjectionUpdate("episode", current["sequence"], state),))

    def request_stop(self, reason):
        if reason not in {"cancelled", "platform_failure", "required_data_unavailable"}:
            raise ValueError("unknown Episode stop reason")
        self.records._assert_lease(self.lease)
        current = self._state()
        state = current["value"]
        if state["status"] != "running" or state["stop_reason"]:
            if state["stop_reason"] != reason:
                raise Conflict("Episode already stopped or has a different stop reason")
            return current
        self._commit(current, {**state, "stop_reason": reason}, "stop_requested", at=None, payload={"reason": reason})
        return self._state()

    def close_phase(self, phase_id, reason):
        """Host outcome handoff; a late outcome cannot close the following phase."""
        self.records._assert_lease(self.lease)
        state = self.clock._state()["value"]
        if state["index"] >= len(self.clock.schedule) or self.clock.schedule[state["index"]].phase_id != phase_id:
            raise Fenced("research outcome belongs to a different phase")
        return self.clock.begin_close(reason, action_id=self._identity("close", phase_id, reason))

    def _due_before(self, at, *, inclusive=False):
        return [flow for flow in self.plans._state()["value"]["workflows"].values()
                if flow["next_at"] is not None and (datetime.fromisoformat(flow["next_at"]) <= at if inclusive
                                                   else datetime.fromisoformat(flow["next_at"]) < at)]

    def _prepare(self, current, point):
        if self._due_before(point.at, inclusive=point.kind == "checkpoint"):
            raise ExecutionEvidenceMissing("replay program omitted an earlier workflow boundary")
        command = point.model_dump(mode="json")
        if point.kind == "workflow":
            flows = [flow for flow in self.plans._state()["value"]["workflows"].values()
                     if flow["next_at"] is not None and datetime.fromisoformat(flow["next_at"]) == point.at]
            orders = self.account._state()["value"]["orders"]
            needed = {orders[flow["order_id"]]["security"] for flow in flows}
            proofs = {p.market.security: p.model_dump(mode="json") for p in point.evidence}
            if needed - proofs.keys():
                raise ExecutionEvidenceMissing("replay program lacks due workflow security evidence")
            command["evidence"] = [proofs[key] for key in sorted(needed)]
            command["skip"] = not flows
        elif point.kind == "day_expiry":
            needed = {order["security"] for order in self.account._state()["value"]["orders"].values()
                      if order["trade_date"] == point.day.isoformat()
                      and order["status"] not in {"filled", "cancel_confirmed", "day_expired", "rejected"}}
            if needed - point.cursors.keys():
                raise ExecutionEvidenceMissing("replay program lacks live security closing cursors")
            command["cursors"] = {key: point.cursors[key].model_dump(mode="json") for key in sorted(needed)}
        state = current["value"]
        pending = {"index": state["cursor"], "action_id": self._identity("effect", state["program_hash"], state["cursor"]),
                   "command": command, "command_hash": digest(command)}
        self._commit(current, {**state, "pending": pending}, "prepared", at=point.at, payload={"command_hash": digest(command)})

    def _execute(self, pending):
        command = pending["command"]
        if digest(command) != pending["command_hash"]:
            raise Conflict("prepared replay command integrity check failed")
        kind, at, action = command["kind"], datetime.fromisoformat(command["at"]), pending["action_id"]
        if kind == "minute":
            return self.plans.engine.advance_minute(command["evidence"], action_id=action)
        if kind == "queue":
            return QueueReplay(self.plans.engine).advance(command["evidence"], through_at=at,
                through_sequence=command["through_sequence"], action_id=action)
        if kind == "workflow":
            if command["skip"]:
                return {"skipped": True}
            return self.plans.advance(at=at, evidence=command["evidence"], action_id=action)
        if kind == "day_expiry":
            return self.plans.expire_day(date.fromisoformat(command["day"]), command["cursors"], at=at,
                                        market_rules=command["market_rules"], action_id=action)
        if kind == "checkpoint":
            return self.account.checkpoint(at=at, action_id=action)
        if kind == "settle_payables":
            return self.account.settle_payables(at=at, action_id=action)
        if kind == "corporate_register":
            return CorporateActions(self.account).register(command["terms"], at=at, action_id=action)
        if kind == "corporate_apply":
            return CorporateActions(self.account).apply(command["corporate_id"], at=at, action_id=action)
        if kind == "corporate_pay":
            return CorporateActions(self.account).pay(command["corporate_id"], at=at, action_id=action)
        if kind == "special_settlement":
            return SpecialSettlements(self.account).apply(command["terms"], at=at, action_id=action)
        if kind == "special_settlement_pay":
            return SpecialSettlements(self.account).pay(command["settlement_id"], at=at, action_id=action)
        if kind == "valuation":
            return self.marks.mark(purpose=command["purpose"], trade_date=date.fromisoformat(command["trade_date"]),
                snapshot_at=at, closes=command["closes"], market_rules=command["market_rules"], action_id=action)
        raise Conflict("unsupported prepared replay command")

    def _complete_pending(self, current):
        state = current["value"]
        pending = state["pending"]
        if (pending["index"] != state["cursor"]
                or pending["action_id"] != self._identity("effect", state["program_hash"], state["cursor"])):
            raise Conflict("prepared replay command belongs to another cursor")
        result = self._execute(pending)
        self.fault("after_effect")
        valuations = dict(state["valuations"])
        command = pending["command"]
        if command["kind"] == "valuation":
            valuations[command["purpose"] + ":" + command["trade_date"]] = result["record_id"]
        self._commit(current, {**state, "pending": None, "cursor": state["cursor"] + 1, "valuations": valuations},
            "replayed", at=datetime.fromisoformat(command["at"]), payload={"index": pending["index"],
                "command_hash": pending["command_hash"], "effect_event_sequence": result.get("sequence"),
                "effect_record_id": result.get("record_id"), "skipped": result.get("skipped", False)})

    def _stop(self, current, clock_state):
        state = current["value"]
        reason = state["stop_reason"] or ("required_data_unavailable" if clock_state["blocked"] else clock_state["close_reason"])
        if clock_state["status"] == "active":
            self.close_phase(self.clock.schedule[clock_state["index"]].phase_id,
                             "cancelled" if reason == "cancelled" else "platform_failure")
            return {"kind": "progress"}
        if clock_state["status"] == "closing":
            self.clock.finish_close(action_id=self._identity("finish", clock_state["index"]))
            return {"kind": "progress"}
        for name in ("candidate_processes", "model_invocations"):
            projection = self.records.projection(self.lease.test_id, name)
            if projection and any(not call["quiescent"] or call["status"] == "prepared" for call in projection["value"]["calls"].values()):
                raise Conflict("Episode stop requires process cleanup")
        status = {"cancelled": "cancelled", "required_data_unavailable": "blocked", "platform_failure": "failed"}[reason]
        self._commit(current, {**state, "status": status, "stop_reason": reason}, "stopped", at=None)
        return {"kind": status, "formal_ready": False}

    def step(self):
        """One persisted preparation, domain effect, or phase transition per tick."""
        self.records._assert_lease(self.lease)
        try:
            return self._step()
        except (ExecutionEvidenceMissing, ValuationUnsupported, CorporateUnsupported, SettlementUnsupported) as error:
            # These domain errors precede their atomic financial commit. Preserve
            # the prepared request in the audit event, then stop at this cursor.
            # Generic storage/platform errors propagate for owner reconciliation.
            current = self._state()
            state = current["value"]
            self._commit(current, {**state, "pending": None,
                "stop_reason": state["stop_reason"] or "required_data_unavailable"}, "evidence_blocked", at=None,
                payload={"cursor": state["cursor"], "pending": state["pending"],
                         "error_type": type(error).__name__, "error_hash": digest(str(error))})
            return {"kind": "progress"}

    def _step(self):
        current = self._state()
        state = current["value"]
        if state["status"] != "running":
            return {"kind": state["status"], "formal_ready": False}
        program = self._program(state)
        if self.records.projection(self.lease.test_id, "account") is None:
            self.account.initialize(action_id=self._identity("initialize"))
            return {"kind": "progress"}
        if self.clock._projection() is None:
            self.clock.create(action_id=self._identity("clock"))
            return {"kind": "progress"}
        phase_state = self.clock._state()["value"]
        if state["pending"]:
            if phase_state["status"] != "pending":
                raise Conflict("prepared replay cannot execute during research")
            self._complete_pending(current)
            return {"kind": "progress"}
        if state["stop_reason"] or phase_state["blocked"] or phase_state["stop_required"]:
            return self._stop(current, phase_state)
        index, status = phase_state["index"], phase_state["status"]
        if status == "active":
            phase = self.clock.schedule[index]
            if self.records._now() >= datetime.fromisoformat(phase_state["deadline_at"]):
                self.close_phase(phase.phase_id, "phase_timeout")
                return {"kind": "progress"}
            return {"kind": "research_required" if phase_state["worker_generation"] == self.lease.generation else "research_recovery_required",
                    "phase_id": phase.phase_id, "snapshot_id": phase_state["snapshot_id"],
                    "deadline_at": phase_state["deadline_at"]}
        if status == "closing":
            self.clock.finish_close(action_id=self._identity("finish", index))
        elif status == "closed":
            self.clock.advance(action_id=self._identity("advance", index))
        elif status == "pending":
            phase = self.clock.schedule[index]
            # Host replay may reach reception before activating research. Candidate
            # observation still uses the earlier immutable PhaseScope boundary.
            barrier = phase.plan_received_at or phase.boundary.snapshot_at
            if state["cursor"] < len(program.points) and program.points[state["cursor"]].at <= barrier:
                self._prepare(current, program.points[state["cursor"]])
            else:
                if self._due_before(barrier, inclusive=True):
                    raise ExecutionEvidenceMissing("phase barrier would skip an earlier workflow")
                self.clock.activate(program.snapshots[phase.phase_id], action_id=self._identity("activate", index))
        elif status == "finished":
            if state["cursor"] != len(program.points) or any(flow["next_at"] is not None for flow in self.plans._state()["value"]["workflows"].values()):
                raise ExecutionEvidenceMissing("Episode cannot finish with unreplayed events or workflows")
            if len(state["valuations"]) != len(self.account.task.trading_dates) + 2:
                raise Conflict("Episode valuation receipts are incomplete")
            self._commit(current, {**state, "status": "ready_for_evaluation"}, "replay_finished", at=program.points[-1].at)
            return {"kind": "ready_for_evaluation", "formal_ready": False}
        else:
            raise Conflict("unknown phase clock status")
        return {"kind": "progress"}
