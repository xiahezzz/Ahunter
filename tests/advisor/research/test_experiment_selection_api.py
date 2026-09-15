"""Public selection controls; accepted sample helpers are synthetic consumer contracts.

The production Episode assessor cannot issue those fixture acceptance claims.
"""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Barrier

import pytest

from advisor.research.experiments.records import ExperimentRecords, ProjectionUpdate
from advisor.research.experiments.registration import record_identity
from advisor.research.experiments.selection import current_selection, holdout_exposure
from tests.advisor.research.test_experiment_api import api, registry, exposures
from tests.advisor.research.test_experiment_registration import candidate, plan_input, register_plan
from tests.advisor.research.test_experiment_promotion import pair, accepted_samples
from tests.advisor.test_agent_cli import mount_api, invoke


def endpoints(api, registry):
    base = api[1] + "/" + registry[1]
    return api[0], base, base + "/selection"


def initialize(api, registry):
    service, experiment, definition, package, _ = registry
    baseline = candidate(service, experiment, package)["proposal"]
    body = {"definition_id": definition["record_id"], "baseline_id": baseline["record_id"]}
    client, _, url = endpoints(api, registry)
    response = client.post(url + "/initialize", json=body)
    assert response.status_code == 201, response.text
    return response.json()["selection"], body


def comparison_result(api, registry, *, name="a", accepted=False, invalid=None):
    initial, _ = initialize(api, registry)
    candidate(registry[0], registry[1], registry[3], name, "baseline")
    comparer, plan, frozen = pair(registry, initial, name=name)
    if accepted:
        accepted_samples(registry, plan, frozen, invalid=invalid)
    return initial, comparer.complete(frozen["record_id"]), plan


def apply_body(initial, result, identity="apply"):
    return {"comparison_id": result["record_id"], "expected_selection_id": initial["record_id"],
            "submission_identity": identity}


def test_selection_initialization_is_canonical_and_overview_is_metadata_only(api, registry):
    client, base, url = endpoints(api, registry)
    assert client.get(url).json() == {"selection": None, "execution_available": False}
    initial, body = initialize(api, registry)
    assert client.post(url + "/initialize", json=body).json()["selection"] == initial
    shown = client.get(url).json()
    assert not shown["execution_available"] and not shown["selection"]["frozen"]
    assert shown["selection"]["record_id"] == initial["record_id"]
    assert shown["selection"]["initial_baseline_id"] == body["baseline_id"]
    assert shown["selection"]["current_baseline_id"] == body["baseline_id"]
    assert set(shown["selection"]) == {"record_id", "content_hash", "sequence", "created_at", "definition_id",
        "initial_baseline_id", "current_baseline_id", "initial_package_hash", "current_package_hash", "operation",
        "formal_ready", "previous_selection_id", "frozen"}
    assert exposures(registry) == []
    other = candidate(registry[0], registry[1], registry[3], "other-root")["proposal"]
    assert client.post(url + "/initialize", json={**body, "baseline_id": other["record_id"]}).status_code == 409
    for table in ("research_work_queue", "research_requests", "ledger_transactions", "lagent_events"):
        assert registry[0].records.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_initialize_rejects_child_and_late_selection(api, registry):
    service, experiment, definition, package, _ = registry
    root = candidate(service, experiment, package)["proposal"]
    child = candidate(service, experiment, package, "child", "baseline")["proposal"]
    client, base, url = endpoints(api, registry)
    body = {"definition_id": definition["record_id"], "baseline_id": child["record_id"]}
    assert client.post(url + "/initialize", json=body).status_code == 409
    plan = register_plan(registry)
    service.records.transition(plan["tests"][0]["record_id"], "preflight", action_id="started")
    assert client.post(url + "/initialize", json={**body, "baseline_id": root["record_id"]}).status_code == 409
    assert client.get(url).json()["selection"] is None


def test_public_freeze_allows_one_exact_holdout_plan_and_ends_optimization(api, registry):
    initial, body = initialize(api, registry)
    client, base, url = endpoints(api, registry)
    freeze = {"expected_selection_id": initial["record_id"], "submission_identity": "freeze"}
    response = client.post(url + "/finalize-holdout", json=freeze)
    assert response.status_code == 200, response.text
    final = response.json()["selection"]
    assert final["value"]["operation"] == "finalize" and not final["value"]["formal_ready"]
    assert not response.json()["execution_available"]
    assert client.post(url + "/finalize-holdout", json=freeze).json() == response.json()
    assert client.post(url + "/initialize", json=body).json()["selection"] == initial
    overview = client.get(url).json()["selection"]
    assert overview["frozen"] and overview["record_id"] == final["record_id"]
    assert "exposure_at_freeze" not in overview and exposures(registry) == []
    assert client.post(base + "/records/" + final["record_id"] + "/detail", json={"audit_identity": "freeze-metadata"}).status_code == 200
    assert holdout_exposure(registry[0].records, registry[2]["record_id"])["unseen"]
    plan = {"definition_id": registry[2]["record_id"], "plan": plan_input(registry[2], task="august-holdout", plan_id="holdout"),
            "purpose": "final_holdout", "selection_id": final["record_id"], "submission_identity": "holdout"}
    result = client.post(base + "/plans", json=plan)
    assert result.status_code == 201 and len(result.json()["tests"]) == 3
    assert all(registry[0].records.status(test["record_id"]) == "created" for test in result.json()["tests"])
    assert client.post(base + "/plans", json=plan).json() == result.json()
    plan["plan"] = plan_input(registry[2], task="august-holdout", plan_id="extra")
    plan["submission_identity"] = "extra"
    assert client.post(base + "/plans", json=plan).status_code == 409
    assert client.post(url + "/finalize-holdout", json={**freeze, "submission_identity": "another-freeze"}).status_code == 409


def test_freeze_rejects_unfinished_tests_and_dated_external_exposure(api, registry):
    initial, _ = initialize(api, registry)
    client, _, url = endpoints(api, registry)
    body = {"expected_selection_id": initial["record_id"], "submission_identity": "freeze"}
    plan = register_plan(registry)
    assert client.post(url + "/finalize-holdout", json=body).status_code == 409
    for test in plan["tests"]:
        registry[0].records.transition(test["record_id"], "cancelled", action_id="stop")
    registry[0].records.put(experiment_id=registry[1], kind="exposure", record_id="known-future",
        submission_identity="known-future", value={"periods": [{"start": "2026-08-17", "end": "2026-08-21"}]})
    assert client.post(url + "/finalize-holdout", json=body).status_code == 409
    assert client.get(url).json()["selection"]["record_id"] == initial["record_id"]


def test_freeze_rejects_unknown_cleanup_even_with_terminal_test(api, registry):
    initial, _ = initialize(api, registry)
    plan = register_plan(registry)
    records = registry[0].records
    for test in plan["tests"]:
        records.transition(test["record_id"], "preflight", action_id="preflight")
        records.transition(test["record_id"], "queued", action_id="queued")
        lease = records.claim(test["record_id"], worker_id="fixture", lease_seconds=100)
        records.transition(test["record_id"], "running", action_id="running", lease=lease)
        records.commit(lease, phase_id="fixture", action_id="unknown", attempt=0, kind="fixture_unknown_cleanup",
            payload={}, simulated_at=None, updates=(ProjectionUpdate("candidate_processes", None,
                {"calls": {"call": {"quiescent": False, "status": "prepared"}}}),))
        records.transition(test["record_id"], "failed", action_id="failed", lease=lease)
    client, _, url = endpoints(api, registry)
    result = client.post(url + "/finalize-holdout", json={"expected_selection_id": initial["record_id"], "submission_identity": "freeze"})
    assert result.status_code == 409 and not client.get(url).json()["selection"]["frozen"]


@pytest.mark.parametrize("accepted,invalid,decision", [
    (False, None, "inconclusive"),
    (True, lambda sample: sample.update(formal_ready=False), "inconclusive"),
    (True, None, "promoted"),
])
def test_apply_revalidates_original_sample_qualification_and_moves_only_eligible_baseline(api, registry, accepted, invalid, decision):
    initial, result, _ = comparison_result(api, registry, accepted=accepted, invalid=invalid)
    client, _, url = endpoints(api, registry)
    assert current_selection(registry[0].records, registry[1]) == initial
    response = client.post(url + "/apply", json=apply_body(initial, result))
    assert response.status_code == 200, response.text
    recorded = response.json()["decision"]
    assert recorded["value"]["decision"] == decision and not response.json()["execution_available"]
    shown = client.get(url).json()["selection"]
    expected = record_identity(registry[1], "candidate", "a") if decision == "promoted" else initial["value"]["current_baseline_id"]
    assert shown["current_baseline_id"] == expected
    assert shown["initial_baseline_id"] == initial["value"]["initial_baseline_id"]
    assert client.post(url + "/apply", json=apply_body(initial, result)).json() == response.json()
    assert client.post(url + "/apply", json=apply_body(initial, result, "another-identity")).status_code == 409
    assert exposures(registry) == []


def test_stale_comparison_decision_does_not_overwrite_new_pointer(api, registry):
    initial, first, _ = comparison_result(api, registry, accepted=True)
    _, second, _ = comparison_result(api, registry, name="b", accepted=True)
    client, _, url = endpoints(api, registry)
    promoted = client.post(url + "/apply", json=apply_body(initial, first)).json()["decision"]
    stale = client.post(url + "/apply", json=apply_body(initial, second, "second"))
    assert stale.status_code == 200 and stale.json()["decision"]["value"]["decision"] == "stale_baseline"
    assert client.get(url).json()["selection"]["record_id"] == promoted["record_id"]
    rebound = {**apply_body(initial, second, "rebound"), "expected_selection_id": promoted["record_id"]}
    assert client.post(url + "/apply", json=rebound).status_code == 409


def test_concurrent_api_comparisons_commit_one_promotion_and_one_stale_decision(api, registry):
    initial, first, _ = comparison_result(api, registry, accepted=True)
    _, second, _ = comparison_result(api, registry, name="b", accepted=True)
    client, _, url = endpoints(api, registry)
    barrier = Barrier(2)
    def submit(result):
        barrier.wait(timeout=5)
        return client.post(url + "/apply", json=apply_body(initial, result, result["record_id"]))
    with ThreadPoolExecutor(max_workers=2) as workers:
        replies = list(workers.map(submit, (first, second)))
    assert all(reply.status_code == 200 for reply in replies)
    decisions = [reply.json()["decision"] for reply in replies]
    assert {decision["value"]["decision"] for decision in decisions} == {"promoted", "stale_baseline"}
    winner = next(decision for decision in decisions if decision["value"]["decision"] == "promoted")
    assert client.get(url).json()["selection"]["record_id"] == winner["record_id"]


def test_promoted_selection_freezes_initial_and_final_candidates_and_rejects_late_promotion(api, registry):
    initial, first, _ = comparison_result(api, registry, accepted=True)
    _, late, _ = comparison_result(api, registry, name="b", accepted=True)
    client, base, url = endpoints(api, registry)
    promoted = client.post(url + "/apply", json=apply_body(initial, first)).json()["decision"]
    body = {"expected_selection_id": promoted["record_id"], "submission_identity": "freeze"}
    response = client.post(url + "/finalize-holdout", json=body)
    assert response.status_code == 200, response.text
    final = response.json()["selection"]
    assert final["value"]["initial_baseline_id"] == initial["value"]["initial_baseline_id"]
    assert final["value"]["current_baseline_id"] == record_identity(registry[1], "candidate", "a")
    decision = client.post(url + "/apply", json=apply_body(initial, late, "late"))
    assert decision.status_code == 200 and decision.json()["decision"]["value"]["decision"] == "selection_frozen"
    assert client.get(url).json()["selection"]["record_id"] == final["record_id"]
    planned = client.post(base + "/plans", json={"definition_id": registry[2]["record_id"],
        "plan": plan_input(registry[2], names=("baseline", "a"), task="august-holdout", plan_id="holdout"),
        "purpose": "final_holdout", "selection_id": final["record_id"], "submission_identity": "holdout"})
    assert planned.status_code == 201 and len(planned.json()["tests"]) == 6, planned.text
    assert {test["value"]["sample"]["proposal_id"] for test in planned.json()["tests"]} == {"baseline", "a"}
    assert all(registry[0].records.status(test["record_id"]) == "created" for test in planned.json()["tests"])
    assert client.post(url + "/finalize-holdout", json=body).json() == response.json()
    assert registry[0].records.db.execute("SELECT COUNT(*) FROM research_work_queue").fetchone()[0] == 0


@pytest.mark.parametrize("field,value", [("formal_ready", True), ("promotion_authorized", True), ("decision", "eligible")])
def test_apply_rejects_self_consistent_record_hashes_with_forged_qualification(api, registry, field, value):
    initial, original, _ = comparison_result(api, registry)
    records = registry[0].records
    forged = records.put(experiment_id=registry[1], kind="comparison", record_id="forged-comparison",
        submission_identity="forged-comparison", value={**original["value"], field: value},
        links=tuple((link["relation"], link["target_id"]) for link in records.related(original["record_id"])))
    client, _, url = endpoints(api, registry)
    response = client.post(url + "/apply", json=apply_body(initial, forged))
    assert response.status_code == 409
    assert current_selection(records, registry[1]) == initial
    assert records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='selection'").fetchone()[0] == 1


@pytest.mark.parametrize("operation", ["initialize", "finalize-holdout"])
def test_selection_write_requires_original_candidate_bytes(api, registry, operation):
    if operation == "initialize":
        baseline = candidate(registry[0], registry[1], registry[3])["proposal"]
        body = {"definition_id": registry[2]["record_id"], "baseline_id": baseline["record_id"]}
    else:
        initial, _ = initialize(api, registry)
        body = {"expected_selection_id": initial["record_id"], "submission_identity": "freeze"}
    registry[0].artifacts._path_for(registry[3].files[0].content_hash).unlink()
    client, _, url = endpoints(api, registry)
    assert client.post(url + "/" + operation, json=body).status_code == 410
    current = client.get(url).json()["selection"]
    assert current is None if operation == "initialize" else not current["frozen"]


@pytest.mark.parametrize("scope", ["holdout", "unknown"])
def test_apply_cannot_be_a_hidden_or_unknown_scope_feedback_endpoint(api, registry, scope):
    initial, _ = initialize(api, registry)
    links = ()
    if scope == "holdout":
        client, base, url = endpoints(api, registry)
        final = client.post(url + "/finalize-holdout", json={"expected_selection_id": initial["record_id"], "submission_identity": "freeze"}).json()["selection"]
        plan = registry[0].test_plan(registry[1], registry[2]["record_id"], plan_input(registry[2], task="august-holdout"),
            purpose="final_holdout", selection_id=final["record_id"], submission_identity="holdout")
        links = (("test", plan["tests"][0]["record_id"]),)
    record = registry[0].records.put(experiment_id=registry[1], kind="comparison", record_id="private-comparison",
        submission_identity="private-comparison", value={"status": "completed", "secret": "FUTURE-RESULT"}, links=links)
    client, _, url = endpoints(api, registry)
    response = client.post(url + "/apply", json=apply_body(initial, record))
    assert response.status_code == 403 and "FUTURE-RESULT" not in response.text
    assert exposures(registry) == []


@pytest.mark.parametrize("operation", ["initialize", "apply", "finalize-holdout"])
@pytest.mark.parametrize("after_commit", [False, True])
def test_selection_write_and_lost_reply_retry_keep_one_original_record(api, registry, monkeypatch, operation, after_commit):
    client, _, url = endpoints(api, registry)
    if operation == "initialize":
        baseline = candidate(registry[0], registry[1], registry[3])["proposal"]
        body = {"definition_id": registry[2]["record_id"], "baseline_id": baseline["record_id"]}
    elif operation == "apply":
        initial, result, _ = comparison_result(api, registry, accepted=True)
        body = apply_body(initial, result)
    else:
        initial, _ = initialize(api, registry)
        body = {"expected_selection_id": initial["record_id"], "submission_identity": "freeze"}
    before = registry[0].records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='selection'").fetchone()[0]
    original = ExperimentRecords.__init__
    def initialize_records(self, *args, **kwargs):
        original(self, *args, **kwargs)
        def fail(point):
            if point == ("after_commit" if after_commit else "after_record"):
                raise sqlite3.OperationalError("selection response lost")
        self.fault = fail
    monkeypatch.setattr(ExperimentRecords, "__init__", initialize_records)
    assert client.post(url + "/" + operation, json=body).status_code == 503
    assert registry[0].records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='selection'").fetchone()[0] == before + int(after_commit)
    monkeypatch.setattr(ExperimentRecords, "__init__", original)
    first = client.post(url + "/" + operation, json=body)
    assert first.status_code in (200, 201) and client.post(url + "/" + operation, json=body).json() == first.json()


def test_selection_scope_and_local_json_boundaries_reject_without_mutation(api, registry):
    client, base, url = endpoints(api, registry)
    for operation, body in (("initialize", {"definition_id": "x", "baseline_id": "y"}),
        ("apply", {"comparison_id": "x", "expected_selection_id": "y", "submission_identity": "z"}),
        ("finalize-holdout", {"expected_selection_id": "x", "submission_identity": "z"})):
        assert client.post(url + "/" + operation, json={**body, "formal_ready": True}).status_code == 400
        assert client.post(url + "/" + operation, json=body, headers={"Origin": "https://untrusted.example"}).status_code == 400
    assert api[2] == []
    initial, body = initialize(api, registry)
    foreign = client.post(api[1], json={"config": {}, "submission_identity": "foreign"}).json()["experiment_id"]
    other = api[1] + "/" + foreign + "/selection"
    assert client.post(other + "/initialize", json=body).status_code == 404
    assert client.post(other + "/finalize-holdout", json={"expected_selection_id": initial["record_id"], "submission_identity": "foreign"}).status_code == 404
    assert client.get(api[1] + "/unknown/selection").status_code == 404


def test_cli_initialization_show_and_holdout_freeze_preserve_original_ids(api, registry, monkeypatch, capsys):
    root = candidate(registry[0], registry[1], registry[3])["proposal"]
    mount_api(monkeypatch, api[0])
    prefix = ["research", "experiments", "selection"]
    code, initialized = invoke(capsys, [*prefix, "initialize", registry[1], "--definition-id", registry[2]["record_id"], "--baseline-id", root["record_id"]])
    assert code == 0 and initialized["status"] == 201
    initial = initialized["data"]["selection"]
    code, shown = invoke(capsys, [*prefix, "show", registry[1]])
    assert code == 0 and shown["data"]["selection"]["record_id"] == initial["record_id"]
    code, frozen = invoke(capsys, [*prefix, "finalize-holdout", registry[1], "--expected-selection-id", initial["record_id"], "--submission-identity", "freeze"])
    assert code == 0 and frozen["data"]["selection"]["value"]["operation"] == "finalize"
    assert not frozen["data"]["execution_available"]


def test_cli_apply_returns_inconclusive_business_decision_without_advancing_baseline(api, registry, monkeypatch, capsys):
    initial, result, _ = comparison_result(api, registry)
    mount_api(monkeypatch, api[0])
    code, outcome = invoke(capsys, ["research", "experiments", "selection", "apply", registry[1],
        "--comparison-id", result["record_id"], "--expected-selection-id", initial["record_id"], "--submission-identity", "apply"])
    assert code == 0 and outcome["status"] == 200 and outcome["data"]["decision"]["value"]["decision"] == "inconclusive"
    assert current_selection(registry[0].records, registry[1]) == initial
