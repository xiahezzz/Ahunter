"""Selection integration using synthetic accepted-assessor contracts.

These receipt fixtures test the consumer/CAS boundary. The real Episode assessor
still rejects fixture replay and cannot emit these formal acceptance claims.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
import sqlite3
from threading import Barrier

import pytest
from pydantic import ValidationError

from advisor.research.experiments.evaluate import ComparisonAssessments
from advisor.research.experiments.queries import ExperimentQueries
from advisor.research.experiments.records import ExperimentRecords
from advisor.research.experiments.registration import record_identity
from advisor.research.experiments.repository import Conflict
from advisor.research.experiments.resolution import digest
from advisor.research.experiments.selection import ExperimentSelection, current_selection
from tests.advisor.research.test_experiment_registration import registry, candidate, plan_input


def setup(registry):
    service, experiment, definition, package, _ = registry
    root = candidate(service, experiment, package)["proposal"]
    for name in ("a", "b", "descendant"):
        candidate(service, experiment, package, name, "baseline")
    selector = ExperimentSelection(service.records)
    initial = selector.initialize(experiment, definition["record_id"], root["record_id"])
    return selector, initial


def conditions(records):
    result = {"version": 1, "actual_model_id": "fixture-model", "actual_reasoning_effort": "high",
              "data_corpus_generation": "fixture-generation-1", "scoring_version": "mean_terminal_net_return@1"}
    for name in ("data_corpus", "search_policy", "executor", "fee_schedule", "price_table", "budget"):
        result[name + "_hash"] = records.artifacts.put_text("synthetic accepted host contract: " + name).content_hash
    return {"august-selection": result}


def pair(registry, selection, *, name="a", opponent="baseline", plan_id=None, runtime=True):
    service, experiment, definition, _, _ = registry
    plan_id = plan_id or "pair-" + name
    plan = service.test_plan(experiment, definition["record_id"],
        plan_input(definition, names=(opponent, name), task="august-selection", plan_id=plan_id),
        submission_identity=plan_id, purpose="selection_validation")
    comparer = ComparisonAssessments(service.records)
    frozen = comparer.freeze(plan["plan"]["record_id"], baseline=opponent, candidate=name, submission_identity=plan_id,
        expected_selection_id=selection["record_id"], runtime_conditions=conditions(service.records) if runtime else None)
    return comparer, plan, frozen


def accepted_samples(registry, plan, frozen, *, differences=("0.002", "0.002", "0.002"), invalid=None, terminal_status=None):
    """Stand-in accepted host facts, with real typed plans and Test lifecycles."""
    service, experiment, definition, package, _ = registry
    records = service.records
    for order, test in enumerate(plan["tests"]):
        test_id, sample = test["record_id"], test["value"]["sample"]
        for state in ("preflight", "queued"):
            records.transition(test_id, state, action_id=state)
        lease = records.claim(test_id, worker_id="accepted-contract-fixture", lease_seconds=10000)
        for state in ("running", "evaluating", terminal_status if terminal_status and order == 1 else "completed"):
            records.transition(test_id, state, action_id=state, lease=lease)
        delta = Decimal(differences[sample["repeat_index"]]) if sample["proposal_id"] == frozen["value"]["candidate"] else Decimal(0)
        value = {"evaluation_version": 1, "request": {"test_id": test_id, "rescore_of": None},
                 "status_at_assessment": "completed", "sample": sample, "valid": True, "formal_ready": True, "reasons": [],
                 "role": "selection_validation", "specification_hash": definition["value"]["specification"]["specification_hash"],
                 "runtime_conditions_hash": digest(conditions(records)[sample["task_id"]]), "package_hash": package.package_hash,
                 "nav_diagnostic": {"initial_nav": "100000", "terminal_nav": str(100000 * (1 + delta)), "formal_score": True}}
        if invalid and order == 1:
            invalid(value)
        records.put(experiment_id=experiment, kind="evaluation", record_id="accepted-" + test_id,
                    submission_identity="accepted-" + test_id, value=value, links=(("test", test_id),))


def apply(selector, selection, result, identity="apply"):
    return selector.apply_comparison(result["record_id"], expected_selection_id=selection["record_id"], submission_identity=identity)


def test_qualified_comparison_only_promotes_on_explicit_selection_cas(registry):
    selector, initial = setup(registry)
    comparer, plan, frozen = pair(registry, initial)
    accepted_samples(registry, plan, frozen)
    result = comparer.complete(frozen["record_id"])
    assert result["value"]["decision"] == "eligible" and result["value"]["promotion_authorized"]
    assert current_selection(selector.records, initial["experiment_id"]) == initial
    feedback = ExperimentQueries(selector.records, viewer="optimizer").comparison_feedback(result["record_id"])
    assert feedback["decision"] == "eligible"  # Eligibility does not claim pointer movement.
    promoted = apply(selector, initial, result)
    assert promoted["value"]["decision"] == "promoted"
    assert promoted["value"]["initial_baseline_id"] == initial["value"]["initial_baseline_id"]
    assert promoted["value"]["current_baseline_id"] == record_identity(initial["experiment_id"], "candidate", "a")
    assert current_selection(selector.records, initial["experiment_id"]) == promoted
    assert apply(selector, initial, result) == promoted
    assert comparer.complete(frozen["record_id"]) == result
    assert comparer.freeze(plan["plan"]["record_id"], baseline="baseline", candidate="a", submission_identity="pair-a",
        expected_selection_id=initial["record_id"], runtime_conditions=conditions(selector.records)) == frozen


@pytest.mark.parametrize("differences,decision", [
    (("0", "0", "0"), "not_improved"), (("-0.1",) * 3, "not_improved"),
    (("0.02", "0", "0"), "unstable"), (("0.007", "0.007", "-0.011"), "unstable"),
])
def test_valid_ties_and_low_or_unstable_returns_retain_baseline(registry, differences, decision):
    selector, initial = setup(registry)
    comparer, plan, frozen = pair(registry, initial)
    accepted_samples(registry, plan, frozen, differences=differences)
    result = comparer.complete(frozen["record_id"])
    recorded = apply(selector, initial, result)
    assert recorded["value"]["decision"] == decision
    assert current_selection(selector.records, initial["experiment_id"]) == initial
    assert apply(selector, initial, result) == recorded


@pytest.mark.parametrize("field,value", [
    ("formal_ready", False), ("valid", False), ("runtime_conditions_hash", "0" * 64),
    ("package_hash", "0" * 64), ("reasons", ["usage_unsettled"]),
    ("specification_hash", "0" * 64),
])
def test_declared_fingerprint_cannot_replace_actual_sample_acceptance(registry, field, value):
    selector, initial = setup(registry)
    comparer, plan, frozen = pair(registry, initial)
    accepted_samples(registry, plan, frozen, invalid=lambda a: a.update({field: value}))
    result = comparer.complete(frozen["record_id"])
    assert not result["value"]["promotion_authorized"]
    assert apply(selector, initial, result)["value"]["decision"] == "inconclusive"
    assert current_selection(selector.records, initial["experiment_id"]) == initial


def test_no_runtime_fingerprint_keeps_otherwise_accepted_samples_inconclusive(registry):
    selector, initial = setup(registry)
    comparer, plan, frozen = pair(registry, initial, runtime=False)
    accepted_samples(registry, plan, frozen)
    result = comparer.complete(frozen["record_id"])
    assert result["value"]["decision"] == "inconclusive"
    assert apply(selector, initial, result)["value"]["decision"] == "inconclusive"


def test_stale_comparison_cannot_overwrite_new_baseline_or_rebind_to_it(registry):
    selector, initial = setup(registry)
    first = pair(registry, initial, name="a")
    second = pair(registry, initial, name="b")
    for _, plan, frozen in (first, second):
        accepted_samples(registry, plan, frozen)
    result_a, result_b = [comparer.complete(frozen["record_id"]) for comparer, _, frozen in (first, second)]
    promoted = apply(selector, initial, result_a)
    with pytest.raises(Conflict, match="rebound"):
        apply(selector, promoted, result_b)
    stale = apply(selector, initial, result_b, "stale")
    assert stale["value"]["decision"] == "stale_baseline"
    assert current_selection(selector.records, initial["experiment_id"]) == promoted
    with pytest.raises(Conflict, match="current baseline"):
        pair(registry, promoted, name="descendant", opponent="baseline")
    # Winning a parent is insufficient; a new plan must face current 'a'.
    newer, plan, frozen = pair(registry, promoted, name="descendant", opponent="a", plan_id="against-current")
    accepted_samples(registry, plan, frozen)
    final = apply(selector, promoted, newer.complete(frozen["record_id"]), "new-current")
    assert final["value"]["current_baseline_id"] == record_identity(initial["experiment_id"], "candidate", "descendant")


@pytest.mark.parametrize("point", ["before_commit", "after_commit"])
def test_promotion_interruption_has_one_permanent_decision_and_pointer(registry, point):
    selector, initial = setup(registry)
    comparer, plan, frozen = pair(registry, initial)
    accepted_samples(registry, plan, frozen)
    result = comparer.complete(frozen["record_id"])
    def fault(actual):
        if actual == point:
            raise OSError("selection interrupted")
    selector.records.fault = fault
    with pytest.raises(OSError):
        apply(selector, initial, result)
    selector.records.fault = lambda _: None
    final = apply(selector, initial, result)
    assert current_selection(selector.records, initial["experiment_id"]) == final
    assert selector.records.db.execute("SELECT count(*) FROM lagent_records WHERE kind='selection'").fetchone()[0] == 2


def test_competing_qualified_comparisons_cannot_both_replace_same_revision(registry):
    selector, initial = setup(registry)
    results = []
    for name in ("a", "b"):
        comparer, plan, frozen = pair(registry, initial, name=name)
        accepted_samples(registry, plan, frozen)
        results.append(comparer.complete(frozen["record_id"]))
    path = selector.records.db.execute("PRAGMA database_list").fetchone()[2]
    barrier = Barrier(2)
    def submit(result):
        db = sqlite3.connect(path, timeout=10)
        try:
            local = ExperimentSelection(ExperimentRecords(db, artifacts=selector.records.artifacts))
            barrier.wait()
            return apply(local, initial, result)["value"]["decision"]
        finally:
            db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(submit, results))
    assert sorted(decisions) == ["promoted", "stale_baseline"]


def test_holdout_uses_original_and_promoted_candidate_and_freeze_blocks_later_selection(registry):
    selector, initial = setup(registry)
    a, b = pair(registry, initial, name="a"), pair(registry, initial, name="b")
    for _, plan, frozen in (a, b):
        accepted_samples(registry, plan, frozen)
    result_a, result_b = [comparer.complete(frozen["record_id"]) for comparer, _, frozen in (a, b)]
    promoted = apply(selector, initial, result_a)
    final = selector.finalize(initial["experiment_id"], expected_selection_id=promoted["record_id"], submission_identity="final")
    assert apply(selector, initial, result_b)["value"]["decision"] == "selection_frozen"
    service, experiment, definition, _, _ = registry
    holdout = service.test_plan(experiment, definition["record_id"],
        plan_input(definition, names=("baseline", "a"), task="august-holdout", plan_id="holdout"),
        purpose="final_holdout", selection_id=final["record_id"], submission_identity="holdout")
    assert len(holdout["tests"]) == 6
    assert current_selection(selector.records, experiment) == final


def test_runtime_condition_contract_requires_all_tasks_dimensions_and_source_bytes(registry):
    selector, initial = setup(registry)
    service, experiment, definition, _, _ = registry
    plan = service.test_plan(experiment, definition["record_id"],
        plan_input(definition, names=("baseline", "a"), task="august-selection", plan_id="conditions"),
        purpose="selection_validation", submission_identity="conditions")
    def attempt(raw):
        return ComparisonAssessments(service.records).freeze(plan["plan"]["record_id"], baseline="baseline", candidate="a",
            submission_identity="conditions", expected_selection_id=initial["record_id"], runtime_conditions=raw)
    with pytest.raises(Conflict, match="exact"):
        attempt({})
    missing = conditions(service.records)
    del missing["august-selection"]["actual_model_id"]
    with pytest.raises(ValidationError):
        attempt(missing)
    missing = conditions(service.records)
    missing["august-selection"]["data_corpus_hash"] = "0" * 64
    with pytest.raises(FileNotFoundError):
        attempt(missing)
    assert attempt(conditions(service.records))["value"]["runtime_fingerprint_complete"]


@pytest.mark.parametrize("status", ["blocked", "failed", "cancelled"])
def test_failed_required_test_overrides_optimistic_assessor_flags(registry, status):
    selector, initial = setup(registry)
    comparer, plan, frozen = pair(registry, initial)
    accepted_samples(registry, plan, frozen, terminal_status=status)
    result = comparer.complete(frozen["record_id"])
    assert result["value"]["decision"] == "inconclusive"
    assert apply(selector, initial, result)["value"]["decision"] == "inconclusive"
    assert current_selection(selector.records, initial["experiment_id"]) == initial


@pytest.mark.parametrize("change", ["decision", "samples"])
def test_selection_recomputes_evidence_instead_of_trusting_result_header(registry, change):
    selector, initial = setup(registry)
    comparer, plan, frozen = pair(registry, initial)
    accepted_samples(registry, plan, frozen)
    result = comparer.complete(frozen["record_id"])
    altered = deepcopy(result["value"])
    if change == "decision":
        altered["decision"] = "promoted"
    else:
        altered["samples"] = altered["samples"][:-1]
    forged = selector.records.put(experiment_id=initial["experiment_id"], kind="comparison", record_id="inconsistent-result",
        submission_identity="inconsistent-result", value=altered,
        links=tuple((link["relation"], link["target_id"]) for link in selector.records.related(result["record_id"])))
    with pytest.raises(Conflict):
        apply(selector, initial, forged)
    assert current_selection(selector.records, initial["experiment_id"]) == initial
