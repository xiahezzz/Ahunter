from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Barrier

import pytest

from advisor.research.experiments.evaluate import ComparisonAssessments
from advisor.research.experiments.repository import Conflict
from advisor.research.experiments.records import ExperimentRecords
from advisor.research.experiments.reuse import BaselineReuse
from advisor.research.experiments.selection import current_selection
from tests.advisor.research.test_experiment_registration import registry, plan_input
from tests.advisor.research.test_experiment_promotion import setup, pair, accepted_samples, conditions, apply


def template(registry, original_plan, *, name="reuse-b", candidate="b", baseline="baseline"):
    definition = registry[2]
    result = plan_input(definition, names=(baseline, candidate), task="august-selection", plan_id=name)
    repeats = {s["repeat_index"]: (s["repeat_id"], s["seed"]) for s in original_plan["plan"]["value"]["plan"]["tests"]}
    for sample in result["tests"]:
        sample["repeat_id"], sample["seed"] = repeats[sample["repeat_index"]]
    return result


def reuse(registry, initial, raw, **changes):
    service, experiment, definition, _, _ = registry
    args = {"expected_selection_id": initial["record_id"], "runtime_conditions": conditions(service.records),
            "submission_identity": raw["plan_id"], "purpose": "selection_validation", **changes}
    return BaselineReuse(service.records).register(experiment, definition["record_id"], raw, **args)


def freeze_reuse(registry, initial, reused):
    records = registry[0].records
    baseline = records.read(initial["value"]["current_baseline_id"])["value"]["proposal"]["proposal_id"]
    return ComparisonAssessments(records).freeze(reused["plan"]["record_id"], baseline=baseline, candidate="b",
        submission_identity="compare-" + reused["plan"]["value"]["plan"]["plan_id"],
        expected_selection_id=initial["record_id"], runtime_conditions=conditions(registry[0].records))


def source(registry, initial, first_plan, *, name="second", omit=(), baseline_return=None):
    service, experiment, definition, _, _ = registry
    raw = template(registry, first_plan, name=name, candidate="a")
    plan = service.test_plan(experiment, definition["record_id"], raw, purpose="selection_validation", submission_identity=name)
    frozen = ComparisonAssessments(service.records).freeze(plan["plan"]["record_id"], baseline="baseline", candidate="a",
        submission_identity=name, expected_selection_id=initial["record_id"], runtime_conditions=conditions(service.records))
    missing = {test["record_id"] for test in plan["tests"] if test["value"]["sample"]["proposal_id"] == "baseline"
               and test["value"]["sample"]["repeat_index"] in omit}
    accepted_view = deepcopy(frozen)
    if baseline_return is not None:
        accepted_view["value"]["candidate"] = "baseline"  # Synthetic accepted source NAV fixtures.
    accepted_samples(registry, {"tests": [test for test in plan["tests"] if test["record_id"] not in missing]}, accepted_view,
        differences=(baseline_return or "0.002",) * 3)
    return plan, frozen, missing


def test_reuse_keeps_original_tests_and_costs_and_can_qualify_new_comparison(registry):
    selector, initial = setup(registry)
    comparer, original, frozen = pair(registry, initial)
    accepted_samples(registry, original, frozen)
    original_result = comparer.complete(frozen["record_id"])
    records = selector.records
    baseline_ids = [t["record_id"] for t in original["tests"] if t["value"]["sample"]["proposal_id"] == "baseline"]
    original_states = {t: records.projection(t, "status") for t in baseline_ids}
    cost = records.put(experiment_id=initial["experiment_id"], kind="cost", record_id="original-bill",
        submission_identity="original-bill", value={"fixture_paid_once": "2.00"}, links=(("test", baseline_ids[0]),))
    raw = template(registry, original)
    resolved = reuse(registry, initial, raw)
    assert resolved["reused_test_ids"] == sorted(baseline_ids)
    assert len(resolved["tests"]) == 6 and len(resolved["new_test_ids"]) == 3
    assert records.db.execute("SELECT count(*) FROM lagent_records WHERE kind='test'").fetchone()[0] == 9
    assert records.db.execute("SELECT count(*) FROM lagent_records WHERE kind='cost'").fetchone()[0] == 1
    assert records.read(cost["record_id"]) == cost
    assert {t: records.projection(t, "status") for t in baseline_ids} == original_states
    assert resolved["plan"]["value"]["reuse"]["source_comparison_id"] == frozen["record_id"]
    bound = freeze_reuse(registry, initial, resolved)
    fresh = {"tests": [t for t in resolved["tests"] if t["record_id"] in resolved["new_test_ids"]]}
    accepted_samples(registry, fresh, bound)
    result = comparer.complete(bound["record_id"])
    assert result["value"]["promotion_authorized"]
    promoted = apply(selector, initial, result)
    assert promoted["value"]["decision"] == "promoted"
    assert records.read(original_result["record_id"]) == original_result
    assert reuse(registry, initial, raw) == resolved  # Same input does not rebind after promotion.


def test_earliest_complete_cohort_is_selected_even_when_newer_baseline_returns_are_higher(registry):
    _, initial = setup(registry)
    _, first, frozen = pair(registry, initial)
    accepted_samples(registry, first, frozen)
    _, newer, _ = source(registry, initial, first, baseline_return="0.2")
    result = reuse(registry, initial, template(registry, first))
    assert result["plan"]["value"]["reuse"]["source_comparison_id"] == frozen["record_id"]
    assert result["plan"]["value"]["reuse"]["source_comparison_id"] != newer["record_id"]


def test_incomplete_groups_are_not_mixed_and_later_eligibility_does_not_rewrite_a_plan(registry):
    service, _, _, _, _ = registry
    _, initial = setup(registry)
    _, first, frozen = pair(registry, initial)
    missing = first["tests"][4]  # baseline repeat 2
    accepted_samples(registry, {"tests": [t for t in first["tests"] if t != missing]}, frozen)
    second, second_frozen, second_missing = source(registry, initial, first, omit=(1,))
    before = service.records.db.execute("SELECT count(*) FROM lagent_records WHERE kind='test'").fetchone()[0]
    raw = template(registry, first)
    with pytest.raises(Conflict, match="no complete"):
        reuse(registry, initial, raw)
    assert service.records.db.execute("SELECT count(*) FROM lagent_records WHERE kind='test'").fetchone()[0] == before
    accepted_samples(registry, {"tests": [t for t in second["tests"] if t["record_id"] in second_missing]}, second_frozen)
    result = reuse(registry, initial, raw)
    assert result["plan"]["value"]["reuse"]["source_comparison_id"] == second_frozen["record_id"]
    accepted_samples(registry, {"tests": [missing]}, frozen)
    assert reuse(registry, initial, raw) == result
    assert freeze_reuse(registry, initial, result)["value"]["plan_id"] == result["plan"]["record_id"]


@pytest.mark.parametrize("mismatch", ["repeat_id", "seed", "conditions", "definition", "role"])
def test_reuse_requires_exact_repeat_design_conditions_definition_and_role(registry, mismatch):
    service, experiment, definition, _, _ = registry
    _, initial = setup(registry)
    _, original, frozen = pair(registry, initial)
    accepted_samples(registry, original, frozen)
    raw, args = template(registry, original), {}
    if mismatch == "repeat_id":
        for sample in raw["tests"]:
            sample["repeat_id"] = "new-" + sample["repeat_id"]
    elif mismatch == "seed":
        raw["seed_support"] = "supported"
        for sample in raw["tests"]:
            sample["seed"] = 42
    elif mismatch == "conditions":
        args["runtime_conditions"] = conditions(service.records)
        args["runtime_conditions"]["august-selection"]["data_corpus_generation"] = "different-generation"
    elif mismatch == "role":
        args["purpose"] = "final_holdout"
    else:
        alias = service.definition(experiment, definition["value"]["specification"], submission_identity="alias")
        with pytest.raises(Conflict, match="definition"):
            BaselineReuse(service.records).register(experiment, alias["record_id"], raw,
                expected_selection_id=initial["record_id"], runtime_conditions=conditions(service.records),
                purpose="selection_validation", submission_identity="different-definition")
        return
    with pytest.raises(Conflict):
        reuse(registry, initial, raw, **args)


@pytest.mark.parametrize("point", ["after_record", "before_commit", "after_commit"])
def test_reuse_registration_is_atomic_and_retry_does_not_add_another_cohort(registry, point):
    selector, initial = setup(registry)
    _, original, frozen = pair(registry, initial)
    accepted_samples(registry, original, frozen)
    raw = template(registry, original)
    seen = 0
    def fault(actual):
        nonlocal seen
        if actual == "after_record":
            seen += 1
        if actual == point and (point != "after_record" or seen == 3):
            raise OSError("reuse interrupted")
    selector.records.fault = fault
    with pytest.raises(OSError):
        reuse(registry, initial, raw)
    selector.records.fault = lambda _: None
    count = selector.records.db.execute("SELECT count(*) FROM lagent_records WHERE kind='test'").fetchone()[0]
    assert count == (9 if point == "after_commit" else 6)
    result = reuse(registry, initial, raw)
    assert reuse(registry, initial, raw) == result
    assert selector.records.db.execute("SELECT count(*) FROM lagent_records WHERE kind='test'").fetchone()[0] == 9


def test_reused_plan_cannot_change_binding_or_runtime_and_direct_registration_cannot_alias_tests(registry):
    service, experiment, definition, _, _ = registry
    _, initial = setup(registry)
    _, original, frozen = pair(registry, initial)
    accepted_samples(registry, original, frozen)
    raw = template(registry, original)
    result = reuse(registry, initial, raw)
    changed = deepcopy(raw)
    changed["tests"][1]["test_id"] = "different-candidate-test"
    with pytest.raises(Conflict, match="different inputs"):
        reuse(registry, initial, changed)
    different = conditions(service.records)
    different["august-selection"]["actual_model_id"] = "different-model"
    with pytest.raises(Conflict, match="exact runtime"):
        ComparisonAssessments(service.records).freeze(result["plan"]["record_id"], baseline="baseline", candidate="b",
            submission_identity="wrong-runtime", expected_selection_id=initial["record_id"], runtime_conditions=different)
    unmarked = deepcopy(result["plan"]["value"]["plan"])
    unmarked["plan_id"] = "unmarked-alias"
    with pytest.raises(Conflict):
        service.test_plan(experiment, definition["record_id"], unmarked, purpose="selection_validation", submission_identity="unmarked")


def test_new_candidate_tests_must_still_be_unstarted_when_reuse_comparison_freezes(registry):
    selector, initial = setup(registry)
    _, original, frozen = pair(registry, initial)
    accepted_samples(registry, original, frozen)
    result = reuse(registry, initial, template(registry, original))
    selector.records.transition(result["new_test_ids"][0], "preflight", action_id="too-early")
    with pytest.raises(Conflict, match="before execution"):
        freeze_reuse(registry, initial, result)


def test_promoted_candidate_original_cohort_becomes_baseline_without_copying_actions(registry):
    selector, initial = setup(registry)
    comparer, original, frozen = pair(registry, initial)
    accepted_samples(registry, original, frozen)
    promoted = apply(selector, initial, comparer.complete(frozen["record_id"]))
    raw = template(registry, original, baseline="a")
    resolved = reuse(registry, promoted, raw)
    original_candidate_ids = sorted(t["record_id"] for t in original["tests"] if t["value"]["sample"]["proposal_id"] == "a")
    assert resolved["reused_test_ids"] == original_candidate_ids
    for test_id in resolved["new_test_ids"]:
        assert selector.records.status(test_id) == "created"
        for name in ("account", "phase_clock", "cost_budget", "candidate_processes", "model_invocations"):
            assert selector.records.projection(test_id, name) is None
    bound = freeze_reuse(registry, promoted, resolved)
    accepted_samples(registry, {"tests": [t for t in resolved["tests"] if t["record_id"] in resolved["new_test_ids"]]},
                     bound, differences=("0.01",) * 3)
    final = apply(selector, promoted, comparer.complete(bound["record_id"]))
    assert final["value"]["decision"] == "promoted"
    assert current_selection(selector.records, initial["experiment_id"]) == final


def test_later_valid_rescore_cannot_launder_ineligible_original_baseline(registry):
    selector, initial = setup(registry)
    _, original, frozen = pair(registry, initial)
    baseline = [t for t in original["tests"] if t["value"]["sample"]["proposal_id"] == "baseline"]
    accepted_samples(registry, {"tests": baseline}, frozen, invalid=lambda a: a.update(formal_ready=False))
    invalid_id = "accepted-" + baseline[1]["record_id"]
    original_assessment = selector.records.read(invalid_id)
    corrected = deepcopy(original_assessment["value"])
    corrected.update(formal_ready=True)
    corrected["request"]["rescore_of"] = invalid_id
    selector.records.put(experiment_id=initial["experiment_id"], kind="evaluation", record_id="later-rescore",
        submission_identity="later-rescore", value=corrected,
        links=(("test", baseline[1]["record_id"]), ("rescore_of", invalid_id)))
    with pytest.raises(Conflict, match="no complete qualified"):
        reuse(registry, initial, template(registry, original))
    assert selector.records.read(invalid_id) == original_assessment


def test_two_connections_register_one_resolved_plan_and_only_three_new_tests(registry):
    selector, initial = setup(registry)
    _, original, frozen = pair(registry, initial)
    accepted_samples(registry, original, frozen)
    raw = template(registry, original)
    policy = conditions(selector.records)
    path = selector.records.db.execute("PRAGMA database_list").fetchone()[2]
    barrier = Barrier(2)
    def submit(_):
        db = sqlite3.connect(path, timeout=10)
        try:
            records = ExperimentRecords(db, artifacts=selector.records.artifacts)
            barrier.wait()
            return BaselineReuse(records).register(initial["experiment_id"], registry[2]["record_id"], raw,
                expected_selection_id=initial["record_id"], runtime_conditions=policy,
                purpose="selection_validation", submission_identity=raw["plan_id"])
        finally:
            db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(submit, (0, 1)))
    assert a == b
    assert selector.records.db.execute("SELECT count(*) FROM lagent_records WHERE kind='test'").fetchone()[0] == 9
