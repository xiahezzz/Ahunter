"""Source-bound candidate CPU accounting; not a complete resource capability.

Actual wait4 usage is measured. This local backend cannot prove a physical cost
upper bound, so launch authorization is available only with explicit fixture
limits AND a fixture price table. No production capability is invented here.
"""
from decimal import Decimal

from pydantic import TypeAdapter

from .budget import CostUnavailable, PriceTable, _sum
from .contracts import PositiveDecimal
from .costs import TerminalCosts, effective_budget
from .records import TERMINAL
from .registration import record_identity
from .repository import Conflict
from .resolution import digest


CPU_SEMANTICS = {
    "version": 1, "scope": "candidate_process_cpu", "source": "guardian_wait4",
    "meters": {"seconds": {"unit": "cpu_second", "sum": ["user_cpu_seconds", "system_cpu_seconds"]}},
    "excluded": ["worker_cpu", "guardian_cpu", "memory", "io", "environment_replay", "evaluation"],
}


class CandidateComputeCosts:
    def __init__(self, budget, *, resource, fixture_maximum_cpu_seconds=None):
        self.budget, self.records, self.resource = budget, budget.records, resource
        self.fixture_maximum = (TypeAdapter(PositiveDecimal).validate_python(fixture_maximum_cpu_seconds)
                                if fixture_maximum_cpu_seconds is not None else None)
        state = effective_budget(self.records, budget.lease.test_id)["value"]
        table = PriceTable.model_validate(state["table"])
        tariff = budget._tariff(table, resource)
        if (tariff.kind != "compute" or [(m.meter_id, m.unit) for m in tariff.meters] != [("seconds", "cpu_second")]
                or self.records.artifacts.read_json(tariff.usage_semantics_hash) != CPU_SEMANTICS):
            raise CostUnavailable("candidate_compute_semantics_missing")
        self.tariff = tariff

    def _id(self, kind, identity):
        return record_identity(self.budget.experiment_id, kind, [self.budget.lease.test_id, identity])

    def _binding(self, identity):
        record = self.records.read(self._id("candidate-compute-binding", identity))
        value = record["value"]
        if (record["experiment_id"] != self.budget.experiment_id or value["test_id"] != self.budget.lease.test_id
                or value["process_id"] != identity or value["resource"] != self.resource
                or {"relation": "test", "target_id": self.budget.lease.test_id} not in self.records.related(record["record_id"])):
            raise Conflict("candidate compute binding scope mismatch")
        return record

    def prepare(self, session, identity, call, *, guard):
        if session.records is not self.records or session.scope.test_id != self.budget.lease.test_id:
            raise Conflict("candidate compute belongs to another Test")
        state = self.budget._state()["value"]
        if self.fixture_maximum is None or state["table"]["evidence_kind"] != "fixture":
            raise CostUnavailable("compute_cost_capability_missing")
        handle = call.get("guardian")
        if handle is None or handle["identity"] != identity:
            raise CostUnavailable("candidate_compute_requires_guardian")
        invocation_id = "candidate-compute:" + identity
        bound = {"invocation_id": invocation_id, "actor_id": session.actor_id, "phase_id": session.scope.phase_id,
                 "bucket": "research", "resource": self.resource, "adapter_ref": "guardian-candidate-cpu@1",
                 "executor_ref": "candidate-guardian@1", "price_table_hash": state["price_table_hash"],
                 "usage_semantics_hash": self.tariff.usage_semantics_hash,
                 "maximum_units": {"seconds": str(self.fixture_maximum)}}
        ref = self.records.artifacts.put_json(bound)
        bound["proof_hash"] = ref.content_hash
        value = {"compute_record_type": "candidate_compute_binding", "process_id": identity,
                 "test_id": session.scope.test_id, "phase_id": session.scope.phase_id,
                 "actor_id": session.actor_id, "is_child": session.is_child, "package_hash": call["package_hash"],
                 "guardian": handle, "process_limits": call["limits"], "resource": self.resource,
                 "bound": bound, "fixture_bound": True, "resource_scope_complete": False, "formal_ready": False}
        prepared = self.records.prepare(experiment_id=self.budget.experiment_id, kind="invocation",
            record_id=self._id("candidate-compute-binding", identity), submission_identity="candidate-compute-binding:" + identity,
            value=value, links=(("test", session.scope.test_id),), artifact_hashes=(ref.content_hash, self.tariff.usage_semantics_hash))
        def reserve_guard():
            guard()
            self.records._insert(prepared)
        event = self.budget.reserve(bound, action_id="candidate-compute-reserve:" + identity, guard=reserve_guard)
        response = event["value"]["payload"]["response"]
        if response["status"] != "reserved":
            raise CostUnavailable(response["code"])
        return {"invocation_id": invocation_id, "binding_id": prepared["request"]["record_id"],
                "fixture_bound": True, "resource_scope_complete": False}

    def collect(self, identity, runner):
        """Settle from original guardian evidence, including after Test termination.

        Reconciliation cancels the original guardian; it never dispatches. A bound
        reservation without a process claim can therefore be recovered safely.
        """
        terminal = self.records.status(self.budget.lease.test_id) in TERMINAL
        if not terminal:
            self.records._assert_lease(self.budget.lease)
        binding = self._binding(identity)
        value = binding["value"]
        if runner.artifacts is not self.records.artifacts:
            raise Conflict("candidate compute runner uses another artifact store")
        _, request = runner._load(value["guardian"])
        if request["package_hash"] != value["package_hash"] or request["limits"] != value["process_limits"]:
            raise Conflict("guardian resource evidence differs from compute binding")
        state = effective_budget(self.records, self.budget.lease.test_id)
        invocation_id = value["bound"]["invocation_id"]
        invocation = state["value"]["invocations"].get(invocation_id)
        if invocation is None:
            return {"status": "not_reserved", "formal_ready": False}
        if invocation["bound"] != value["bound"]:
            raise Conflict("candidate compute invocation binding changed")
        measurement_id = self._id("candidate-compute-measurement", identity)
        if invocation["status"] in {"settled", "released_unstarted"}:
            measurement = self.records.read(measurement_id)
            if measurement["value"]["invocation_id"] != invocation_id:
                raise Conflict("candidate compute settlement lacks its measurement")
            if invocation["status"] == "settled" and measurement["value"]["receipt"] != invocation["receipt"]:
                raise Conflict("candidate compute measurement differs from settled usage")
            source = self.records.artifacts.read_json(measurement["value"]["guardian_receipt_hash"])
            if source["request_hash"] != value["guardian"]["request_hash"]:
                raise Conflict("candidate compute measurement has a different source")
            return {"status": invocation["status"], "invocation_id": invocation_id,
                    "cost_usd": invocation["cost_usd"], "resource_scope_complete": False, "formal_ready": False}
        source = runner.reconcile(value["guardian"])
        if source is None:
            if not terminal and invocation["status"] == "started":
                self.budget.mark_unknown(invocation_id, action_id="candidate-compute-unknown:" + identity)
            return {"status": "unsettled", "invocation_id": invocation_id,
                    "resource_scope_complete": False, "formal_ready": False}
        # Only candidate CPU is priced here. Proven non-dispatch means zero in
        # this scope, never zero guardian/worker overhead or a zero supplier bill.
        result = source["result"]
        seconds = (_sum([Decimal(str(result["user_cpu_seconds"])), Decimal(str(result["system_cpu_seconds"]))])
                   if result is not None else Decimal(0))
        outcome = ("completed" if result and result["returncode"] == 0 and result["stop_reason"] == "exited"
                   else "cancelled" if result is None or result["stop_reason"] in
                   {"cancelled", "phase_closed", "owner_disconnected", "owner_cancelled", "owner_unresponsive"}
                   else "failed")
        claim = {key: value["bound"][key] for key in
                 ("resource", "adapter_ref", "executor_ref", "price_table_hash", "usage_semantics_hash")}
        claim.update(units={"seconds": str(seconds)}, outcome=outcome, supplier_bill_usd=None)
        source_ref = self.records.artifacts.put_json(source)
        receipt_ref = self.records.artifacts.put_json({"invocation_id": invocation_id, **claim})
        receipt = {**claim, "evidence_hash": receipt_ref.content_hash}
        measurement = self.records.prepare(experiment_id=self.budget.experiment_id, kind="invocation",
            record_id=measurement_id, submission_identity="candidate-compute-measurement:" + identity,
            value={"compute_record_type": "candidate_compute_measurement", "invocation_id": invocation_id,
                   "guardian_receipt_hash": source_ref.content_hash, "receipt": receipt,
                   "resource_scope_complete": False, "formal_ready": False},
            links=(("test", self.budget.lease.test_id), ("source", binding["record_id"])),
            artifact_hashes=(source_ref.content_hash, receipt_ref.content_hash))
        guard = lambda: self.records._insert(measurement)
        if invocation["status"] == "reserved":
            if source["state"] != "not_started":
                raise Conflict("candidate ran without a started compute authorization")
            if terminal:
                TerminalCosts(self.records).release_unstarted(self.budget.lease.test_id, invocation_id, guard=guard)
            else:
                self.budget.release_unstarted(invocation_id, action_id="candidate-compute-release:" + identity, guard=guard)
        elif terminal:
            TerminalCosts(self.records).settle(self.budget.lease.test_id, invocation_id, receipt, guard=guard)
        else:
            self.budget.settle(invocation_id, receipt, action_id="candidate-compute-settle:" + identity, guard=guard)
        final = effective_budget(self.records, self.budget.lease.test_id)["value"]["invocations"][invocation_id]
        return {"status": final["status"], "invocation_id": invocation_id, "cost_usd": final["cost_usd"],
                "resource_scope_complete": False, "formal_ready": False}
