"""Typed, atomic registrations on the shared append-only experiment store."""
from __future__ import annotations

from itertools import product

from .candidates import read_candidate
from .contracts import CandidateProposal, ResolvedSpecification, TestPlan
from .records import TERMINAL
from .repository import Conflict
from .resolution import digest, encode, verify_specification


def record_identity(experiment_id, kind, local_id):
    # Local names may be reused in another experiment, but never alias a record.
    return f"le-{kind}-" + digest([experiment_id, kind, local_id])


class ExperimentRegistry:
    def __init__(self, records):
        self.records = records
        self.artifacts = records.artifacts

    def _record(self, experiment_id, record_id, kind):
        record = self.records.read(record_id)
        if record["experiment_id"] != experiment_id or record["kind"] != kind or record["value"].get("registration_version") != 1:
            raise Conflict("record has the wrong kind or experiment")
        return record

    def definition(self, experiment_id, specification, *, submission_identity):
        prepared = self._prepare_definition(experiment_id, specification, submission_identity=submission_identity)
        with self.records._transaction():
            return self.records._insert(prepared)

    def _prepare_definition(self, experiment_id, specification, *, submission_identity, calendar_source=None):
        sealed = ResolvedSpecification.model_validate(specification)
        verify_specification(sealed)
        value = {"registration_version": 1, "specification": sealed.model_dump(mode="json")}
        links = ()
        if calendar_source is not None:
            value["calendar_source"] = calendar_source
            links = (("data_bundle", calendar_source["bundle_id"]),)
        ref = self.artifacts.put_bytes(encode(value).encode(), media_type="application/json")
        return self.records.prepare(experiment_id=experiment_id, kind="definition",
                                record_id=record_identity(experiment_id, "definition", submission_identity),
                                submission_identity=submission_identity, value=value, links=links, artifact_hashes=(ref.content_hash,))

    def candidate(self, experiment_id, proposal, *, submission_identity):
        proposal = CandidateProposal.model_validate(proposal)
        package = read_candidate(proposal.package_hash, artifacts=self.artifacts)
        package_id = record_identity(experiment_id, "package", package.package_hash)
        proposal_id = record_identity(experiment_id, "candidate", proposal.proposal_id)
        links = [("package", package_id)]
        if proposal.parent_proposal_id is not None:
            parent_id = record_identity(experiment_id, "candidate", proposal.parent_proposal_id)
            parent = self._record(experiment_id, parent_id, "candidate_proposal")
            # A historical failed/eliminated parent remains usable if its package exists.
            read_candidate(parent["value"]["proposal"]["package_hash"], artifacts=self.artifacts)
            links.append(("parent", parent_id))
        value = {"registration_version": 1, "proposal": proposal.model_dump(mode="json")}
        requests = [
            dict(experiment_id=experiment_id, kind="candidate_package", record_id=package_id,
                 submission_identity=package.package_hash, value={"registration_version": 1, "package": package.model_dump(mode="json")},
                 artifact_hashes=(package.package_hash, *(f.content_hash for f in package.files))),
            dict(experiment_id=experiment_id, kind="candidate_proposal", record_id=proposal_id,
                 submission_identity=submission_identity, value=value, links=links,
                 artifact_hashes=(() if proposal.diff_artifact_hash is None else (proposal.diff_artifact_hash,))),
        ]
        prepared = [self.records.prepare(**request) for request in requests]
        with self.records._transaction():
            if not self.records.db.execute("SELECT 1 FROM lagent_records WHERE record_id=?", (proposal_id,)).fetchone():
                from .selection import require_selection_open
                require_selection_open(self.records, experiment_id)
            package_record, proposal_record = [self.records._insert(item) for item in prepared]
        # Derived display metadata; never inflate tests or alter immutable proposals.
        count = self.records.db.execute("SELECT COUNT(*) FROM lagent_record_links WHERE relation='package' AND target_id=?", (package_id,)).fetchone()[0]
        return {"package": package_record, "proposal": proposal_record, "duplicate_content": count > 1}

    def test_plan(self, experiment_id, definition_id, plan, *, submission_identity, purpose, selection_id=None):
        plan, plan_id, requests = self._prepare_plan(experiment_id, definition_id, plan,
            submission_identity=submission_identity, purpose=purpose, selection_id=selection_id)
        prepared = [self.records.prepare(**request) for request in requests]
        with self.records._transaction():
            if not self.records.db.execute("SELECT 1 FROM lagent_records WHERE record_id=?", (plan_id,)).fetchone():
                from .selection import require_holdout_plan, require_selection_open
                if purpose == "final_holdout":
                    require_holdout_plan(self.records, experiment_id, definition_id, plan, selection_id)
                else:
                    require_selection_open(self.records, experiment_id)
            for item in prepared:
                self.records._insert(item)
            return self._registered_plan(experiment_id, plan, plan_id)

    def _registered_plan(self, experiment_id, plan, plan_id):
        return {"plan": self.records.read(plan_id),
                "tests": [self.records.read(record_identity(experiment_id, "test", s.test_id)) for s in plan.tests],
                "execution_available": False}

    def _prepare_plan(self, experiment_id, definition_id, plan, *, submission_identity, purpose,
                      selection_id=None, reuse=None):
        """Shared plan validation/preparation; host callers own the admission transaction."""
        if (purpose not in {"tuning", "selection_validation", "calibration", "final_holdout"}
                or (purpose == "final_holdout" and selection_id is None)):
            raise ValueError("holdout requires finalized selection; unsupported plan purpose")
        if purpose != "final_holdout" and selection_id is not None:
            raise ValueError("selection freeze reference is only valid for final holdout")
        definition = self._record(experiment_id, definition_id, "definition")
        sealed = ResolvedSpecification.model_validate(definition["value"]["specification"])
        spec = verify_specification(sealed)
        plan = TestPlan.model_validate(plan)
        if plan.specification_hash != sealed.specification_hash:
            raise Conflict("test plan specification differs from its definition")
        task_map = {task.task_id: task for task in spec.tasks}
        selected_tasks = {sample.task_id for sample in plan.tests}
        proposals = {sample.proposal_id for sample in plan.tests}
        expected_role = "tuning" if purpose == "calibration" else purpose
        if not selected_tasks <= task_map.keys() or any(task_map[task].role != expected_role for task in selected_tasks):
            raise Conflict("plan tasks have an unknown or incompatible role")
        if purpose in {"selection_validation", "final_holdout"} and selected_tasks != {t.task_id for t in spec.tasks if t.role == expected_role}:
            raise Conflict("selection plan must include every predeclared selection task")
        if purpose == "calibration" and (len(proposals) != 1 or len(selected_tasks) != 1):
            raise Conflict("calibration requires one initial baseline and one tuning task")
        repeats = spec.budget.calibration_repeats if purpose == "calibration" else spec.evaluate.repeats
        actual = {(s.proposal_id, s.task_id, s.repeat_index) for s in plan.tests}
        expected = set(product(proposals, selected_tasks, range(repeats)))
        if actual != expected:
            raise Conflict("test plan must contain the complete predeclared candidate/task/repeat matrix")
        if len(proposals) > 1:
            width = len(proposals)
            for offset in range(0, len(plan.tests), width):
                block = plan.tests[offset:offset + width]
                if len({(sample.task_id, sample.repeat_index) for sample in block}) != 1 or {sample.proposal_id for sample in block} != proposals:
                    raise Conflict("candidate runs must be predeclared in interleaved task/repeat blocks")
        candidate_records = {name: self._record(experiment_id, record_identity(experiment_id, "candidate", name), "candidate_proposal")
                             for name in sorted(proposals)}
        for candidate in candidate_records.values():
            read_candidate(candidate["value"]["proposal"]["package_hash"], artifacts=self.artifacts)
        plan_id = record_identity(experiment_id, "plan", plan.plan_id)
        plan_value = {"registration_version": 1, "definition_id": definition_id, "purpose": purpose,
                      "plan": plan.model_dump(mode="json")}
        freeze_links = () if selection_id is None else (("selection", selection_id),)
        if selection_id is not None:
            plan_value["selection_id"] = selection_id
        reused = {} if reuse is None else reuse["sources"]
        if reuse is not None:
            plan_value["reuse"] = reuse
        artifact = self.artifacts.put_bytes(encode(plan_value).encode(), media_type="application/json")
        requests = [dict(experiment_id=experiment_id, kind="test_plan", record_id=plan_id,
                         submission_identity=submission_identity, value=plan_value,
                         links=(("definition", definition_id), *freeze_links, *(("test", test_id) for test_id in reused),
                                *(("candidate", item["record_id"]) for item in candidate_records.values())),
                         artifact_hashes=(artifact.content_hash,))]
        for order, sample in enumerate(plan.tests):
            if record_identity(experiment_id, "test", sample.test_id) in reused:
                continue
            value = {"registration_version": 1, "definition_id": definition_id, "plan_id": plan_id,
                     "purpose": purpose, "order": order, "sample": sample.model_dump(mode="json"),
                     "role": task_map[sample.task_id].role, "task_id": sample.task_id}
            if selection_id is not None:
                value["selection_id"] = selection_id
            requests.append(dict(experiment_id=experiment_id, kind="test", record_id=record_identity(experiment_id, "test", sample.test_id),
                                 submission_identity=sample.test_id, value=value,
                                 links=(("definition", definition_id), ("plan", plan_id), *freeze_links,
                                        ("candidate", candidate_records[sample.proposal_id]["record_id"]))))
        return plan, plan_id, requests

    def rerun(self, experiment_id, test_id, *, rerun_identity, reason):
        if not reason:
            raise ValueError("rerun reason required")
        original = self._record(experiment_id, test_id, "test")
        if original["value"].get("purpose") == "final_holdout":
            raise Conflict("final holdout cannot add unplanned reruns")
        if self.records.status(test_id) not in TERMINAL:
            raise Conflict("only terminal tests may be rerun")
        value = {**original["value"], "rerun_of": test_id, "rerun_reason": reason,
                 "eligible_for_original_comparison": False}
        prepared = self.records.prepare(experiment_id=experiment_id, kind="test",
                                record_id=record_identity(experiment_id, "rerun", rerun_identity),
                                submission_identity="rerun:" + rerun_identity, value=value,
                                links=tuple((link["relation"], link["target_id"]) for link in self.records.related(test_id)) + (("rerun_of", test_id),))
        with self.records._transaction():
            if not self.records.db.execute("SELECT 1 FROM lagent_records WHERE record_id=?", (prepared["request"]["record_id"],)).fetchone():
                from .selection import require_selection_open
                require_selection_open(self.records, experiment_id)
            return self.records._insert(prepared)
