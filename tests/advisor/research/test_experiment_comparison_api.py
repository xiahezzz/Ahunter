"""Comparison ingress and immutable results; accepted fixtures test consumers only."""
import base64
import hashlib
import json
import sqlite3

import pytest

from advisor.research.experiments.evaluate import ComparisonAssessments
from advisor.research.experiments.records import ExperimentRecords
from advisor.research.experiments.selection import current_selection
from tests.advisor.research.test_experiment_api import api, registry, exposures
from tests.advisor.research.test_experiment_registration import candidate, plan_input
from tests.advisor.research.test_experiment_selection_api import initialize
from tests.advisor.research.test_experiment_promotion import conditions, accepted_samples
from tests.advisor.test_agent_cli import mount_api, invoke


def prepared(api, registry, *, runtime=True, plan_id="selection", previous=None):
    initial, _ = initialize(api, registry)
    service, experiment, definition, package, _ = registry
    candidate(service, experiment, package, "a", "baseline")
    plan = service.test_plan(experiment, definition["record_id"],
        plan_input(definition, names=("baseline", "a"), task="august-selection", plan_id=plan_id),
        submission_identity=plan_id, purpose="selection_validation")
    supplied = conditions(service.records) if runtime else None
    refs = {value for item in (supplied or {}).values() for key, value in item.items() if key.endswith("_hash")}
    originals = {ref: base64.b64encode(service.artifacts.read_bytes(ref)).decode("ascii") for ref in refs}
    body = {"plan_id": plan["plan"]["record_id"], "baseline": "baseline", "candidate": "a",
            "submission_identity": plan_id, "previous_comparison": previous, "expected_selection_id": initial["record_id"],
            "runtime_conditions": supplied, "runtime_artifacts_base64": originals}
    return api[0], api[1] + "/" + experiment + "/comparisons", body, plan, initial


def freeze(client, url, body):
    response = client.post(url, json=body)
    assert response.status_code == 201, response.text
    assert not response.json()["execution_available"]
    return response.json()["comparison"]


def stop_samples(registry, plan):
    for test in plan["tests"]:
        registry[0].records.transition(test["record_id"], "cancelled", action_id="stop")


def test_public_freeze_uploads_exact_condition_bytes_and_binds_original_samples(api, registry):
    client, url, body, plan, initial = prepared(api, registry)
    original = b"new declared budget context\x00\xff\r\n"
    content_hash = hashlib.sha256(original).hexdigest()
    prior_hash = body["runtime_conditions"]["august-selection"]["budget_hash"]
    body["runtime_conditions"]["august-selection"]["budget_hash"] = content_hash
    body["runtime_artifacts_base64"].pop(prior_hash)
    body["runtime_artifacts_base64"][content_hash] = base64.b64encode(original).decode("ascii")
    assert not registry[0].artifacts._path_for(content_hash).exists()
    frozen = freeze(client, url, body)
    assert registry[0].artifacts.read_bytes(content_hash) == original
    assert frozen["value"]["runtime_conditions"]["tasks"] == body["runtime_conditions"]
    assert frozen["value"]["test_ids"] == [test["record_id"] for test in plan["tests"]]
    assert frozen["value"]["selection_id"] == initial["record_id"]
    assert frozen["value"]["runtime_fingerprint_complete"]
    assert freeze(client, url, body) == frozen
    assert current_selection(registry[0].records, registry[1]) == initial
    assert exposures(registry) == []
    for table in ("research_work_queue", "research_requests", "lagent_events", "ledger_transactions"):
        assert registry[0].records.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("damage", ["missing", "extra", "wrong_bytes", "base64", "conditions_null", "formal_flag"])
def test_hashes_alone_or_malformed_upload_cannot_create_new_artifact_links(api, registry, damage):
    client, url, body, _, _ = prepared(api, registry)
    key = next(iter(body["runtime_artifacts_base64"]))
    if damage == "missing": body["runtime_artifacts_base64"].pop(key)
    elif damage == "extra": body["runtime_artifacts_base64"]["0" * 64] = ""
    elif damage == "wrong_bytes": body["runtime_artifacts_base64"][key] = "YWJj"
    elif damage == "base64": body["runtime_artifacts_base64"][key] = "not base64!"
    elif damage == "conditions_null": body["runtime_conditions"] = None
    else: body["runtime_conditions"]["august-selection"]["formal_ready"] = True
    before = set(registry[0].artifacts.root.glob("*/*"))
    response = client.post(url, json=body)
    assert response.status_code == 400, response.text
    assert set(registry[0].artifacts.root.glob("*/*")) == before
    assert registry[0].records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='comparison'").fetchone()[0] == 0


def test_public_complete_waits_for_all_terminal_samples_and_preserves_inconclusive_result(api, registry):
    client, url, body, plan, initial = prepared(api, registry, runtime=False)
    frozen = freeze(client, url, body)
    endpoint = url + "/" + frozen["record_id"] + "/complete"
    assert client.post(endpoint, json={}).status_code == 409
    assert registry[0].records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='comparison'").fetchone()[0] == 1
    stop_samples(registry, plan)
    response = client.post(endpoint, json={})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["comparison"]["status"] == "completed" and not result["comparison"]["formal_ready"]
    assert result["feedback"]["decision"] == "inconclusive" and result["feedback"]["mean_improvement"] is None
    assert "samples" not in response.text and "invalid_samples" not in response.text
    assert client.post(endpoint, json={}).json() == result
    record = registry[0].records.read(result["comparison"]["record_id"])
    assert all(sample["assessment_id"] is None for sample in record["value"]["samples"])
    assert current_selection(registry[0].records, registry[1]) == initial
    assert exposures(registry) == []


def test_public_retry_preserves_an_existing_internal_inconclusive_snapshot(api, registry):
    client, url, body, _, _ = prepared(api, registry, runtime=False)
    frozen = freeze(client, url, body)
    prior = ComparisonAssessments(registry[0].records).complete(frozen["record_id"])
    response = client.post(url + "/" + frozen["record_id"] + "/complete", json={})
    assert response.status_code == 200
    assert response.json()["comparison"]["record_id"] == prior["record_id"]
    assert response.json()["feedback"]["decision"] == "inconclusive"
    assert registry[0].records.read(prior["record_id"]) == prior


def test_qualified_fixture_result_returns_only_feedback_and_requires_explicit_selection_commit(api, registry):
    client, url, body, plan, initial = prepared(api, registry)
    frozen = freeze(client, url, body)
    accepted_samples(registry, plan, frozen)
    response = client.post(url + "/" + frozen["record_id"] + "/complete", json={})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["feedback"]["decision"] == "eligible" and result["comparison"]["promotion_authorized"]
    assert not result["execution_available"] and "samples" not in result["comparison"]
    assert current_selection(registry[0].records, registry[1]) == initial
    assert exposures(registry) == []
    actual = registry[0].records.read(result["comparison"]["record_id"])
    assert len(actual["value"]["samples"]) == 6
    base = api[1] + "/" + registry[1]
    detail = client.post(base + "/records/" + actual["record_id"] + "/detail", json={"audit_identity": "result-detail"})
    assert detail.status_code == 200 and detail.json() == actual and exposures(registry)
    selected = client.post(base + "/selection/apply", json={"comparison_id": actual["record_id"],
        "expected_selection_id": initial["record_id"], "submission_identity": "select"})
    assert selected.status_code == 200 and selected.json()["decision"]["value"]["decision"] == "promoted"


def test_complete_retries_original_result_and_does_not_pick_later_assessments(api, registry):
    client, url, body, plan, _ = prepared(api, registry)
    frozen = freeze(client, url, body)
    accepted_samples(registry, plan, frozen, invalid=lambda value: value.update(formal_ready=False))
    endpoint = url + "/" + frozen["record_id"] + "/complete"
    first = client.post(endpoint, json={}).json()
    assert first["feedback"]["decision"] == "inconclusive"
    records = registry[0].records
    original = records.read(first["comparison"]["record_id"])
    for sample in original["value"]["samples"]:
        assessment = records.read(sample["assessment_id"])
        records.put(experiment_id=registry[1], kind="evaluation", record_id="later-" + sample["test_id"],
            submission_identity="later-" + sample["test_id"], value={**assessment["value"], "formal_ready": True},
            links=(("test", sample["test_id"]),))
    assert client.post(endpoint, json={}).json() == first
    assert records.read(original["record_id"]) == original


def test_repaired_comparison_requires_new_plan_and_links_prior_inconclusive_result(api, registry):
    client, url, body, plan, _ = prepared(api, registry, runtime=False)
    frozen = freeze(client, url, body)
    stop_samples(registry, plan)
    result = client.post(url + "/" + frozen["record_id"] + "/complete", json={}).json()["comparison"]
    changed = {**body, "submission_identity": "same-plan-repair", "previous_comparison": result["record_id"]}
    assert client.post(url, json=changed).status_code == 409
    _, _, repaired, _, _ = prepared(api, registry, runtime=False, plan_id="repair", previous=result["record_id"])
    registered = freeze(client, url, repaired)
    assert registered["value"]["previous_comparison"] == result["record_id"]
    assert {"relation": "comparison", "target_id": result["record_id"]} in registry[0].records.related(registered["record_id"])


@pytest.mark.parametrize("change", ["late", "wrong_task", "changed_identity", "old_selection"])
def test_comparison_freeze_keeps_original_plan_timing_conditions_and_selection(api, registry, change):
    client, url, body, plan, initial = prepared(api, registry)
    if change == "late": registry[0].records.transition(plan["tests"][0]["record_id"], "preflight", action_id="started")
    elif change == "wrong_task":
        body["runtime_conditions"] = {"wrong-task": body["runtime_conditions"]["august-selection"]}
    elif change == "changed_identity":
        freeze(client, url, body)
        body["runtime_conditions"]["august-selection"]["actual_model_id"] = "changed-model"
    else:
        # Freeze with no planned Tests remaining active, then try a fresh pair registration.
        stop_samples(registry, plan)
        base = api[1] + "/" + registry[1]
        final = client.post(base + "/selection/finalize-holdout", json={"expected_selection_id": initial["record_id"], "submission_identity": "freeze"})
        assert final.status_code == 200
    assert client.post(url, json=body).status_code == 409


@pytest.mark.parametrize("operation", ["freeze", "complete"])
@pytest.mark.parametrize("after_commit", [False, True])
def test_comparison_write_fault_and_lost_reply_preserve_one_original_record(api, registry, monkeypatch, operation, after_commit):
    client, url, body, plan, _ = prepared(api, registry)
    if operation == "complete":
        frozen = freeze(client, url, body)
        stop_samples(registry, plan)
        url, body = url + "/" + frozen["record_id"] + "/complete", {}
    before = registry[0].records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='comparison'").fetchone()[0]
    original = ExperimentRecords.__init__
    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        def fail(point):
            if point == ("after_commit" if after_commit else "after_record"): raise sqlite3.OperationalError("comparison reply lost")
        self.fault = fail
    monkeypatch.setattr(ExperimentRecords, "__init__", init)
    assert client.post(url, json=body).status_code == 503
    assert registry[0].records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='comparison'").fetchone()[0] == before + int(after_commit)
    monkeypatch.setattr(ExperimentRecords, "__init__", original)
    first = client.post(url, json=body)
    assert first.status_code in (200, 201) and client.post(url, json=body).json() == first.json()


@pytest.mark.parametrize("scope", ["unknown", "holdout"])
def test_complete_rejects_hidden_scope_and_never_accepts_uploaded_outcomes(api, registry, scope):
    client, url, body, plan, initial = prepared(api, registry)
    frozen = freeze(client, url, body)
    links = ()
    if scope == "holdout":
        stop_samples(registry, plan)
        final = client.post(api[1] + "/" + registry[1] + "/selection/finalize-holdout", json={
            "expected_selection_id": initial["record_id"], "submission_identity": "freeze"}).json()["selection"]
        hidden = registry[0].test_plan(registry[1], registry[2]["record_id"], plan_input(registry[2], task="august-holdout"),
            purpose="final_holdout", selection_id=final["record_id"], submission_identity="holdout")
        links = (("test", hidden["tests"][0]["record_id"]),)
    unknown = registry[0].records.put(experiment_id=registry[1], kind="comparison", record_id="unknown",
        submission_identity="unknown", value={"record_type": "plan", "secret": "FUTURE-NAV"}, links=links)
    assert client.post(url + "/" + unknown["record_id"] + "/complete", json={}).status_code == 403
    assert client.post(url, json={**body, "previous_comparison": unknown["record_id"], "submission_identity": "repair-hidden"}).status_code == 403
    assert client.post(url + "/" + frozen["record_id"] + "/complete", json={"samples": [], "formal_ready": True}).status_code == 400
    assert exposures(registry) == []


def test_comparison_scope_and_local_json_validation_reject_before_input_writes(api, registry):
    client, url, body, _, _ = prepared(api, registry)
    for endpoint, request in ((url, body), (url + "/unknown/complete", {})):
        assert client.post(endpoint, json=request, headers={"Origin": "https://untrusted.example"}).status_code == 400
    foreign = client.post(api[1], json={"config": {}, "submission_identity": "foreign"}).json()["experiment_id"]
    assert client.post(api[1] + "/" + foreign + "/comparisons", json=body).status_code == 404
    frozen = freeze(client, url, body)
    assert client.post(api[1] + "/" + foreign + "/comparisons/" + frozen["record_id"] + "/complete", json={}).status_code == 404


def test_cli_freeze_and_complete_keep_canonical_result_identity_and_business_status(api, registry, monkeypatch, capsys, tmp_path):
    client, url, body, plan, _ = prepared(api, registry, runtime=False)
    mount_api(monkeypatch, client)
    path = tmp_path / "comparison.json"
    path.write_text(json.dumps(body))
    prefix = ["research", "experiments", "comparisons"]
    code, frozen = invoke(capsys, [*prefix, "freeze", registry[1], "--json", str(path)])
    assert code == 0 and frozen["status"] == 201
    record_id = frozen["data"]["comparison"]["record_id"]
    command = [*prefix, "complete", registry[1], record_id]
    code, unfinished = invoke(capsys, command)
    assert code == 4 and unfinished["status"] == 409
    stop_samples(registry, plan)
    code, result = invoke(capsys, command)
    assert code == 0 and result["status"] == 200 and result["data"]["feedback"]["decision"] == "inconclusive"
    assert invoke(capsys, command)[1] == result
    assert "samples" not in result["data"]["comparison"]
