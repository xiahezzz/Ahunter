"""Metered guardian candidates at durable Episode phase handoffs.

This host adapter has no scheduler or model driver. Its owner serializes ticks
under the existing Test lease; uncertain dispatches require recovery, not retry.
"""
from dataclasses import asdict
from datetime import datetime

from .budget import CostUnavailable
from .gateway import CandidateGateway
from .guardian import GuardedCandidateRunner
from .phase_process import CandidateProcessRecovery, PhaseCandidateProcesses
from .records import ProjectionUpdate
from .repository import Conflict, Missing
from .resolution import digest


CANDIDATE_STOPS = frozenset({"exited", "wall_timeout", "scratch_limit", "output_limit", "invalid_frame",
    "frame_limit", "incomplete_frame", "request_pipelining", "response_limit", "total_response_limit", "request_limit"})
INTERRUPTED_STOPS = frozenset({"owner_disconnected", "owner_cancelled", "owner_unresponsive", "phase_closed",
    "recovery_prevented_launch", "guardian_prevented_launch"})


class EpisodeCandidates:
    def __init__(self, replay, runner, *, compute, limits, channel_limits, products, plan_context, fault=None):
        if not isinstance(runner, GuardedCandidateRunner) or runner.artifacts is not replay.records.artifacts:
            raise Conflict("Episode candidates require the recoverable guardian backend")
        if compute.records is not replay.records or compute.budget.lease != replay.lease:
            raise Conflict("Episode candidate compute belongs to another Test or lease")
        self.replay, self.runner, self.compute = replay, runner, compute
        self.records, self.lease, self.clock = replay.records, replay.lease, replay.clock
        self.limits, self.channel_limits = limits, channel_limits
        self.products, self.plan_context = tuple(products), plan_context
        self.fault = fault or (lambda point: None)

    @property
    def configuration(self):
        return {"limits": asdict(self.limits), "channel_limits": asdict(self.channel_limits),
            "guardian_limits": asdict(self.runner.guardian_limits), "main_actor_id": self.replay.plans.main_actor_id,
            "candidate_recoveries": self.clock.spec.runtime.candidate_recoveries,
            "resource": self.compute.resource, "fixture_maximum_cpu_seconds": str(self.compute.fixture_maximum),
            "price_table_hash": self.compute.budget._state()["value"]["price_table_hash"],
            "products": [{"product": p.product_ref, "version": p.version, "required": p.required} for p in self.products]}

    def _state(self):
        return self.records.projection(self.lease.test_id, "episode_research")

    def _commit(self, current, value, label, payload):
        return self.records.commit(self.lease, phase_id="episode-research",
            action_id="episode-research:" + digest([label, current["sequence"] if current else None, payload]),
            attempt=0, kind="episode_research_" + label, payload=payload, simulated_at=None,
            updates=(ProjectionUpdate("episode_research", current["sequence"] if current else None, value),))

    def _bind(self):
        current = self._state()
        if current is None:
            self._commit(None, {"configuration": self.configuration, "phases": {}, "formal_ready": False}, "bound", {})
        elif current["value"]["configuration"] != self.configuration:
            raise Conflict("Episode candidate configuration changed during recovery")

    def _calls(self):
        value = self.records.projection(self.lease.test_id, "candidate_processes")
        return value["value"]["calls"] if value else {}

    def _collect(self, identity):
        try:
            self.compute._binding(identity)
        except Missing:
            return True  # Claim-before-start ordering proves an unbound call spent no candidate CPU.
        return self.compute.collect(identity, self.runner)["status"] in {"settled", "released_unstarted", "not_reserved"}

    def _cleanup(self, phase_id):
        recovery = CandidateProcessRecovery(self.records, self.lease, self.runner, compute=self.compute)
        for identity, call in self._calls().items():
            if call["phase_id"] == phase_id:
                if not recovery.reconcile(identity)["quiescent"] or not self._collect(identity):
                    return False
        models = self.records.projection(self.lease.test_id, "model_invocations")
        if models and any(c["phase_id"] == phase_id and (not c["quiescent"] or c["status"] == "prepared")
                          for c in models["value"]["calls"].values()):
            return False
        return True

    def _prepare(self, phase_id):
        current = self._state()
        phases = current["value"]["phases"]
        phase = phases.setdefault(phase_id, {"attempts": [], "candidate_failures": 0, "outcome": None})
        number = len(phase["attempts"])
        process_id = "episode-root:" + digest([phase_id, number])
        identity = digest([self.lease.test_id, phase_id, self.replay.plans.main_actor_id, False, process_id])
        phase["attempts"].append({"number": number, "process_id": process_id, "identity": identity,
            "generation": self.lease.generation, "status": "prepared", "outcome": None})
        self._commit(current, current["value"], "prepared", {"phase_id": phase_id, "identity": identity})

    def _finish(self, phase_id, outcome):
        current = self._state()
        phase = current["value"]["phases"][phase_id]
        attempt = phase["attempts"][-1]
        attempt.update(status="finished", outcome=outcome)
        if outcome == "candidate_error":
            phase["candidate_failures"] += 1
            if phase["candidate_failures"] > self.clock.spec.runtime.candidate_recoveries:
                phase["outcome"] = "candidate_recoveries_exhausted"
        elif outcome != "interrupted":
            phase["outcome"] = outcome
        self._commit(current, current["value"], "finished", {"phase_id": phase_id,
            "identity": attempt["identity"], "outcome": outcome})

    def _tool_failure(self, attempt):
        codes = set()
        for row in self.records.db.execute("SELECT event_sequence FROM lagent_events WHERE test_id=? AND kind='candidate_tool'", (self.lease.test_id,)):
            payload = self.records.event(row[0])["value"]["payload"]
            if payload.get("attempt_id") == attempt["process_id"]:
                response = payload["response"]
                code = response.get("code")
                if code in {"product_quality_failure", "product_unavailable_at_boundary"} and response["status"] != "blocked":
                    continue
                codes.add(code)
        if "platform_failure" in codes:
            return "platform_failure"
        if codes & {"execution_evidence_missing", "account_unavailable", "product_quality_failure", "product_unavailable_at_boundary"}:
            return "required_data_unavailable"
        return None

    def _resolve(self, phase_id, attempt):
        call = self._calls().get(attempt["identity"])
        if call is None:
            if attempt["generation"] == self.lease.generation and self.clock._state()["value"]["status"] == "active":
                return {"kind": "worker_recovery_required", "formal_ready": False}
            # The superseded worker cannot commit the mandatory process claim.
            # Release a possible reservation made before that atomic start/claim.
            if not self._collect(attempt["identity"]):
                return {"kind": "cleanup_required", "formal_ready": False}
            outcome = "interrupted"
        else:
            if not call["quiescent"]:
                call = CandidateProcessRecovery(self.records, self.lease, self.runner, compute=self.compute).reconcile(attempt["identity"])
            if not call["quiescent"] or not self._collect(attempt["identity"]):
                return {"kind": "cleanup_required", "formal_ready": False}
            stop = call.get("stop_reason")
            outcome = "platform_failure" if call.get("gateway_failure") else self._tool_failure(attempt)
            if outcome is None:
                if call["status"] == "not_started" or stop in INTERRUPTED_STOPS:
                    outcome = "interrupted"
                elif stop == "cancelled":
                    outcome = "cancelled"
                elif stop not in CANDIDATE_STOPS:
                    outcome = "platform_failure"
                elif stop == "exited" and call.get("returncode") == 0:
                    outcome = "completed"
                else:
                    outcome = "candidate_error"
        self._finish(phase_id, outcome)
        return {"kind": "progress"}

    def _dispatch(self, phase_id):
        current = self._state()
        attempt = current["value"]["phases"][phase_id]["attempts"][-1]
        attempt["status"] = "dispatching"
        self._commit(current, current["value"], "dispatching", {"identity": attempt["identity"]})
        self.fault("after_dispatch_intent")
        session = self.clock.session(self.replay.plans.main_actor_id)
        gateway = CandidateGateway(session, self.clock, self.replay.account, self.replay.plans,
            products=self.products, plan_context=self.plan_context, attempt_id=attempt["process_id"])
        try:
            PhaseCandidateProcesses(gateway, self.runner, compute=self.compute).run(attempt["process_id"],
                limits=self.limits, channel_limits=self.channel_limits, cancelled=self.cancelled)
        except CostUnavailable as error:
            # No candidate starts without the atomic funded process claim.
            outcome = "budget_exhausted" if str(error) in {"insufficient_call_budget", "cost_accounting_invalid"} else "required_data_unavailable"
            self._finish(phase_id, outcome)
        self.fault("after_process")
        return {"kind": "progress"}

    def step(self, *, cancelled=lambda: False):
        self.records._assert_lease(self.lease)
        state = self.replay._state()["value"]
        if state["status"] != "running":
            return self.replay.step()
        self._bind()
        def cancellation_requested():
            if cancelled() and not self.replay._state()["value"]["stop_reason"]:
                self.replay.request_stop("cancelled")
            return bool(self.replay._state()["value"]["stop_reason"])
        self.cancelled = cancellation_requested
        cancellation_requested()
        clock = self.clock._projection()
        if clock is None:
            return self.replay.step()
        clock = clock["value"]
        if clock["index"] >= len(self.clock.schedule):
            return self.replay.step()
        phase_id = self.clock.schedule[clock["index"]].phase_id
        stopping = self.replay._state()["value"]["stop_reason"] or clock["status"] == "closing"
        expired = clock["status"] == "active" and self.records._now() >= datetime.fromisoformat(clock["deadline_at"])
        if (stopping or expired) and clock["status"] == "active":
            # Revoke phase grants before reconciling a delayed dispatch intent.
            return self.replay.step()
        if stopping or expired or clock["status"] == "active" and clock["worker_generation"] != self.lease.generation:
            if not self._cleanup(phase_id):
                return {"kind": "cleanup_required", "formal_ready": False}
        current = self._state()
        phase = current["value"]["phases"].get(phase_id)
        attempt = phase["attempts"][-1] if phase else None
        if attempt and attempt["status"] == "dispatching":
            return self._resolve(phase_id, attempt)
        if (stopping or expired) and attempt and attempt["status"] == "prepared":
            self._finish(phase_id, "interrupted")
            return {"kind": "progress"}
        if clock["status"] == "closing" and phase and phase["outcome"] is None:
            phase["outcome"] = clock["close_reason"]
            self._commit(current, current["value"], "phase_fenced", {"phase_id": phase_id, "reason": clock["close_reason"]})
            return {"kind": "progress"}
        if stopping or expired or clock["status"] != "active":
            return self.replay.step()
        if phase and phase["outcome"]:
            if phase["outcome"] in {"cancelled", "platform_failure", "required_data_unavailable"}:
                self.replay.request_stop(phase["outcome"])
            else:
                self.replay.close_phase(phase_id, phase["outcome"])
            return {"kind": "progress"}
        if clock["worker_generation"] != self.lease.generation:
            self.clock.resume(action_id="episode-candidate-resume:" + digest([phase_id, self.lease.generation]))
            return {"kind": "progress"}
        if attempt and attempt["status"] == "prepared":
            if attempt["generation"] != self.lease.generation:
                self._finish(phase_id, "interrupted")
                return {"kind": "progress"}
            return self._dispatch(phase_id)
        self._prepare(phase_id)
        return {"kind": "progress"}
