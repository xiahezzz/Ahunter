from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Barrier

import pytest

from advisor.research.experiments.contracts import original_case
from advisor.research.experiments.queries import ExperimentQueries, QueryDenied
from advisor.research.experiments.records import ExperimentRecords, ProjectionUpdate
from advisor.research.experiments.registration import ExperimentRegistry
from advisor.research.experiments.repository import Conflict, ExperimentStore
from advisor.research.experiments.resolution import resolve
from advisor.research.experiments.selection import ExperimentSelection, current_selection, holdout_exposure
from tests.advisor.research.test_experiment_contracts import calendar
from tests.advisor.research.test_experiment_registration import registry, candidate, plan_input, register_plan


def initialize(registry):
    service, experiment, definition, package, _ = registry
    baseline = candidate(service, experiment, package)["proposal"]
    selector = ExperimentSelection(service.records)
    initial = selector.initialize(experiment, definition["record_id"], baseline["record_id"])
    return selector, initial


def freeze(registry):
    selector, initial = initialize(registry)
    final = selector.finalize(initial["experiment_id"], expected_selection_id=initial["record_id"], submission_identity="final")
    return selector, initial, final


def holdout(registry, final, *, plan_id="holdout", names=("baseline",), definition=None):
    service, experiment, original, _, _ = registry
    definition = definition or original
    raw = plan_input(definition, names=names, task="august-holdout", plan_id=plan_id)
    return service.test_plan(experiment, definition["record_id"], raw, submission_identity=plan_id,
                             purpose="final_holdout", selection_id=final["record_id"])


def test_freeze_and_one_holdout_plan_bind_exact_initial_and_current_candidate(registry):
    selector, initial, final = freeze(registry)
    service, experiment, definition, package, _ = registry
    assert current_selection(service.records, experiment) == final
    assert final["value"]["current_baseline_id"] == initial["value"]["initial_baseline_id"]
    assert final["value"]["exposure_at_freeze"]["unseen"] and not final["value"]["formal_ready"]
    result = holdout(registry, final)
    assert len(result["tests"]) == 3  # Unchanged initial/final candidate is not run twice.
    assert all(t["value"]["role"] == "final_holdout" and t["value"]["selection_id"] == final["record_id"] for t in result["tests"])
    assert holdout(registry, final) == result
    with pytest.raises(Conflict, match="single"):
        holdout(registry, final, plan_id="renamed-retry")
    assert selector.initialize(experiment, definition["record_id"], initial["value"]["initial_baseline_id"]) == initial
    assert current_selection(service.records, experiment) == final
    assert selector.finalize(experiment, expected_selection_id=initial["record_id"], submission_identity="final") == final
    with pytest.raises(Conflict, match="revision"):
        selector.finalize(experiment, expected_selection_id=initial["record_id"], submission_identity="another")


def test_holdout_is_rejected_before_freeze_and_optimization_stops_after_freeze(registry):
    selector, initial = initialize(registry)
    service, experiment, definition, package, _ = registry
    candidate(service, experiment, package, "branch", "baseline")
    with pytest.raises(Conflict, match="finalized"):
        holdout(registry, initial)
    final = selector.finalize(experiment, expected_selection_id=initial["record_id"], submission_identity="final")
    with pytest.raises(Conflict, match="selected candidate"):
        holdout(registry, final, names=("baseline", "branch"))
    with pytest.raises(Conflict, match="frozen"):
        candidate(service, experiment, package, "new-branch", "baseline")
    assert candidate(service, experiment, package, "branch", "baseline")["proposal"]["value"]["proposal"]["proposal_id"] == "branch"
    with pytest.raises(Conflict, match="frozen"):
        register_plan(registry)
    result = holdout(registry, final)
    test_id = result["tests"][0]["record_id"]
    service.records.transition(test_id, "cancelled", action_id="cancel")
    with pytest.raises(Conflict, match="unplanned"):
        service.rerun(experiment, test_id, rerun_identity="extra", reason="want another score")


def test_selection_cannot_initialize_late_or_freeze_unfinished_plans(registry):
    selector, initial = initialize(registry)
    plan = register_plan(registry)
    records = selector.records
    with pytest.raises(Conflict, match="unfinished"):
        selector.finalize(initial["experiment_id"], expected_selection_id=initial["record_id"], submission_identity="final")
    for test in plan["tests"]:
        records.transition(test["record_id"], "cancelled", action_id="cancel")
    final = selector.finalize(initial["experiment_id"], expected_selection_id=initial["record_id"], submission_identity="final")
    assert final["value"]["operation"] == "finalize"
    with pytest.raises(Conflict, match="frozen"):
        registry[0].rerun(initial["experiment_id"], plan["tests"][0]["record_id"], rerun_identity="after-final", reason="again")


def test_initial_baseline_requires_root_and_cannot_change(registry):
    selector, initial = initialize(registry)
    service, experiment, definition, package, _ = registry
    branch = candidate(service, experiment, package, "branch", "baseline")["proposal"]
    root = candidate(service, experiment, package, "another-root")["proposal"]
    with pytest.raises(Conflict, match="root"):
        selector.initialize(experiment, definition["record_id"], branch["record_id"])
    with pytest.raises(Conflict, match="different"):
        selector.initialize(experiment, definition["record_id"], root["record_id"])
    assert current_selection(service.records, experiment) == initial


@pytest.mark.parametrize("point", ["before_commit", "after_commit"])
def test_selection_freeze_atomic_retry_keeps_original_revision(registry, point):
    selector, initial = initialize(registry)
    def fault(actual):
        if actual == point:
            raise OSError("freeze interrupted")
    selector.records.fault = fault
    with pytest.raises(OSError):
        selector.finalize(initial["experiment_id"], expected_selection_id=initial["record_id"], submission_identity="final")
    selector.records.fault = lambda _: None
    final = selector.finalize(initial["experiment_id"], expected_selection_id=initial["record_id"], submission_identity="final")
    assert current_selection(selector.records, initial["experiment_id"]) == final
    assert selector.records.db.execute("SELECT count(*) FROM lagent_records WHERE kind='selection'").fetchone()[0] == 2


@pytest.mark.parametrize("point", ["after_record", "before_commit", "after_commit"])
def test_holdout_plan_and_samples_commit_together(registry, point):
    selector, _, final = freeze(registry)
    count = 0
    def fault(actual):
        nonlocal count
        if actual == "after_record":
            count += 1
        if actual == point and (point != "after_record" or count == 3):
            raise OSError("plan interrupted")
    selector.records.fault = fault
    with pytest.raises(OSError):
        holdout(registry, final)
    selector.records.fault = lambda _: None
    total = selector.records.db.execute("SELECT count(*) FROM lagent_records WHERE kind IN ('test_plan','test')").fetchone()[0]
    assert total == (4 if point == "after_commit" else 0)
    result = holdout(registry, final)
    assert holdout(registry, final) == result


def test_dated_external_exposure_survives_experiment_and_task_rename(registry):
    service, experiment, definition, package, _ = registry
    exposure = ExperimentQueries(service.records, viewer="owner").declare_exposure(experiment, submission_identity="seen",
        periods=[{"start": "2026-08-18", "end": "2026-08-18"}], note="Viewed prior holdout outcome")
    new_experiment = ExperimentStore(service.records.db).create(original_case(), "renamed-experiment")["experiment_id"]
    raw = original_case()
    raw["tasks"][2]["task_id"] = "unseen-renamed"
    renamed = service.definition(new_experiment, resolve(raw, calendar=calendar).specification, submission_identity="renamed")
    baseline = candidate(service, new_experiment, package)["proposal"]
    selector = ExperimentSelection(service.records)
    initial = selector.initialize(new_experiment, renamed["record_id"], baseline["record_id"])
    report = holdout_exposure(service.records, renamed["record_id"])
    assert report["exposures"][0]["record_id"] == exposure["record_id"]
    assert report["exposures"][0]["dates"] == ["2026-08-18"] and not report["unseen"]
    with pytest.raises(Conflict, match="exposed"):
        selector.finalize(new_experiment, expected_selection_id=initial["record_id"], submission_identity="final")


def test_metadata_read_is_audited_but_does_not_consume_holdout(registry):
    selector, initial, final = freeze(registry)
    owner = ExperimentQueries(selector.records, viewer="owner")
    assert owner.detail(final["record_id"], audit_identity="inspect-freeze") == final
    report = holdout_exposure(selector.records, registry[2]["record_id"])
    assert report["unseen"] and len(report["metadata_read_ids"]) == 1
    plan = holdout(registry, final)
    owner.detail(plan["plan"]["record_id"], audit_identity="inspect-plan")
    report = holdout_exposure(selector.records, registry[2]["record_id"])
    assert report["unseen"] and len(report["metadata_read_ids"]) == 2
    with pytest.raises(QueryDenied):
        ExperimentQueries(selector.records, viewer="optimizer").detail(final["record_id"])


def test_holdout_result_exposure_remains_after_freeze_without_erasing_original_plan(registry):
    selector, _, final = freeze(registry)
    plan = holdout(registry, final)
    test_id = plan["tests"][0]["record_id"]
    records = selector.records
    result = records.put(experiment_id=final["experiment_id"], kind="evaluation", record_id="holdout-result",
                         submission_identity="holdout-result", value={"diagnostic": "fixture"}, links=(("test", test_id),))
    ExperimentQueries(records, viewer="owner").detail(result["record_id"], audit_identity="inspect-result")
    assert not holdout_exposure(records, registry[2]["record_id"])["unseen"]
    assert holdout(registry, final) == plan
    assert records.read(final["record_id"])["value"]["exposure_at_freeze"]["unseen"]
    with pytest.raises(QueryDenied):
        ExperimentQueries(records, viewer="optimizer").detail(result["record_id"])


def test_exposure_between_freeze_and_plan_prevents_admission(registry):
    selector, _, final = freeze(registry)
    ExperimentQueries(selector.records, viewer="owner").declare_exposure(final["experiment_id"], submission_identity="race",
        periods=[{"start": "2026-08-17", "end": "2026-08-21"}], note="New feedback after freeze")
    with pytest.raises(Conflict, match="exposure changed"):
        holdout(registry, final)
    assert selector.records.db.execute("SELECT count(*) FROM lagent_records WHERE kind='test_plan'").fetchone()[0] == 0


def test_role_overlap_is_not_a_fresh_holdout(registry):
    service, experiment, _, package, _ = registry
    raw = original_case()
    raw["tasks"][2].update(period=deepcopy(raw["tasks"][1]["period"]), research_start_date=raw["tasks"][1]["research_start_date"])
    definition = service.definition(experiment, resolve(raw, calendar=calendar).specification, submission_identity="overlap")
    assert holdout_exposure(service.records, definition["record_id"])["overlapping_roles"] == [f"2026-08-{d}" for d in range(10,15)]


def test_two_connections_cannot_finalize_two_selections(registry):
    selector, initial = initialize(registry)
    path = selector.records.db.execute("PRAGMA database_list").fetchone()[2]
    barrier = Barrier(2)
    def finalize(identity):
        db = sqlite3.connect(path, timeout=10)
        try:
            records = ExperimentRecords(db, artifacts=selector.records.artifacts)
            barrier.wait()
            try:
                result = ExperimentSelection(records).finalize(initial["experiment_id"],
                    expected_selection_id=initial["record_id"], submission_identity=identity)
                return result["record_id"]
            except Conflict:
                return None
        finally:
            db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finalize, ("a", "b")))
    assert sum(value is not None for value in results) == 1
    assert selector.records.db.execute("SELECT count(*) FROM lagent_records WHERE kind='selection'").fetchone()[0] == 2


def test_initial_baseline_cannot_be_chosen_after_tests_start(registry):
    plan = register_plan(registry)
    service, experiment, definition, package, _ = registry
    service.records.transition(plan["tests"][0]["record_id"], "preflight", action_id="preflight")
    baseline = candidate(service, experiment, package)["proposal"]
    with pytest.raises(Conflict, match="before any Test"):
        ExperimentSelection(service.records).initialize(experiment, definition["record_id"], baseline["record_id"])


def test_previous_tuning_run_cannot_be_relabeled_holdout_in_another_experiment(registry):
    service, experiment, original, package, _ = registry
    raw = original_case()
    task = raw["tasks"][2]
    task.update(role="tuning", task_id="old-tuning")
    raw["tasks"] = [task]
    old = service.definition(experiment, resolve(raw, calendar=calendar).specification, submission_identity="old")
    candidate(service, experiment, package)
    plan = service.test_plan(experiment, old["record_id"], plan_input(old, task="old-tuning", plan_id="old"),
                             submission_identity="old", purpose="tuning")
    test_id = plan["tests"][0]["record_id"]
    for status in ("preflight", "queued"):
        service.records.transition(test_id, status, action_id=status)
    lease = service.records.claim(test_id, worker_id="old", lease_seconds=100)
    service.records.transition(test_id, "running", action_id="running", lease=lease)
    service.records.transition(test_id, "failed", action_id="failed", lease=lease)
    other = ExperimentStore(service.records.db).create(original_case(), "new")["experiment_id"]
    new = service.definition(other, original["value"]["specification"], submission_identity="new")
    report = holdout_exposure(service.records, new["record_id"])
    assert not report["unseen"] and not report["overlapping_roles"] and not report["exposures"]
    assert report["previous_training_tests"][0]["test_id"] == test_id


def test_prior_exposure_of_other_dates_does_not_consume_new_window(registry):
    service, experiment, definition, _, _ = registry
    ExperimentQueries(service.records, viewer="owner").declare_exposure(experiment, submission_identity="old-window",
        periods=[{"start": "2026-08-03", "end": "2026-08-14"}], note="Known tuning and validation dates")
    _, _, final = freeze(registry)
    assert final["value"]["exposure_at_freeze"]["unseen"]
    assert holdout(registry, final)["tests"]


def test_unknown_cleanup_in_terminal_test_prevents_selection_freeze(registry):
    selector, initial = initialize(registry)
    plan = register_plan(registry)
    records = selector.records
    test_id = plan["tests"][0]["record_id"]
    for status in ("preflight", "queued"):
        records.transition(test_id, status, action_id=status)
    lease = records.claim(test_id, worker_id="fixture", lease_seconds=100)
    records.transition(test_id, "running", action_id="running", lease=lease)
    records.commit(lease, phase_id="fixture", action_id="unknown", attempt=0, kind="fixture_unknown", payload={}, simulated_at=None,
        updates=(ProjectionUpdate("candidate_processes", None, {"calls": {"unknown": {"status": "running", "quiescent": False}}}),))
    records.transition(test_id, "failed", action_id="failed", lease=lease)
    for test in plan["tests"][1:]:
        records.transition(test["record_id"], "cancelled", action_id="cancel")
    with pytest.raises(Conflict, match="physical cleanup"):
        selector.finalize(initial["experiment_id"], expected_selection_id=initial["record_id"], submission_identity="final")


def test_holdout_does_not_accept_new_definition_after_selection_freeze(registry):
    _, _, final = freeze(registry)
    service, experiment, _, _, _ = registry
    raw = original_case()
    raw["account"]["initial_cash"] = "300000"
    altered = service.definition(experiment, resolve(raw, calendar=calendar).specification, submission_identity="new-account")
    with pytest.raises(Conflict, match="exact finalized"):
        holdout(registry, final, definition=altered)


def test_unknown_exposure_cannot_be_erased_to_claim_unseen(registry):
    service, experiment, definition, _, _ = registry
    unknown = service.records.put(experiment_id=experiment, kind="exposure", record_id="unknown-exposure",
        submission_identity="unknown", value={"scopes": [{"role": "unknown", "trading_dates": []}]})
    report = holdout_exposure(service.records, definition["record_id"])
    assert not report["unseen"] and report["unresolved_exposures"][0]["record_id"] == unknown["record_id"]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        service.records.db.execute("UPDATE lagent_records SET value_json='{}' WHERE record_id=?", (unknown["record_id"],))
    service.records.db.rollback()
    assert holdout_exposure(service.records, definition["record_id"])["unresolved_exposures"] == report["unresolved_exposures"]
