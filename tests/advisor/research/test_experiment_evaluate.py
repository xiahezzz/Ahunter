from copy import deepcopy
from decimal import Decimal, localcontext
from fractions import Fraction

import pytest

from advisor.research.experiments.contracts import original_case
from advisor.research.experiments.evaluate import ComparisonAssessments, EpisodeAssessments, compare_returns, terminal_return
from advisor.research.experiments.queries import ExperimentQueries, QueryDenied
from advisor.research.experiments.records import ProjectionUpdate
from advisor.research.experiments.repository import Conflict
from advisor.research.experiments.resolution import resolve
from tests.advisor.research.test_experiment_contracts import calendar
from tests.advisor.research.test_experiment_registration import registry, candidate, plan_input
from tests.advisor.research.test_experiment_bundles import bundle
from tests.advisor.research.test_experiment_episode import episode, program, run


def comparison(*, differences=("0.001", "0.001", "0.001"), role="selection_validation", raw=None):
    raw = deepcopy(raw or original_case())
    sealed = resolve(raw, calendar=calendar).specification
    tests, outcomes = [], {}
    for task in raw["tasks"]:
        if task["role"] != role:
            continue
        for index, difference in enumerate(differences):
            for name in ("baseline", "candidate"):
                identity = f"{task['task_id']}-{name}-{index}"
                tests.append({"test_id": identity, "proposal_id": name, "task_id": task["task_id"],
                              "repeat_id": f"repeat-{index}", "repeat_index": index, "seed": None})
                outcomes[identity] = {"status": "completed", "valid": True, "initial_nav": "100000",
                    "terminal_nav": str(Decimal("100000") * (1 + (Decimal(difference) if name == "candidate" else 0)))}
    plan = {"plan_id": "comparison", "specification_hash": sealed.specification_hash,
            "tests": tests, "seed_support": "unsupported"}
    return sealed, plan, outcomes


def score(fixture):
    return compare_returns(*fixture, baseline="baseline", candidate="candidate")


@pytest.mark.parametrize("differences,decision", [
    (("0.001", "0.001", "0.001"), "eligible"),
    (("0.0065", "0.0065", "-0.01"), "eligible"),
    (("0.0065", "0.0066", "-0.0100001"), "unstable"),
    (("0.02", "0", "0"), "unstable"),
    (("0.000999999999999999",) * 3, "not_improved"),
    (("0",) * 3, "not_improved"),
    (("-0.02",) * 3, "not_improved"),
])
def test_exact_thresholds_ties_positive_pairs_and_legal_low_returns(differences, decision):
    fixture = comparison(differences=differences)
    # Ambient decimal precision must not change arithmetic or a threshold equality.
    with localcontext() as context:
        context.prec = 3
        result = score(fixture)
    assert result["decision"] == decision
    assert not result["statistical_significance_claimed"] and not result["annualized"]
    assert result["seed_support"] == "unsupported"


def test_configured_repeats_fraction_and_zero_threshold_have_no_hidden_defaults():
    raw = original_case()
    raw["evaluate"].update(repeats=4, minimum_improvement="0", positive_repeat_fraction={"numerator": 3, "denominator": 4})
    assert score(comparison(raw=raw, differences=("0.01", "0.01", "0", "0")))["decision"] == "unstable"
    assert score(comparison(raw=raw, differences=("0.01", "0.01", "0.01", "0")))["decision"] == "eligible"
    assert score(comparison(raw=raw, differences=("0",) * 4))["decision"] == "not_improved"


def test_task_weights_apply_before_paired_stability_and_role_means():
    raw = original_case()
    selection = raw["tasks"][1]
    selection["weight"] = "1"
    raw["tasks"].append({**deepcopy(selection), "task_id": "second-selection", "weight": "3"})
    fixture = comparison(raw=raw, differences=("0.004", "0.004", "0.004"))
    for identity, outcome in fixture[2].items():
        if identity.startswith("second-selection-candidate"):
            outcome["terminal_nav"] = "100000"
    result = score(fixture)
    assert result["decision"] == "eligible"
    assert result["summary"]["mean_improvement"] == "0.001"
    assert result["summary"]["paired_differences_exact"] == [{"numerator": "1", "denominator": "1000"}] * 3


@pytest.mark.parametrize("failure", ["missing", "blocked", "failed", "cancelled", "running", "invalid"])
def test_one_required_bad_sample_prevents_all_aggregate_scores(failure):
    fixture = comparison()
    identity = next(iter(fixture[2]))
    if failure == "missing":
        del fixture[2][identity]
    elif failure == "invalid":
        fixture[2][identity]["valid"] = False
    else:
        fixture[2][identity]["status"] = failure
    result = score(fixture)
    assert result["decision"] == "inconclusive" and result["summary"] is None and not result["groups"]
    assert result["invalid_samples"][0]["test_id"] == identity


@pytest.mark.parametrize("role,decision", [("tuning", "tuning_only"), ("final_holdout", "holdout_only")])
def test_tuning_and_holdout_winners_never_select(role, decision):
    assert score(comparison(role=role))["decision"] == decision


def test_mixed_lengths_group_by_default_and_require_frozen_opt_in():
    raw = original_case()
    other = deepcopy(raw["tasks"][1])
    other.update(task_id="short-selection", period={"mode": "trading_days", "start": "2026-08-10", "trading_days": 3})
    raw["tasks"].append(other)
    result = score(comparison(raw=raw))
    assert result["decision"] == "inconclusive" and result["summary"] is None
    assert [g["trading_day_counts"] for g in result["groups"]] == [[3], [5]]
    raw["evaluate"]["allow_mixed_durations"] = True
    result = score(comparison(raw=raw))
    assert result["decision"] == "eligible" and result["groups"][0]["trading_day_counts"] == [3, 5]
    assert not result["annualized"]


def test_sample_addition_deletion_reordering_and_wrong_opponent_are_rejected():
    sealed, plan, outcomes = comparison()
    with pytest.raises(Conflict, match="unplanned"):
        score((sealed, plan, {**outcomes, "lucky-rerun": next(iter(outcomes.values()))}))
    with pytest.raises(Conflict, match="matrix"):
        score((sealed, {**plan, "tests": plan["tests"][:-1]}, outcomes))
    with pytest.raises(Conflict, match="interleave"):
        score((sealed, {**plan, "tests": sorted(plan["tests"], key=lambda t: t["proposal_id"])}, outcomes))
    with pytest.raises(Conflict, match="baseline"):
        compare_returns(sealed, plan, outcomes, baseline="parent-only", candidate="candidate")


@pytest.mark.parametrize("initial,terminal", [("0", "1"), ("NaN", "1"), ("1", "Infinity"), (True, "1"), (1.0, "1")])
def test_invalid_numeric_evidence_is_not_coerced_into_a_score(initial, terminal):
    with pytest.raises(ValueError):
        terminal_return(initial, terminal)


def finished(episode):
    replay, fixture = episode
    replay.create(program(episode))
    assert run(replay, fixture)[0]["kind"] == "ready_for_evaluation"
    replay.records.transition(replay.lease.test_id, "evaluating", action_id="evaluate", lease=replay.lease)
    return replay, EpisodeAssessments(replay.records)


def test_actual_episode_assessment_uses_committed_nav_and_preserves_formal_gates(episode):
    replay, assessor = finished(episode)
    prior = replay._state()
    result = assessor.assess(replay.lease.test_id, submission_identity="first")
    value = result["value"]
    assert not value["valid"] and not value["formal_ready"]
    assert {"fixture_market_evidence", "formal_replay_acceptance_unavailable", "resource_accounting_incomplete", "costs_missing"} <= set(value["reasons"])
    assert value["nav_diagnostic"]["initial_nav"] == "200000"
    assert Decimal(value["nav_diagnostic"]["net_return"]) < 0
    assert not value["nav_diagnostic"]["formal_score"]
    assert replay._state() == prior and replay.records.status(replay.lease.test_id) == "evaluating"
    assert assessor.assess(replay.lease.test_id, submission_identity="first") == result
    with pytest.raises(Conflict, match="link"):
        assessor.assess(replay.lease.test_id, submission_identity="unlinked-new")
    second = assessor.assess(replay.lease.test_id, submission_identity="second", rescore_of=result["record_id"])
    assert second["value"]["nav_diagnostic"] == value["nav_diagnostic"]
    assert {"relation": "rescore_of", "target_id": result["record_id"]} in replay.records.related(second["record_id"])


def test_cancelled_assessment_cannot_become_zero_return_sample(episode):
    replay, _ = episode
    replay.records.transition(replay.lease.test_id, "cancelled", action_id="cancel", lease=replay.lease)
    result = EpisodeAssessments(replay.records).assess(replay.lease.test_id, submission_identity="cancelled")
    assert not result["value"]["valid"] and result["value"]["nav_diagnostic"] is None
    assert "test_cancelled" in result["value"]["reasons"]


@pytest.mark.parametrize("point", ["before_commit", "after_commit"])
def test_assessment_commit_recovery_keeps_one_original_snapshot(episode, point):
    replay, assessor = finished(episode)
    def fault(actual):
        if actual == point:
            raise OSError("assessment interrupted")
    replay.records.fault = fault
    with pytest.raises(OSError, match="interrupted"):
        assessor.assess(replay.lease.test_id, submission_identity="retry")
    replay.records.fault = lambda _: None
    result = assessor.assess(replay.lease.test_id, submission_identity="retry")
    assert assessor.assess(replay.lease.test_id, submission_identity="retry") == result
    assert replay.records.db.execute("SELECT count(*) FROM lagent_records WHERE kind='evaluation'").fetchone()[0] == 1


def test_unknown_child_cleanup_and_missing_daily_nav_remain_invalid(episode):
    replay, assessor = finished(episode)
    current = replay._state()
    changed = deepcopy(current["value"])
    changed["valuations"].pop("daily:2026-08-04")
    replay.records.commit(replay.lease, phase_id="fixture-fault", action_id="unknown-cleanup", attempt=0,
        kind="fixture_fault", payload={}, simulated_at=None,
        updates=(ProjectionUpdate("episode", current["sequence"], changed),
                 ProjectionUpdate("candidate_processes", None, {"calls": {"lost": {"quiescent": False, "status": "running"}}})))
    value = assessor.assess(replay.lease.test_id, submission_identity="fault")["value"]
    assert {"physical_cleanup_unknown", "nav_coverage_incomplete"} <= set(value["reasons"])
    assert value["nav_diagnostic"] is None


def test_terminal_return_retains_exact_rational_value():
    assert terminal_return("3", "4") == Fraction(1, 3)
    assert terminal_return("100", "-10") == Fraction(-11, 10)


def registered_comparison(registry, plan_id="selection"):
    service, experiment, definition, package, _ = registry
    for name in ("baseline", "candidate"):
        candidate(service, experiment, package, name)
    plan = service.test_plan(experiment, definition["record_id"],
        plan_input(definition, names=("baseline", "candidate"), task="august-selection", plan_id=plan_id),
        submission_identity=plan_id, purpose="selection_validation")
    return ComparisonAssessments(service.records), plan


def test_frozen_comparison_missing_samples_are_permanent_and_null_feedback_is_audited(registry):
    comparisons, plan = registered_comparison(registry)
    records = comparisons.records
    frozen = comparisons.freeze(plan["plan"]["record_id"], baseline="baseline", candidate="candidate", submission_identity="pair")
    first = comparisons.complete(frozen["record_id"])
    assert first["value"]["decision"] == "inconclusive"
    assert all(s["assessment_id"] is None for s in first["value"]["samples"])
    assert not first["value"]["promotion_authorized"]
    test_id = plan["tests"][0]["record_id"]
    records.transition(test_id, "cancelled", action_id="cancel")
    EpisodeAssessments(records).assess(test_id, submission_identity="after-result")
    assert comparisons.complete(frozen["record_id"]) == first
    assert comparisons.freeze(plan["plan"]["record_id"], baseline="baseline", candidate="candidate", submission_identity="pair") == frozen
    feedback = ExperimentQueries(records, viewer="optimizer").comparison_feedback(first["record_id"])
    assert feedback["candidate_mean_return"] is None and feedback["positive_repeats"] is None
    assert feedback["planned_repeats"] == 3 and feedback["decision"] == "inconclusive"
    assert "samples" not in feedback
    with pytest.raises(QueryDenied):
        ExperimentQueries(records, viewer="optimizer").detail(first["record_id"])
    with pytest.raises(Conflict, match="before execution"):
        comparisons.freeze(plan["plan"]["record_id"], baseline="baseline", candidate="candidate", submission_identity="late")


def test_comparison_uses_first_assessment_identity_and_new_plan_retains_original_failure(registry):
    comparisons, plan = registered_comparison(registry)
    records = comparisons.records
    frozen = comparisons.freeze(plan["plan"]["record_id"], baseline="baseline", candidate="candidate", submission_identity="pair")
    test_id = plan["tests"][0]["record_id"]
    records.transition(test_id, "cancelled", action_id="cancel")
    assessor = EpisodeAssessments(records)
    first = assessor.assess(test_id, submission_identity="first")
    assessor.assess(test_id, submission_identity="later", rescore_of=first["record_id"])
    result = comparisons.complete(frozen["record_id"])
    assert result["value"]["samples"][0]["assessment_id"] == first["record_id"]
    with pytest.raises(Conflict, match="new plan"):
        comparisons.freeze(plan["plan"]["record_id"], baseline="baseline", candidate="candidate",
                           submission_identity="same-plan", previous_comparison=result["record_id"])
    _, new_plan = registered_comparison(registry, "repaired-selection")
    repaired = comparisons.freeze(new_plan["plan"]["record_id"], baseline="baseline", candidate="candidate",
                                  submission_identity="new-pair", previous_comparison=result["record_id"])
    assert {"relation": "comparison", "target_id": result["record_id"]} in records.related(repaired["record_id"])
    assert set(repaired["value"]["test_ids"]).isdisjoint(frozen["value"]["test_ids"])


@pytest.mark.parametrize("point", ["before_commit", "after_commit"])
def test_comparison_result_atomic_retry_does_not_resample(registry, point):
    comparisons, plan = registered_comparison(registry)
    frozen = comparisons.freeze(plan["plan"]["record_id"], baseline="baseline", candidate="candidate", submission_identity="pair")
    def fault(actual):
        if point == actual:
            raise OSError("comparison interrupted")
    comparisons.records.fault = fault
    with pytest.raises(OSError):
        comparisons.complete(frozen["record_id"])
    comparisons.records.fault = lambda _: None
    result = comparisons.complete(frozen["record_id"])
    assert comparisons.complete(frozen["record_id"]) == result
    assert comparisons.records.db.execute("SELECT count(*) FROM lagent_records WHERE kind='comparison'").fetchone()[0] == 2


def test_unknown_stability_policy_is_not_silently_interpreted():
    raw = original_case()
    raw["evaluate"]["stability_policy_ref"] = "unimplemented-v2"
    with pytest.raises(Conflict, match="unsupported"):
        score(comparison(raw=raw))


def test_uncommitted_higher_nav_is_not_selected_and_cross_test_nav_is_rejected(episode):
    replay, assessor = finished(episode)
    records = replay.records
    state = replay._state()
    terminal_key = "terminal:" + replay.account.task.trading_dates[-1].isoformat()
    original = records.read(state["value"]["valuations"][terminal_key])
    higher = records.put(experiment_id=original["experiment_id"], kind="valuation", record_id="uncommitted-nav",
        submission_identity="uncommitted-nav", value={**original["value"], "nav": "999999"},
        links=(("test", replay.lease.test_id),))
    result = assessor.assess(replay.lease.test_id, submission_identity="original")
    assert result["value"]["nav_diagnostic"]["terminal_id"] == original["record_id"]
    assert {"relation": "source", "target_id": higher["record_id"]} not in records.related(result["record_id"])
    other = records.db.execute("SELECT record_id FROM lagent_records WHERE kind='test' AND record_id != ? LIMIT 1",
                               (replay.lease.test_id,)).fetchone()[0]
    foreign = records.put(experiment_id=original["experiment_id"], kind="valuation", record_id="other-test-nav",
        submission_identity="other-test-nav", value=original["value"], links=(("test", other),))
    changed = deepcopy(state["value"])
    changed["valuations"][terminal_key] = foreign["record_id"]
    records.commit(replay.lease, phase_id="fixture-fault", action_id="foreign-nav", attempt=0, kind="fixture_fault",
        payload={}, simulated_at=None, updates=(ProjectionUpdate("episode", state["sequence"], changed),))
    with pytest.raises(Conflict, match="valuation binding"):
        assessor.assess(replay.lease.test_id, submission_identity="foreign", rescore_of=result["record_id"])
