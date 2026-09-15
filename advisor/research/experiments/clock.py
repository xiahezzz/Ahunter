"""Deterministic simulation phases with durable snapshots and host-only grants.

This is an Episode building block, not a runner or an execution-readiness gate.
Worker ownership and all state live in the shared experiment event store.
"""
from datetime import date, datetime, timedelta
import json
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import model_validator

from .contracts import Contract, Hash, Name, ResolvedSpecification
from .data.access import PhaseScope, PhaseSession
from .data.temporal import Availability, Boundary
from .records import Fenced, ProjectionUpdate
from .registration import record_identity
from .repository import Conflict
from .resolution import digest, verify_specification


class PhaseDefinition(Contract):
    phase_id: Name
    kind: Literal["initial_research", "auction", "postauction", "postmarket"]
    trading_date: date | None
    boundary: Boundary
    plan_received_at: datetime | None
    valuation_trade_date: date


class SnapshotItem(Contract):
    item_id: Name
    product_ref: Name
    content_hash: Hash
    availability: Availability


class SnapshotInput(Contract):
    items: tuple[SnapshotItem, ...]
    required_items: tuple[Name, ...]

    @model_validator(mode="after")
    def unique(self):
        if len({item.item_id for item in self.items}) != len(self.items) or len(set(self.required_items)) != len(self.required_items):
            raise ValueError("snapshot identities must be unique")
        return self


def phase_schedule(specification, task_id):
    sealed = ResolvedSpecification.model_validate(specification)
    spec = verify_specification(sealed)
    task = next((t for t in sealed.tasks if t.task_id == task_id), None)
    raw = next((t for t in spec.tasks if t.task_id == task_id), None)
    if task is None or raw is None or not task.trading_dates:
        raise ValueError("resolved task is missing trading dates")
    clock, zone = spec.clock, ZoneInfo(spec.clock.timezone)
    result = []
    def add(kind, day, event, snapshot, submit, *, trading):
        result.append(PhaseDefinition(
            phase_id=f"{task_id}:{day.isoformat()}:{kind}", kind=kind,
            trading_date=day if trading else None,
            boundary=Boundary(event_cutoff=datetime.combine(day, event, zone), snapshot_at=datetime.combine(day, snapshot, zone),
                              market_timezone=clock.timezone),
            plan_received_at=datetime.combine(day, submit, zone) if submit is not None else None,
            valuation_trade_date=task.trading_dates[-1]))
    add("initial_research", raw.research_start_date, clock.initial_research_at, clock.initial_research_at, None, trading=False)
    for day in task.trading_dates:
        add("auction", day, clock.auction_as_of, clock.auction_as_of, clock.auction_submit_at, trading=True)
        add("postauction", day, clock.postauction_event_cutoff, clock.postauction_as_of, clock.postauction_submit_at, trading=True)
        add("postmarket", day, clock.postmarket_as_of, clock.postmarket_as_of, None, trading=True)
    if any(left.boundary.snapshot_at >= right.boundary.event_cutoff for left, right in zip(result, result[1:])):
        raise ValueError("configured research windows overlap or precede initial research")
    return tuple(result)


class PhaseClock:
    def __init__(self, records, lease):
        self.records, self.lease = records, lease
        test = records._test(lease.test_id)
        if test["value"].get("registration_version") != 1:
            raise Conflict("phase clock requires a typed Test Record")
        definition = records.read(test["value"]["definition_id"])
        if definition["kind"] != "definition" or definition["experiment_id"] != test["experiment_id"]:
            raise Conflict("invalid test definition")
        self.experiment_id = test["experiment_id"]
        self.sealed = ResolvedSpecification.model_validate(definition["value"]["specification"])
        self.spec = verify_specification(self.sealed)
        self.schedule = phase_schedule(self.sealed, test["value"]["task_id"])

    def _projection(self):
        return self.records.projection(self.lease.test_id, "phase_clock")

    def _state(self):
        current = self._projection()
        if current is None:
            raise Conflict("phase clock has not been created")
        if current["value"]["schedule_hash"] != digest(self.schedule):
            raise Conflict("phase clock schedule differs from sealed specification")
        return current

    def _prior(self, action_id, request):
        row = self.records.db.execute("SELECT event_sequence FROM lagent_events WHERE test_id=? AND kind LIKE 'phase_%' AND action_id=? AND attempt=0",
                                      (self.lease.test_id, action_id)).fetchone()
        if row is None:
            return None
        event = self.records.event(row[0])
        if event["value"]["payload"].get("request_hash") != digest(request):
            raise Conflict("clock action identity has different input")
        return event

    def _commit(self, action_id, request, state, expected, *, kind, simulated_at):
        return self.records.commit(self.lease, phase_id=self.schedule[min(state["index"], len(self.schedule) - 1)].phase_id, action_id=action_id, attempt=0, kind=kind,
            payload={"request_hash": digest(request), "request": request}, simulated_at=simulated_at,
            updates=(ProjectionUpdate("phase_clock", expected, state),))

    def create(self, *, action_id):
        request = {"operation": "create", "specification_hash": self.sealed.specification_hash}
        previous = self._prior(action_id, request)
        if previous is not None:
            return previous
        if self._projection() is not None:
            raise Conflict("test already has a phase clock")
        if self.records.status(self.lease.test_id) != "running":
            raise Fenced("phase clock creation requires a running test")
        state = {"schedule_hash": digest(self.schedule), "index": 0, "status": "pending", "blocked": False, "stop_required": False, "research_stopped": False,
                 "snapshot_id": None, "activated_at": None, "deadline_at": None, "worker_generation": None,
                 "close_reason": None, "closed_at": None, "valuation_trade_date": self.schedule[-1].valuation_trade_date.isoformat()}
        return self._commit(action_id, request, state, None, kind="phase_clock_created", simulated_at=self.schedule[0].boundary.snapshot_at)

    def _snapshot(self, phase, supplied, expected_sequence):
        supplied = SnapshotInput.model_validate(supplied)
        visible = tuple(item for item in supplied.items if item.availability.visible(phase.boundary))
        ids, products = {item.item_id for item in visible}, {item.product_ref for item in visible}
        missing_items = sorted(set(supplied.required_items) - ids)
        missing_products = sorted(set(self.spec.data.product_refs) - products)
        value = {"boundary": phase.boundary.model_dump(mode="json"), "items": [item.model_dump(mode="json") for item in visible],
                 "missing_items": missing_items, "missing_products": missing_products,
                 "status": "blocked" if missing_items or missing_products else "frozen"}
        # Source-policy bodies must not gain permanent copies through a snapshot.
        # External evidence remains available through the retention-aware query path.
        for item in visible:
            existing = self.records.db.execute("SELECT retention_class FROM research_artifacts WHERE content_hash=?", (item.content_hash,)).fetchone()
            if existing is not None and existing[0] == "source_policy":
                raise Conflict("external source originals cannot be made permanent snapshot artifacts")
        snapshot_id = record_identity(self.experiment_id, "phase-snapshot", [self.lease.test_id, phase.phase_id])
        artifact = self.records.artifacts.put_json(value)
        prepared = self.records.prepare(experiment_id=self.experiment_id, kind="snapshot", record_id=snapshot_id,
            submission_identity=digest([self.lease.test_id, phase.phase_id]),
            value={"phase_id": phase.phase_id, "snapshot_hash": artifact.content_hash, "state": "prepared",
                   "input_hash": digest(supplied), "quality": value["status"]},
            links=(("test", self.lease.test_id),), artifact_hashes=(artifact.content_hash, *(item.content_hash for item in visible)))
        with self.records._transaction():
            self.records._assert_lease(self.lease)
            current = self._state()
            if current["sequence"] != expected_sequence or current["value"]["status"] != "pending":
                raise Fenced("snapshot preparation lost its pending phase")
            return self.records._insert(prepared)

    def activate(self, supplied, *, action_id):
        supplied = SnapshotInput.model_validate(supplied)
        request = {"operation": "activate", "snapshot_input_hash": digest(supplied)}
        previous = self._prior(action_id, request)
        if previous is not None:
            return previous
        self.records._assert_lease(self.lease)
        current = self._state()
        state = current["value"]
        if state["status"] != "pending" or state["blocked"]:
            raise Conflict("only the next pending phase can be activated")
        phase = self.schedule[state["index"]]
        snapshot = self._snapshot(phase, supplied, current["sequence"])
        now = self.records._now()
        blocked = snapshot["value"]["quality"] == "blocked"
        updated = {**state, "status": "closing" if blocked or state["research_stopped"] else "active", "blocked": blocked,
                   "snapshot_id": snapshot["record_id"], "activated_at": now.isoformat(),
                   "deadline_at": (now + timedelta(seconds=self.spec.runtime.phase_wall_timeout_seconds)).isoformat(),
                   "worker_generation": self.lease.generation,
                   "close_reason": "required_snapshot_unavailable" if blocked else "budget_exhausted" if state["research_stopped"] else None}
        return self._commit(action_id, request, updated, current["sequence"], kind="phase_quality_blocked" if blocked else "phase_research_skipped" if state["research_stopped"] else "phase_activated",
                            simulated_at=phase.boundary.snapshot_at)

    def assert_scope(self, scope):
        self.records._assert_lease(self.lease)
        current = self._state()["value"]
        if current["status"] != "active" or current["blocked"]:
            raise Fenced("phase permission is closed")
        phase = self.schedule[current["index"]]
        if (scope.experiment_id != self.experiment_id or scope.test_id != self.lease.test_id or scope.phase_id != phase.phase_id
                or scope.generation != self.lease.generation or current["worker_generation"] != self.lease.generation
                or scope.boundary != phase.boundary):
            raise Fenced("phase permission is stale or has a different observation boundary")
        if self.records._now() >= datetime.fromisoformat(current["deadline_at"]):
            raise Fenced("phase wall deadline expired")

    def session(self, actor_id):
        state = self._state()["value"]
        if state["status"] != "active":
            raise Fenced("no active phase")
        phase = self.schedule[state["index"]]
        scope = PhaseScope(self.experiment_id, self.lease.test_id, phase.phase_id, self.lease.generation, phase.boundary)
        self.assert_scope(scope)
        return PhaseSession(self.records, scope, actor_id=actor_id, assert_active=self.assert_scope)

    def observe(self, scope):
        self.assert_scope(scope)
        state = self._state()["value"]
        phase = self.schedule[state["index"]]
        snapshot = self.records.read(state["snapshot_id"])
        value = json.loads(self.records.artifacts.read_bytes(snapshot["value"]["snapshot_hash"]))
        if value["boundary"] != phase.boundary.model_dump(mode="json") or value["status"] != "frozen":
            raise Conflict("snapshot does not match the active phase")
        for item in value["items"]:
            metadata = SnapshotItem.model_validate(item)
            if not metadata.availability.visible(phase.boundary):
                raise Conflict("snapshot evidence violates its frozen boundary")
            item["value"] = json.loads(self.records.artifacts.read_bytes(metadata.content_hash))
        self.assert_scope(scope)
        return {"phase": phase.model_dump(mode="json"), "snapshot": value,
                "actual_activated_at": state["activated_at"], "actual_deadline_at": state["deadline_at"],
                "actual_elapsed_seconds": max(0, (self.records._now() - datetime.fromisoformat(state["activated_at"])).total_seconds())}

    def resume(self, *, action_id):
        request = {"operation": "resume", "worker_generation": self.lease.generation}
        previous = self._prior(action_id, request)
        if previous is not None:
            return previous
        self.records._assert_lease(self.lease)
        current = self._state()
        state = current["value"]
        if state["status"] != "active" or state["worker_generation"] == self.lease.generation:
            raise Conflict("only an active phase from an earlier worker can resume")
        if self.records._now() >= datetime.fromisoformat(state["deadline_at"]):
            raise Fenced("phase deadline cannot be extended by worker recovery")
        return self._commit(action_id, request, {**state, "worker_generation": self.lease.generation}, current["sequence"],
                            kind="phase_worker_resumed", simulated_at=self.schedule[state["index"]].boundary.snapshot_at)

    def begin_close(self, reason, *, action_id):
        if reason not in {"completed", "candidate_error", "candidate_recoveries_exhausted", "phase_timeout", "budget_exhausted", "cancelled", "platform_failure"}:
            raise ValueError("unknown phase close reason")
        request = {"operation": "begin_close", "reason": reason}
        previous = self._prior(action_id, request)
        if previous is not None:
            return previous
        current = self._state()
        state = current["value"]
        if state["status"] != "active":
            raise Conflict("only an active phase can begin closing")
        expired = self.records._now() >= datetime.fromisoformat(state["deadline_at"])
        if reason == "phase_timeout" and not expired:
            raise Conflict("phase deadline has not expired")
        return self._commit(action_id, request, {**state, "status": "closing", "close_reason": "phase_timeout" if expired and reason not in {"cancelled", "platform_failure", "budget_exhausted"} else reason,
                                                   "stop_required": reason in {"cancelled", "platform_failure"},
                                                   "research_stopped": state["research_stopped"] or reason == "budget_exhausted"},
                            current["sequence"], kind="phase_closing", simulated_at=self.schedule[state["index"]].boundary.snapshot_at)

    def finish_close(self, *, action_id):
        request = {"operation": "finish_close"}
        previous = self._prior(action_id, request)
        if previous is not None:
            return previous
        current = self._state()
        state = current["value"]
        if state["status"] != "closing":
            raise Conflict("phase must be fenced before closing completes")
        model_calls = self.records.projection(self.lease.test_id, "model_invocations")
        phase_id = self.schedule[state["index"]].phase_id
        if model_calls and any(c["phase_id"] == phase_id and (not c["quiescent"] or c["status"] == "prepared")
                               for c in model_calls["value"]["calls"].values()):
            raise Conflict("phase model processes have not finished cleanup")
        processes = self.records.projection(self.lease.test_id, "candidate_processes")
        if processes and any(c["phase_id"] == phase_id and not c["quiescent"]
                             for c in processes["value"]["calls"].values()):
            raise Conflict("phase candidate processes have not finished cleanup")
        return self._commit(action_id, request, {**state, "status": "closed", "closed_at": self.records._now().isoformat()},
                            current["sequence"], kind="phase_closed", simulated_at=self.schedule[state["index"]].boundary.snapshot_at)

    def advance(self, *, action_id):
        request = {"operation": "advance"}
        previous = self._prior(action_id, request)
        if previous is not None:
            return previous
        current = self._state()
        state = current["value"]
        if state["status"] != "closed" or state["blocked"] or state["stop_required"]:
            raise Conflict("only a closed phase without a quality block can advance")
        index = state["index"] + 1
        final = index == len(self.schedule)
        updated = {**state, "index": index, "status": "finished" if final else "pending", "snapshot_id": None,
                   "activated_at": None, "deadline_at": None, "worker_generation": None, "close_reason": None, "closed_at": None}
        return self._commit(action_id, request, updated, current["sequence"], kind="phase_clock_finished" if final else "phase_advanced",
                            simulated_at=self.schedule[-1 if final else index].boundary.snapshot_at)


    def discard_late(self, scope, *, actor_id, callback_id, output_hash):
        """Current owner audits a rejected callback without storing its output text."""
        self.records._assert_lease(self.lease)
        if scope.test_id != self.lease.test_id or scope.experiment_id != self.experiment_id:
            raise Fenced("callback belongs to another test")
        if not isinstance(output_hash, str) or len(output_hash) != 64 or any(c not in "0123456789abcdef" for c in output_hash):
            raise ValueError("callback output hash is required")
        try:
            self.assert_scope(scope)
        except Fenced:
            pass
        else:
            raise Conflict("callback still holds an active phase permission")
        action_id = "late-" + digest([scope.phase_id, scope.generation, actor_id, callback_id])
        return self.records.commit(self.lease, phase_id=scope.phase_id, action_id=action_id, attempt=0,
            kind="phase_late_result_discarded", payload={"actor_id": actor_id, "callback_id": callback_id,
                "scope_generation": scope.generation, "output_hash": output_hash, "reason": "phase_permission_expired"},
            simulated_at=scope.boundary.snapshot_at)
