"""Reuse one predeclared baseline cohort without new Tests, costs or executions."""
from .contracts import TestPlan
from .evaluate import _runtime_conditions, assessment_matches_conditions, compare_returns
from .registration import ExperimentRegistry, record_identity
from .repository import Conflict
from .resolution import digest
from .selection import current_selection, require_selection_open


def _repeat_key(sample):
    return sample["task_id"], sample["repeat_index"], sample["repeat_id"], sample["seed"]


def _source_cohort(records, source, *, experiment_id, definition, requested, baseline, purpose, conditions, package_hash):
    """Return all eligible repeats from one original plan, never mix individual winners."""
    value = source["value"]
    if (source["kind"] != "comparison" or source["experiment_id"] != experiment_id
            or value.get("comparison_version") != 1 or value.get("record_type") != "plan"
            or value.get("selection_id") is None or value.get("runtime_conditions") is None
            or value.get("packages", {}).get(baseline) != package_hash):
        return None
    plan = records.read(value["plan_id"])
    if (plan["kind"] != "test_plan" or plan["experiment_id"] != experiment_id
            or plan["value"].get("registration_version") != 1 or plan["content_hash"] != value["plan_hash"]):
        raise Conflict("baseline source plan binding mismatch")
    if (plan["value"]["purpose"] != purpose or plan["value"]["plan"]["seed_support"] != requested["seed_support"]
            or value["specification_hash"] != definition["value"]["specification"]["specification_hash"]):
        return None
    source_definition = records.read(plan["value"]["definition_id"])
    if value["runtime_conditions"]["tasks"] != conditions["tasks"]:
        return None
    runtime, _ = _runtime_conditions(records, source_definition, plan["value"]["plan"], value["runtime_conditions"]["tasks"])
    if (runtime != value["runtime_conditions"]
            or digest({"runtime": runtime, "packages": value["packages"]}) != value["runtime_fingerprint"]):
        raise Conflict("baseline source runtime fingerprint mismatch")
    if runtime["specification_hash"] != conditions["specification_hash"]:
        return None
    wanted = {_repeat_key(s) for s in requested["tests"] if s["proposal_id"] == baseline}
    samples = [s for s in plan["value"]["plan"]["tests"] if s["proposal_id"] == baseline]
    if len(samples) != len(wanted) or {_repeat_key(s) for s in samples} != wanted:
        return None
    proofs = {}
    for sample in samples:
        test_id = record_identity(experiment_id, "test", sample["test_id"])
        test = records._test(test_id)
        # An original candidate cohort can become a future baseline. Existing
        # reuse aliases cannot form a chain; point directly to the original plan.
        if (test["value"].get("plan_id") != plan["record_id"] or test["value"].get("sample") != sample
                or test["value"].get("definition_id") != plan["value"]["definition_id"]
                or test["value"].get("rerun_of") or records.status(test_id) != "completed"):
            return None
        first = records.db.execute("SELECT record_id FROM lagent_records WHERE kind='evaluation' "
            "AND json_extract(value_json,'$.evaluation_version')=1 AND json_extract(value_json,'$.request.test_id')=? "
            "ORDER BY record_sequence LIMIT 1", (test_id,)).fetchone()
        if first is None:
            return None
        assessment = records.read(first[0])
        if (assessment["experiment_id"] != experiment_id
                or {"relation": "test", "target_id": test_id} not in records.related(first[0])):
            raise Conflict("baseline source assessment scope mismatch")
        if not assessment_matches_conditions(assessment, sample, runtime["specification_hash"],
                                              runtime["tasks"][sample["task_id"]], package_hash):
            return None
        proofs[test_id] = {"source_plan_id": plan["record_id"], "source_plan_hash": plan["content_hash"],
            "source_comparison_id": source["record_id"], "source_comparison_hash": source["content_hash"],
            "test_hash": test["content_hash"], "assessment_id": assessment["record_id"], "assessment_hash": assessment["content_hash"]}
    return {"samples": samples, "proofs": proofs}


def validate_reused_plan(records, plan, selection_id, runtime):
    """Validate the pinned cohort, not a newly selected candidate on every read."""
    reuse = plan["value"].get("reuse")
    if reuse is None:
        return
    request = reuse["request"]
    if selection_id != request["expected_selection_id"] or runtime is None or runtime["tasks"] != request["runtime_conditions"]:
        raise Conflict("reused baseline requires its original selection and exact runtime conditions")
    selected = records.read(selection_id)
    baseline_record = records.read(selected["value"]["current_baseline_id"])
    baseline = baseline_record["value"]["proposal"]["proposal_id"]
    definition = records.read(plan["value"]["definition_id"])
    source = records.read(reuse["source_comparison_id"])
    cohort = _source_cohort(records, source, experiment_id=plan["experiment_id"], definition=definition,
        requested=plan["value"]["plan"], baseline=baseline, purpose=plan["value"]["purpose"], conditions=runtime,
        package_hash=selected["value"]["current_package_hash"])
    if cohort is None or cohort["proofs"] != reuse["sources"]:
        raise Conflict("pinned baseline cohort no longer satisfies original reuse qualifications")
    actual = [s for s in plan["value"]["plan"]["tests"] if s["proposal_id"] == baseline]
    if sorted(actual, key=_repeat_key) != sorted(cohort["samples"], key=_repeat_key):
        raise Conflict("reuse must retain every original baseline sample identity")


class BaselineReuse:
    def __init__(self, records):
        self.records = records
        self.registry = ExperimentRegistry(records)

    def register(self, experiment_id, definition_id, plan, *, expected_selection_id,
                 runtime_conditions, submission_identity, purpose):
        """Resolve baseline slots to the earliest eligible complete cohort, atomically.

        Input test IDs for baseline slots are placeholders only. The returned
        sealed plan is authoritative and retains the original baseline Test IDs.
        Repeat IDs, seeds, task/proposal IDs and ordering may not be rewritten.
        """
        if purpose not in {"tuning", "selection_validation"}:
            raise Conflict("baseline reuse cannot supply calibration or final holdout samples")
        if not isinstance(submission_identity, str) or not submission_identity.strip():
            raise ValueError("reuse submission identity required")
        records = self.records
        template = TestPlan.model_validate(plan).model_dump(mode="json")
        definition = self.registry._record(experiment_id, definition_id, "definition")
        runtime, artifacts = _runtime_conditions(records, definition, template, runtime_conditions)
        request = {"definition_id": definition_id, "input_plan": template, "expected_selection_id": expected_selection_id,
                   "runtime_conditions": runtime["tasks"], "submission_identity": submission_identity, "purpose": purpose}
        plan_id = record_identity(experiment_id, "plan", template["plan_id"])
        with records._transaction():
            if records.db.execute("SELECT 1 FROM lagent_records WHERE record_id=?", (plan_id,)).fetchone():
                existing = records.read(plan_id)
                if existing["value"].get("reuse", {}).get("request") != request:
                    raise Conflict("reuse plan identity already has different inputs")
                return self._result(existing)
            current = current_selection(records, experiment_id)
            if (current is None or current["record_id"] != expected_selection_id
                    or current["value"]["definition_id"] != definition_id):
                raise Conflict("baseline reuse requires the expected current selection and definition")
            require_selection_open(records, experiment_id)
            baseline = records.read(current["value"]["current_baseline_id"])["value"]["proposal"]["proposal_id"]
            others = {s["proposal_id"] for s in template["tests"]} - {baseline}
            if len(others) != 1:
                raise Conflict("reuse plan requires the current baseline and one candidate")
            compare_returns(definition["value"]["specification"], template, {}, baseline=baseline, candidate=next(iter(others)))
            cohort = None
            # Ordering depends solely on immutable registration sequence, never NAV.
            for (identity,) in records.db.execute("SELECT record_id FROM lagent_records WHERE experiment_id=? AND kind='comparison' "
                    "AND json_extract(value_json,'$.comparison_version')=1 AND json_extract(value_json,'$.record_type')='plan' "
                    "ORDER BY record_sequence", (experiment_id,)).fetchall():
                source = records.read(identity)
                cohort = _source_cohort(records, source, experiment_id=experiment_id, definition=definition,
                    requested=template, baseline=baseline, purpose=purpose, conditions=runtime,
                    package_hash=current["value"]["current_package_hash"])
                if cohort is not None:
                    break
            if cohort is None:
                raise Conflict("no complete qualified baseline cohort matches the predeclared repeat design")
            by_repeat = {_repeat_key(s): s for s in cohort["samples"]}
            resolved = {**template, "tests": [by_repeat[_repeat_key(s)] if s["proposal_id"] == baseline else s for s in template["tests"]]}
            reuse = {"policy": "earliest_qualified_complete_cohort_v1", "request": request,
                     "source_comparison_id": source["record_id"], "sources": cohort["proofs"]}
            _, _, requests = self.registry._prepare_plan(experiment_id, definition_id, resolved,
                submission_identity=submission_identity, purpose=purpose, reuse=reuse)
            requests[0]["artifact_hashes"] = (*requests[0]["artifact_hashes"], *artifacts)
            for item in requests:
                records._insert(records.prepare(**item))
            return self._result(records.read(plan_id))

    def _result(self, plan):
        result = self.registry._registered_plan(plan["experiment_id"], TestPlan.model_validate(plan["value"]["plan"]), plan["record_id"])
        return {**result, "reused_test_ids": sorted(plan["value"]["reuse"]["sources"]),
                "new_test_ids": [t["record_id"] for t in result["tests"] if t["record_id"] not in plan["value"]["reuse"]["sources"]]}
