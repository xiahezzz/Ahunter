"""Phase-bound real candidate tools; model/delegation orchestration remains separate."""
from dataclasses import asdict, replace
from datetime import datetime
import json
import uuid
from threading import Event, Thread

from pydantic import TypeAdapter

from .channel import ChannelLimits, ToolChannel
from .contracts import Name
from .records import Fenced, ProjectionUpdate
from .registration import record_identity
from .repository import Conflict
from .resolution import digest


class PhaseCandidateProcesses:
    def __init__(self, gateway, runner, *, compute=None):
        if runner.artifacts is not gateway.records.artifacts:
            raise Conflict("process runner and gateway use different artifact stores")
        self.gateway, self.runner = gateway, runner
        self.records, self.clock = gateway.records, gateway.clock
        self.session = gateway.session
        if compute is not None and (compute.records is not self.records or compute.budget.lease != self.clock.lease):
            raise Conflict("candidate compute uses another Test or lease")
        self.compute = compute

    def _check(self):
        self.session.check()
        if (not self.session.is_child and self.session.actor_id != self.gateway.plans.main_actor_id
                or self.clock._state()["value"]["research_stopped"]):
            raise Fenced("candidate process is not permitted to research")

    def _state(self):
        current = self.records.projection(self.session.scope.test_id, "candidate_processes")
        return current or {"sequence": None, "value": {"calls": {}}}

    def _commit(self, identity, value, *, active, current=None, evidence=None):
        current = self._state() if current is None else current
        prepared = None
        if evidence is not None:
            ref = self.records.artifacts.put_json(evidence)
            value["guardian_receipt_hash"] = ref.content_hash
            prepared = self.records.prepare(experiment_id=self.session.scope.experiment_id, kind="invocation",
                record_id=record_identity(self.session.scope.experiment_id, "candidate-process-receipt", [identity]),
                submission_identity="candidate-process-receipt:" + identity, value={"process_id": identity, **value},
                links=(("test", self.session.scope.test_id),), artifact_hashes=(ref.content_hash,))
        current["value"]["calls"][identity] = value
        def guard():
            if active:
                self._check()
            if prepared is not None:
                self.records._insert(prepared)
        if active and self.compute is not None:
            return self.compute.budget.start(value["compute"]["invocation_id"],
                action_id="candidate-compute-start:" + identity, guard=guard,
                extra_updates=(ProjectionUpdate("candidate_processes", current["sequence"], current["value"]),))
        return self.records.commit(self.clock.lease, phase_id=self.session.scope.phase_id,
            action_id="candidate-process:" + digest([identity, "start" if active else "exit"]),
            attempt=0, kind="candidate_process", payload={"process_id": identity, **value},
            simulated_at=self.session.scope.boundary.snapshot_at,
            updates=(ProjectionUpdate("candidate_processes", current["sequence"], current["value"]),),
            guard=guard)

    def run(self, process_id, *, limits, channel_limits: ChannelLimits, cancelled=lambda: False):
        """Host-thread dispatcher and independent IO/deadline monitor.

        SQLite stays on the host thread. A slow synchronous data adapter cannot stop
        the monitor from killing the candidate, but the host call itself still needs
        its adapter timeout. Unknown previous dispatches are never automatically rerun.
        """
        self._check()
        process_id = TypeAdapter(Name).validate_python(process_id)
        scope = self.session.scope
        identity = digest([scope.test_id, scope.phase_id, self.session.actor_id,
                           self.session.is_child, process_id])
        state = self._state()
        if identity in state["value"]["calls"]:
            raise Conflict("candidate process identity already dispatched; do not restart blindly")
        active = [c for c in state["value"]["calls"].values() if not c["quiescent"]]
        if any(c["actor_id"] == self.session.actor_id and c["is_child"] == self.session.is_child for c in active):
            raise Conflict("candidate actor already has an unreaped process")
        if self.session.is_child and sum(c["is_child"] for c in active) >= self.clock.spec.runtime.subagent_concurrency:
            raise Conflict("candidate child concurrency exhausted")
        deadline = datetime.fromisoformat(self.clock._state()["value"]["deadline_at"])
        remaining = (deadline - self.records._now()).total_seconds()
        if remaining <= 0:
            raise Fenced("candidate phase deadline elapsed")
        effective = replace(limits, wall_seconds=min(limits.wall_seconds, remaining),
                            poll_seconds=min(limits.poll_seconds, remaining))
        initial = json.dumps({"protocol": "ahunter-candidate-tools@1",
                              "role": "child" if self.session.is_child else "main"}).encode() + b"\n"
        if len(initial) > effective.input_bytes:
            raise ValueError("candidate input limit cannot hold protocol greeting")
        call = {"phase_id": scope.phase_id, "generation": scope.generation,
                "actor_id": self.session.actor_id, "is_child": self.session.is_child,
                "package_hash": self.gateway.package_hash, "status": "dispatch_claimed",
                "quiescent": False, "limits": asdict(effective), "channel_limits": asdict(channel_limits),
                "dispatch_claim": uuid.uuid4().hex, "formal_ready": False}
        handle = None
        if hasattr(self.runner, "prepare"):
            handle = self.runner.prepare(identity, self.gateway.package_hash, initial, limits=effective)
            call["guardian"] = handle
        if self.compute is not None:
            call["compute"] = self.compute.prepare(self.session, identity, call, guard=self._check)
        # CAS dispatch claim before starting a thread/process. If this commit is
        # uncertain, retry refuses the identity; no second physical execution.
        self._commit(identity, call, active=True, current=state)
        try:
            self._check()
        except Fenced:
            # The host has not started a thread or process; this is definite
            # non-dispatch, unlike recovery of an uncertain prior claim.
            value = {**call, "status": "not_started", "quiescent": True, "stop_reason": "phase_closed"}
            evidence = self.runner.reconcile(handle) if handle is not None else None
            if handle is not None and (evidence is None or evidence["state"] != "not_started"):
                raise Conflict("guardian non-dispatch is not proven")
            self._commit(identity, value, active=False, evidence=evidence)
            if self.compute is not None:
                value["compute_result"] = self.compute.collect(identity, self.runner)
            return value
        channel, done = ToolChannel(channel_limits), Event()
        outcome = {}
        def execute():
            try:
                outcome["result"] = self.runner.run(self.gateway.package_hash, initial,
                    limits=effective, channel=channel, **({"process_handle": handle} if handle is not None else {}))
            except BaseException as error:
                outcome["error_type"] = type(error).__name__
            finally:
                done.set()
        worker = Thread(target=execute, name="candidate-process-monitor", daemon=False)
        worker.start()
        failure = None
        try:
            while not done.is_set():
                try:
                    self._check()
                except Fenced:
                    channel.abort("phase_closed")
                if cancelled():
                    channel.abort("cancelled")
                request = channel.request()
                if request is not None and not channel.failure:
                    response = self.gateway.call(request)
                    if response.get("code") == "platform_failure":
                        # Gateway audit failure can itself prevent a tool event.
                        # Preserve the host failure in the process receipt even
                        # if the child exits before it receives our stop signal.
                        outcome["gateway_failure"] = "platform_failure"
                        channel.abort("host_failure")
                    # A query may block while the independent process monitor
                    # enforces wall time. Never publish its result after exit/fence.
                    try:
                        self._check()
                    except Fenced:
                        channel.abort("phase_closed")
                    if not done.is_set() and not channel.failure:
                        channel.answer(response)
                done.wait(effective.poll_seconds)
        except BaseException as error:
            failure = error
            channel.abort("host_failure")
        finally:
            if failure is not None:
                channel.abort("host_failure")
            worker.join()  # Real exit, never a synthetic quiescence timeout.
        result = outcome.get("result")
        if result is not None:
            value = {**call, "status": "reaped", **result.audit(),
                     **({"gateway_failure": outcome["gateway_failure"]} if outcome.get("gateway_failure") else {})}
            # A fenced former owner cannot publish cleanup; the claim remains
            # conservatively unresolved for future explicit recovery.
            evidence = self.runner.evidence(handle) if handle is not None else None
            self._commit(identity, value, active=False, evidence=evidence)
        else:
            evidence = self.runner.evidence(handle) if handle is not None else None
            if evidence is not None:
                value = {**call, "status": evidence["state"], "quiescent": True,
                         **(evidence["result"] or {"stop_reason": "guardian_prevented_launch"}),
                         "transport_code": "process_monitor_failure"}
            else:
                value = {**call, "status": "cleanup_unknown", "code": "process_monitor_failure"}
            if outcome.get("gateway_failure"):
                value["gateway_failure"] = outcome["gateway_failure"]
            self._commit(identity, value, active=False, evidence=evidence)
        if self.compute is not None:
            value["compute_result"] = self.compute.collect(identity, self.runner)
        if failure is not None:
            raise failure
        return value


class CandidateProcessRecovery:
    """Current lease owner reconciles the prior execution; no active phase grant needed."""
    def __init__(self, records, lease, runner, *, compute=None):
        if runner.artifacts is not records.artifacts:
            raise Conflict("recovery runner uses a different artifact store")
        self.records, self.lease, self.runner = records, lease, runner
        if compute is not None and (compute.records is not records or compute.budget.lease != lease):
            raise Conflict("candidate compute recovery uses another Test or lease")
        self.compute = compute

    def reconcile(self, identity):
        self.records._assert_lease(self.lease)
        current = self.records.projection(self.lease.test_id, "candidate_processes")
        if current is None or identity not in current["value"]["calls"]:
            raise LookupError("candidate execution is not registered in this Test")
        call = current["value"]["calls"][identity]
        if call["quiescent"]:
            if self.compute is not None and call.get("compute"):
                self.compute.collect(identity, self.runner)
            return call
        handle = call.get("guardian")
        if handle is None or handle["identity"] != identity:
            raise Conflict("candidate execution has no recoverable guardian identity")
        _, request = self.runner._load(handle)
        if request["package_hash"] != call["package_hash"] or request["limits"] != call["limits"]:
            raise Conflict("guardian recovery scope differs from Test claim")
        receipt = self.runner.reconcile(handle)
        if receipt is None:
            return {"process_id": identity, "status": "pending", "quiescent": False, "formal_ready": False}
        value = {**call, "status": receipt["state"], "quiescent": True,
                 **(receipt["result"] or {"stop_reason": "recovery_prevented_launch"})}
        ref = self.records.artifacts.put_json(receipt)
        value["guardian_receipt_hash"] = ref.content_hash
        current["value"]["calls"][identity] = value
        test = self.records._test(self.lease.test_id)
        prepared = self.records.prepare(experiment_id=test["experiment_id"], kind="invocation",
            record_id=record_identity(test["experiment_id"], "candidate-process-receipt", [identity]),
            submission_identity="candidate-process-receipt:" + identity,
            value={"process_id": identity, **value}, links=(("test", self.lease.test_id),), artifact_hashes=(ref.content_hash,))
        self.records.commit(self.lease, phase_id=call["phase_id"],
            action_id="candidate-process-reconcile:" + identity, attempt=0, kind="candidate_process_reconciled",
            payload={"process_id": identity, **value}, simulated_at=None,
            updates=(ProjectionUpdate("candidate_processes", current["sequence"], current["value"]),),
            guard=lambda: self.records._insert(prepared))
        if self.compute is not None and call.get("compute"):
            self.compute.collect(identity, self.runner)
        return value
