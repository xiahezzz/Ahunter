"""Permanent initial baseline and final selection freeze, with dated exposure.

This host control plane commits qualified comparisons with an expected revision;
arithmetic eligibility alone cannot change a baseline.
"""
from datetime import date

from .candidates import read_candidate
from .contracts import ResolvedSpecification
from .records import TERMINAL
from .registration import record_identity
from .repository import Conflict
from .resolution import digest, verify_specification


def current_selection(records, experiment_id):
    row = records.db.execute("SELECT record_id FROM lagent_records WHERE experiment_id=? AND kind='selection' "
        "AND json_extract(value_json,'$.selection_version')=1 ORDER BY record_sequence DESC LIMIT 1",
        (experiment_id,)).fetchone()
    return records.read(row[0]) if row else None


def require_selection_open(records, experiment_id):
    current = current_selection(records, experiment_id)
    if current and current["value"]["operation"] == "finalize":
        raise Conflict("experiment selection is frozen; new optimization requires another experiment")


def selection_overview(records, experiment_id):
    """Public pointer metadata, without exposure reports or comparison samples."""
    record = current_selection(records, experiment_id)
    if record is None:
        return None
    value = record["value"]
    if value.get("operation") not in {"initialize", "promote", "finalize"}:
        raise Conflict("selection operation is unknown")
    fields = ("definition_id", "initial_baseline_id", "current_baseline_id", "initial_package_hash",
              "current_package_hash", "operation", "formal_ready")
    return {**{key: record[key] for key in ("record_id", "content_hash", "sequence", "created_at")},
            **{key: value[key] for key in fields}, "previous_selection_id": value.get("previous_selection_id"),
            "frozen": value["operation"] == "finalize"}


def _cancellation_metadata(records, record):
    """Only the exact control receipt is metadata; attached results are not."""
    value = record["value"]
    fields = {"test_id", "operation"}
    if record["kind"] != "service_control" or value.get("operation") not in {"cancel", "cancel_request"}:
        return False
    if value["operation"] == "cancel_request":
        fields.add("reason")
        if not isinstance(value.get("reason"), str):
            return False
    if set(value) != fields or not isinstance(value["test_id"], str):
        return False
    if records.related(record["record_id"]) != [{"relation": "test", "target_id": value["test_id"]}]:
        return False
    if records.db.execute("SELECT 1 FROM lagent_artifact_links WHERE record_id=?", (record["record_id"],)).fetchone():
        return False
    test = records.read(value["test_id"])
    return test["kind"] == "test" and test["value"].get("registration_version") == 1


def holdout_exposure(records, definition_id):
    """Read retained dates across the single owner's experiments; names do not erase knowledge."""
    definition = records.read(definition_id)
    if definition["kind"] != "definition" or definition["value"].get("registration_version") != 1:
        raise Conflict("holdout exposure requires a typed definition")
    sealed = ResolvedSpecification.model_validate(definition["value"]["specification"])
    spec = verify_specification(sealed)
    dates = {t.task_id: {d.isoformat() for d in t.trading_dates} for t in sealed.tasks}
    holdout_dates = set().union(*(dates[t.task_id] for t in spec.tasks if t.role == "final_holdout"))
    if not holdout_dates:
        raise Conflict("definition has no predeclared final holdout")
    overlaps = sorted(holdout_dates & set().union(*(dates[t.task_id] for t in spec.tasks if t.role != "final_holdout")))
    exposures, unresolved, metadata_reads = [], [], []
    for (identity,) in records.db.execute("SELECT record_id FROM lagent_records WHERE kind='exposure' ORDER BY record_sequence").fetchall():
        record = records.read(identity)
        value, known_dates = record["value"], set()
        if value.get("target_id") and (value.get("action") in {"record_detail", "record_export"}
                                       or value.get("action", "").startswith("artifact:")):
            target = records.read(value["target_id"])
            typed_registration = target["kind"] in {"definition", "test_plan", "test"} and target["value"].get("registration_version") == 1
            typed_selection = target["kind"] == "selection" and target["value"].get("selection_version") == 1
            if typed_registration or typed_selection or _cancellation_metadata(records, target):
                # Dates, seeds and candidate identities are registration metadata,
                # not future market observations. Keep the read audit, but do not
                # make viewing the freeze ID itself consume the holdout.
                metadata_reads.append(identity)
                continue
        for scope in value.get("scopes", ()):
            known_dates.update(date.fromisoformat(d).isoformat() for d in scope.get("trading_dates", ()))
        matched = holdout_dates & known_dates
        for period in value.get("periods", ()):
            start, end = date.fromisoformat(period["start"]), date.fromisoformat(period["end"])
            if end < start:
                raise Conflict("exposure period is invalid")
            matched.update(d for d in holdout_dates if start <= date.fromisoformat(d) <= end)
        if not known_dates and not value.get("periods"):
            unresolved.append({"record_id": identity, "content_hash": record["content_hash"]})
        if matched:
            exposures.append({"record_id": identity, "content_hash": record["content_hash"], "dates": sorted(matched)})
    # Tuning detail is readable without a hidden-detail exposure record. A
    # previously executed non-holdout task therefore also prevents relabeling.
    reused = []
    rows = records.db.execute("SELECT DISTINCT e.test_id FROM lagent_events e, json_each(e.value_json,'$.updates') u "
        "WHERE json_extract(u.value,'$.name')='status' AND json_extract(u.value,'$.value.status')='running'").fetchall()
    for (test_id,) in rows:
        test = records._test(test_id)
        if test["value"].get("registration_version") != 1:
            continue
        prior_definition = records.read(test["value"]["definition_id"])
        prior_sealed = ResolvedSpecification.model_validate(prior_definition["value"]["specification"])
        prior_spec = verify_specification(prior_sealed)
        task_id = test["value"]["task_id"]
        role = next(t.role for t in prior_spec.tasks if t.task_id == task_id)
        if role == "final_holdout":
            continue
        matched = holdout_dates & {d.isoformat() for t in prior_sealed.tasks if t.task_id == task_id for d in t.trading_dates}
        if matched:
            reused.append({"test_id": test_id, "test_hash": test["content_hash"], "dates": sorted(matched)})
    return {"definition_id": definition_id, "definition_hash": definition["content_hash"],
            "holdout_dates": sorted(holdout_dates), "overlapping_roles": overlaps,
            "exposures": exposures, "unresolved_exposures": unresolved, "previous_training_tests": reused,
            "metadata_read_ids": metadata_reads,
            "unseen": not (overlaps or exposures or unresolved or reused)}


class ExperimentSelection:
    def __init__(self, records):
        self.records = records

    def initialize(self, experiment_id, definition_id, baseline_id):
        records = self.records
        definition, baseline = records.read(definition_id), records.read(baseline_id)
        if (definition["experiment_id"] != experiment_id or definition["kind"] != "definition"
                or definition["value"].get("registration_version") != 1
                or baseline["experiment_id"] != experiment_id or baseline["kind"] != "candidate_proposal"
                or baseline["value"].get("registration_version") != 1
                or baseline["value"]["proposal"]["parent_proposal_id"] is not None):
            raise Conflict("initial selection requires its definition and a registered root baseline")
        verify_specification(ResolvedSpecification.model_validate(definition["value"]["specification"]))
        package_hash = baseline["value"]["proposal"]["package_hash"]
        read_candidate(package_hash, artifacts=records.artifacts)
        identity = record_identity(experiment_id, "selection", "initial")
        value = {"selection_version": 1, "operation": "initialize", "definition_id": definition_id,
                 "initial_baseline_id": baseline_id, "current_baseline_id": baseline_id,
                 "initial_package_hash": package_hash, "current_package_hash": package_hash,
                 "formal_ready": False}
        prepared = records.prepare(experiment_id=experiment_id, kind="selection", record_id=identity,
            submission_identity="selection:initial", value=value, links=(("target", definition_id), ("candidate", baseline_id)))
        with records._transaction():
            if not records.db.execute("SELECT 1 FROM lagent_records WHERE record_id=?", (identity,)).fetchone():
                if current_selection(records, experiment_id) is not None:
                    raise Conflict("initial selection already exists")
                for (test_id,) in records.db.execute("SELECT record_id FROM lagent_records WHERE experiment_id=? AND kind='test'", (experiment_id,)):
                    if records.status(test_id) != "created" or records.projection(test_id, "cost_budget") is not None:
                        raise Conflict("initial baseline must be fixed before any Test starts")
            return records._insert(prepared)

    def apply_comparison(self, comparison_id, *, expected_selection_id, submission_identity):
        """One permanent decision per comparison; only promotion advances selection."""
        from .evaluate import qualified_comparison
        if not isinstance(submission_identity, str) or not submission_identity.strip():
            raise ValueError("selection submission identity required")
        records = self.records
        comparison = records.read(comparison_id)
        c = comparison["value"]
        if (comparison["kind"] != "comparison" or c.get("comparison_version") != 1
                or c.get("record_type") != "result" or c.get("status") != "completed"):
            raise Conflict("selection requires a completed comparison record")
        experiment_id = comparison["experiment_id"]
        identity = record_identity(experiment_id, "selection-decision", comparison_id)
        request = {"comparison_id": comparison_id, "expected_selection_id": expected_selection_id,
                   "submission_identity": submission_identity}
        with records._transaction():
            if records.db.execute("SELECT 1 FROM lagent_records WHERE record_id=?", (identity,)).fetchone():
                previous = records.read(identity)
                if previous["value"]["request"] != request:
                    raise Conflict("comparison selection decision already has different inputs")
                return previous
            frozen = records.read(c["comparison_id"])
            f = frozen["value"]
            if (frozen["kind"] != "comparison" or f.get("comparison_version") != 1
                    or f.get("record_type") != "plan" or frozen["experiment_id"] != experiment_id
                    or c["registration_fingerprint"] != f["registration_fingerprint"]
                    or c.get("selection_id") != f.get("selection_id")):
                raise Conflict("selection comparison binding mismatch")
            if f.get("selection_id") is not None and expected_selection_id != f["selection_id"]:
                raise Conflict("comparison cannot be rebound to another selection revision")
            current = current_selection(records, experiment_id)
            if current is None:
                raise Conflict("selection must be initialized before applying comparisons")
            result, qualified = qualified_comparison(records, frozen, c["samples"])
            if (result != c["result"] or c["decision"] != result["decision"]
                    or c.get("formal_ready") is not qualified
                    or c.get("promotion_authorized") is not (qualified and result["decision"] == "eligible")
                    or c.get("runtime_fingerprint") != f.get("runtime_fingerprint")):
                raise Conflict("comparison decision contradicts its original evidence")
            if current["value"]["operation"] == "finalize":
                decision, reason = "selection_frozen", "selection_frozen"
            elif current["record_id"] != expected_selection_id:
                decision, reason = "stale_baseline", "selection_revision_changed"
            elif f.get("selection_id") is None:
                decision, reason = "inconclusive", "comparison_has_no_selection_binding"
            elif result["decision"] != "eligible":
                decision, reason = result["decision"], result["reason"]
            elif not qualified:
                decision, reason = "inconclusive", "runtime_acceptance_incomplete"
            else:
                baseline_id = record_identity(experiment_id, "candidate", f["baseline"])
                if (current["content_hash"] != f["selection_hash"]
                        or current["value"]["current_baseline_id"] != baseline_id
                        or current["value"]["definition_id"] != f["definition_id"]):
                    raise Conflict("comparison must qualify against the current baseline and definition")
                decision, reason = "promoted", "selection_thresholds_satisfied"
            value = {"selection_decision_version": 1, "request": request, "decision": decision, "reason": reason,
                     "comparison_hash": comparison["content_hash"], "observed_selection_id": current["record_id"],
                     "observed_selection_hash": current["content_hash"], "formal_ready": qualified}
            links = [("comparison", comparison_id), ("selection", current["record_id"])]
            if decision == "promoted":
                candidate_id = record_identity(experiment_id, "candidate", f["candidate"])
                candidate = records.read(candidate_id)
                package_hash = f["packages"][f["candidate"]]
                if candidate["value"]["proposal"]["package_hash"] != package_hash:
                    raise Conflict("comparison candidate package mismatch")
                read_candidate(package_hash, artifacts=records.artifacts)
                previous = current["value"]
                value.update(selection_version=1, operation="promote", definition_id=previous["definition_id"],
                    initial_baseline_id=previous["initial_baseline_id"], initial_package_hash=previous["initial_package_hash"],
                    current_baseline_id=candidate_id, current_package_hash=package_hash,
                    previous_selection_id=current["record_id"], previous_selection_hash=current["content_hash"])
                links.extend((("candidate", candidate_id), ("target", previous["definition_id"])))
            prepared = records.prepare(experiment_id=experiment_id, kind="selection", record_id=identity,
                submission_identity="selection-decision:" + comparison_id, value=value, links=links)
            return records._insert(prepared)

    def finalize(self, experiment_id, *, expected_selection_id, submission_identity):
        if not isinstance(submission_identity, str) or not submission_identity.strip():
            raise ValueError("selection submission identity required")
        records = self.records
        identity = record_identity(experiment_id, "selection-freeze", submission_identity)
        with records._transaction():
            if records.db.execute("SELECT 1 FROM lagent_records WHERE record_id=?", (identity,)).fetchone():
                previous = records.read(identity)
                if previous["value"]["previous_selection_id"] != expected_selection_id:
                    raise Conflict("selection identity already has different inputs")
                return previous
            current = current_selection(records, experiment_id)
            if current is None or current["record_id"] != expected_selection_id:
                raise Conflict("selection revision changed")
            require_selection_open(records, experiment_id)
            for (test_id,) in records.db.execute("SELECT record_id FROM lagent_records WHERE experiment_id=? AND kind='test'", (experiment_id,)):
                if records.status(test_id) not in TERMINAL:
                    raise Conflict("selection cannot freeze while planned Tests remain unfinished")
                for name in ("candidate_processes", "model_invocations"):
                    state = records.projection(test_id, name)
                    if state and any(not call["quiescent"] or call["status"] == "prepared" for call in state["value"]["calls"].values()):
                        raise Conflict("selection cannot freeze while physical cleanup is unknown")
            value = current["value"]
            report = holdout_exposure(records, value["definition_id"])
            if not report["unseen"]:
                raise Conflict("final holdout is exposed, overlaps training, or lacks exposure provenance")
            for package in {value["initial_package_hash"], value["current_package_hash"]}:
                read_candidate(package, artifacts=records.artifacts)
            frozen = {**value, "operation": "finalize", "previous_selection_id": current["record_id"],
                      "previous_selection_hash": current["content_hash"], "exposure_at_freeze": report,
                      "formal_ready": False}
            prepared = records.prepare(experiment_id=experiment_id, kind="selection", record_id=identity,
                submission_identity="selection-freeze:" + submission_identity, value=frozen,
                links=(("target", value["definition_id"]), ("selection", current["record_id"]),
                       ("candidate", value["initial_baseline_id"]), ("candidate", value["current_baseline_id"])))
            return records._insert(prepared)


def require_holdout_plan(records, experiment_id, definition_id, plan, selection_id):
    """Called inside the same writer transaction as complete plan insertion."""
    current = current_selection(records, experiment_id)
    if (current is None or current["record_id"] != selection_id or current["value"]["operation"] != "finalize"
            or current["value"]["definition_id"] != definition_id):
        raise Conflict("holdout requires the exact finalized selection and definition")
    expected = {records.read(current["value"][key])["value"]["proposal"]["proposal_id"]
                for key in ("initial_baseline_id", "current_baseline_id")}
    if {sample.proposal_id for sample in plan.tests} != expected:
        raise Conflict("holdout runs only the initial baseline and final selected candidate")
    existing = records.db.execute("SELECT record_id FROM lagent_records WHERE experiment_id=? AND kind='test_plan' "
        "AND json_extract(value_json,'$.selection_id')=?", (experiment_id, selection_id)).fetchone()
    if existing:
        raise Conflict("final selection already has its single predeclared holdout plan")
    if not holdout_exposure(records, definition_id)["unseen"]:
        raise Conflict("holdout exposure changed after selection freeze")
