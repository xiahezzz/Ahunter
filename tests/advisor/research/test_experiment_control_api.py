import json
import sqlite3

import pytest

from advisor.research.experiments.records import ExperimentRecords
from advisor.research.experiments.selection import ExperimentSelection, holdout_exposure
from advisor.research.experiments.guardian import _publish
from advisor.research.experiments.resolution import digest
from advisor.research.work_queue import read_work
from tests.advisor.research.test_experiment_api import api, registry
from tests.advisor.research.test_experiment_registration import register_plan, candidate, plan_input
from tests.advisor.research.test_shared_experiment_queue import queued, runtime, limits, bundle, PRELUDE, TRADE
from tests.advisor.test_agent_cli import mount_api, invoke


def url(api, registry, test_id, action):
    return api[1] + "/" + registry[1] + "/tests/" + test_id + "/" + action


def cancel_body(identity="cancel", reason="owner stopped this test"):
    return {"submission_identity": identity, "reason": reason}


@pytest.mark.parametrize("state", ["created", "preflight", "queued"])
def test_unstarted_cancel_is_atomic_terminal_and_does_not_make_queue_work(api, registry, state):
    records = registry[0].records
    test = register_plan(registry)["tests"][0]
    test_id = test["record_id"]
    if state != "created": records.transition(test_id, "preflight", action_id="preflight")
    if state == "queued": records.transition(test_id, "queued", action_id="queued")
    response = api[0].post(url(api, registry, test_id, "cancel"), json=cancel_body())
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["status"] == "cancelled" and value["terminal"] and not value["cancel_pending"]
    assert value["queue_state"] is None and not value["execution_available"]
    assert records.read(test_id) == test
    assert records.db.execute("SELECT COUNT(*) FROM research_work_queue").fetchone()[0] == 0
    assert records.db.execute("SELECT COUNT(*) FROM ledger_transactions").fetchone()[0] == 0
    assert api[0].post(url(api, registry, test_id, "cancel"), json=cancel_body()).json() == value
    assert api[0].post(url(api, registry, test_id, "cancel"), json=cancel_body(reason="changed")).status_code == 409
    events = records.events(test_id, after=0, limit=100)["items"]
    cancelled = [event for event in events if event["kind"] == "owner_cancellation"]
    assert len(cancelled) == 1 and cancelled[0]["value"]["payload"]["request_id"] == value["request"]["record_id"]


def test_api_cancel_queued_work_waits_for_original_service_and_keeps_request_identity(api, registry, queued):
    q = queued
    endpoint = url(api, registry, q.test_id, "cancel")
    response = api[0].post(endpoint, json=cancel_body())
    assert response.status_code == 202, response.text
    value = response.json()
    assert value["status"] == "queued" and value["cancel_pending"] and not value["terminal"]
    assert read_work(q.records.db, q.test_id).cancel_requested
    assert q.records.projection(q.test_id, "candidate_processes") is None
    assert api[0].post(endpoint, json=cancel_body()).json() == value
    finished = q.owner.tick()
    assert finished.source_status == "cancelled" and finished.state == "finished"
    final = api[0].post(endpoint, json=cancel_body())
    assert final.status_code == 200 and final.json()["terminal"] and not final.json()["cancel_pending"]
    assert final.json()["request"] == value["request"]
    assert q.builders == [] and q.records.projection(q.test_id, "account") is None


@pytest.mark.parametrize("registry", [PRELUDE + "call('observe',{},'observe')"], indirect=True)
def test_running_cancellation_does_not_claim_terminal_while_physical_cleanup_is_unknown(api, registry, queued, monkeypatch):
    q = queued
    def uncertain(*args, process_handle, **kwargs):
        directory, request = q.runner._load(process_handle)
        _publish(directory, "child-intent.json", {"request_hash": digest(request)})
        raise RuntimeError("unknown launch")
    monkeypatch.setattr(q.runner, "run", uncertain)
    assert q.owner.tick().state == "running"
    response = api[0].post(url(api, registry, q.test_id, "cancel"), json=cancel_body())
    assert response.status_code == 202
    assert response.json()["status"] == "running" and response.json()["cancel_pending"]
    assert q.owner.tick().source_status == "running"
    calls = q.records.projection(q.test_id, "candidate_processes")["value"]["calls"]
    assert len(calls) == 1 and not next(iter(calls.values()))["quiescent"]


@pytest.mark.parametrize("point", ["after_record", "after_event", "after_projection", "before_commit", "after_commit"])
def test_cancel_receipt_and_terminal_event_recover_together_after_fault(api, registry, monkeypatch, point):
    records = registry[0].records
    test_id = register_plan(registry)["tests"][0]["record_id"]
    original = ExperimentRecords.__init__
    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        def fail(actual):
            if actual == point: raise sqlite3.OperationalError("cancel reply lost")
        self.fault = fail
    monkeypatch.setattr(ExperimentRecords, "__init__", initialize)
    endpoint = url(api, registry, test_id, "cancel")
    assert api[0].post(endpoint, json=cancel_body()).status_code == 503
    committed = point == "after_commit"
    assert records.status(test_id) == ("cancelled" if committed else "created")
    assert records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='service_control'").fetchone()[0] == int(committed)
    monkeypatch.setattr(ExperimentRecords, "__init__", original)
    result = api[0].post(endpoint, json=cancel_body())
    assert result.status_code == 200 and api[0].post(endpoint, json=cancel_body()).json() == result.json()


def test_shared_cancel_receipt_failure_rolls_back_queue_intent(api, registry, queued, monkeypatch):
    original = ExperimentRecords._insert
    def fail(self, prepared):
        if prepared["kind"] == "service_control": raise sqlite3.OperationalError("control storage unavailable")
        return original(self, prepared)
    monkeypatch.setattr(ExperimentRecords, "_insert", fail)
    assert api[0].post(url(api, registry, queued.test_id, "cancel"), json=cancel_body()).status_code == 503
    assert not read_work(queued.records.db, queued.test_id).cancel_requested
    assert queued.records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='service_control'").fetchone()[0] == 0


def test_missing_shared_index_does_not_allow_direct_terminal_cancellation(api, registry, queued):
    q = queued
    q.records.db.execute("DELETE FROM research_work_queue WHERE work_id=?", (q.test_id,))
    q.records.db.commit()
    response = api[0].post(url(api, registry, q.test_id, "cancel"), json=cancel_body())
    assert response.status_code == 409
    assert q.records.status(q.test_id) == "queued"
    assert q.records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='service_control'").fetchone()[0] == 0


@pytest.mark.parametrize("terminal", ["completed", "failed", "blocked"])
def test_cancel_after_terminal_records_request_without_rewriting_original_outcome(api, registry, terminal):
    records = registry[0].records
    test_id = register_plan(registry)["tests"][0]["record_id"]
    records.transition(test_id, "preflight", action_id="preflight")
    if terminal == "completed":
        records.transition(test_id, "queued", action_id="queued")
        lease = records.claim(test_id, worker_id="fixture", lease_seconds=30)
        for state in ("running", "evaluating", "completed"):
            records.transition(test_id, state, action_id=state, lease=lease)
    else:
        records.transition(test_id, terminal, action_id=terminal)
    events = records.events(test_id, after=0, limit=100)
    response = api[0].post(url(api, registry, test_id, "cancel"), json=cancel_body())
    assert response.status_code == 200 and response.json()["status"] == terminal
    assert response.json()["terminal"] and not response.json()["cancel_pending"]
    assert records.events(test_id, after=0, limit=100) == events


def test_rerun_requires_terminal_preserves_samples_and_returns_new_created_record(api, registry):
    records = registry[0].records
    test = register_plan(registry)["tests"][0]
    test_id = test["record_id"]
    endpoint = url(api, registry, test_id, "rerun")
    body = cancel_body("rerun", "inspect a separate repetition")
    assert api[0].post(endpoint, json=body).status_code == 409
    assert api[0].post(url(api, registry, test_id, "cancel"), json=cancel_body()).status_code == 200
    response = api[0].post(endpoint, json=body)
    assert response.status_code == 201, response.text
    value = response.json()
    assert value["status"] == "created" and not value["execution_available"]
    rerun = value["test"]
    assert rerun["record_id"] != test_id and rerun["value"]["rerun_of"] == test_id
    assert not rerun["value"]["eligible_for_original_comparison"]
    assert rerun["value"]["sample"] == test["value"]["sample"]
    assert {item["relation"] for item in records.related(rerun["record_id"])} >= {"definition", "plan", "candidate", "rerun_of"}
    assert records.status(test_id) == "cancelled" and records.read(test_id) == test
    assert api[0].post(endpoint, json=body).json() == value
    assert api[0].post(endpoint, json=cancel_body("rerun", "changed reason")).status_code == 409
    assert records.db.execute("SELECT COUNT(*) FROM research_work_queue").fetchone()[0] == 0
    assert records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='test'").fetchone()[0] == 4


@pytest.mark.parametrize("contamination", ["field", "artifact", "link"])
def test_freeze_allows_cancellation_and_metadata_audit_but_disallows_holdout_reruns(api, registry, contamination):
    service, experiment, definition, package, _ = registry
    baseline = candidate(service, experiment, package)
    selector = ExperimentSelection(service.records)
    initial = selector.initialize(experiment, definition["record_id"], baseline["proposal"]["record_id"])
    frozen = selector.finalize(experiment, expected_selection_id=initial["record_id"], submission_identity="freeze")
    task = next(task["task_id"] for task in json.loads(definition["value"]["specification"]["effective_json"])["tasks"] if task["role"] == "final_holdout")
    plan = service.test_plan(experiment, definition["record_id"], plan_input(definition, task=task),
        purpose="final_holdout", selection_id=frozen["record_id"], submission_identity="holdout")
    test_id = plan["tests"][0]["record_id"]
    cancelled = api[0].post(url(api, registry, test_id, "cancel"), json=cancel_body())
    assert cancelled.status_code == 200
    assert api[0].post(url(api, registry, test_id, "rerun"), json=cancel_body("rerun")).status_code == 409
    receipt = cancelled.json()["request"]
    base = api[1] + "/" + experiment + "/records/"
    shown = api[0].post(base + receipt["record_id"] + "/detail", json={"audit_identity": "cancel-receipt"})
    assert shown.status_code == 200 and shown.json() == receipt
    exposure = holdout_exposure(service.records, definition["record_id"])
    assert exposure["unseen"] and exposure["metadata_read_ids"]
    value, links, artifacts = dict(receipt["value"]), [("test", test_id)], ()
    if contamination == "field": value["future_result"] = "fixture future observation"
    elif contamination == "artifact": artifacts = (service.artifacts.put_bytes(b"fixture future observation").content_hash,)
    else: links.append(("plan", plan["plan"]["record_id"]))
    contaminated = service.records.put(experiment_id=experiment, kind="service_control", record_id="contaminated-control",
        submission_identity="contaminated", value=value, links=links, artifact_hashes=artifacts)
    shown = api[0].post(base + contaminated["record_id"] + "/detail", json={"audit_identity": "contaminated-control"})
    assert shown.status_code == 200
    assert not holdout_exposure(service.records, definition["record_id"])["unseen"]


def test_existing_rerun_retry_survives_freeze_but_new_reruns_are_rejected(api, registry):
    service, experiment, definition, *_ = registry
    tests = register_plan(registry)["tests"]
    test_id = tests[0]["record_id"]
    baseline_id = next(link["target_id"] for link in service.records.related(test_id) if link["relation"] == "candidate")
    selector = ExperimentSelection(service.records)
    initial = selector.initialize(experiment, definition["record_id"], baseline_id)
    for test in tests:
        assert api[0].post(url(api, registry, test["record_id"], "cancel"), json=cancel_body(test["record_id"])).status_code == 200
    endpoint = url(api, registry, test_id, "rerun")
    first = api[0].post(endpoint, json=cancel_body("rerun"))
    assert first.status_code == 201
    repeated_id = first.json()["test"]["record_id"]
    assert api[0].post(url(api, registry, repeated_id, "cancel"), json=cancel_body("stop-rerun")).status_code == 200
    selector.finalize(experiment, expected_selection_id=initial["record_id"], submission_identity="freeze")
    retry = api[0].post(endpoint, json=cancel_body("rerun"))
    assert retry.status_code == 201 and retry.json()["test"] == first.json()["test"]
    assert retry.json()["status"] == "cancelled"
    assert api[0].post(endpoint, json=cancel_body("new-rerun")).status_code == 409


def test_control_ids_are_experiment_scoped_and_cannot_rebind_to_another_test(api, registry):
    tests = register_plan(registry)["tests"]
    client, base, _ = api
    foreign = client.post(base, json={"config": {}, "submission_identity": "foreign"}).json()["experiment_id"]
    for action in ("cancel", "rerun"):
        endpoint = base + "/" + foreign + "/tests/" + tests[0]["record_id"] + "/" + action
        assert client.post(endpoint, json=cancel_body()).status_code == 404
    assert client.post(url(api, registry, tests[0]["record_id"], "cancel"), json=cancel_body()).status_code == 200
    assert client.post(url(api, registry, tests[1]["record_id"], "cancel"), json=cancel_body()).status_code == 409
    assert registry[0].records.status(tests[1]["record_id"]) == "created"


@pytest.mark.parametrize("after_commit", [False, True])
def test_rerun_retry_recovers_original_link_after_storage_or_reply_loss(api, registry, monkeypatch, after_commit):
    records = registry[0].records
    test_id = register_plan(registry)["tests"][0]["record_id"]
    api[0].post(url(api, registry, test_id, "cancel"), json=cancel_body()).raise_for_status()
    original = ExperimentRecords.__init__
    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        def fail(point):
            if point == ("after_commit" if after_commit else "after_record"):
                raise sqlite3.OperationalError("rerun reply lost")
        self.fault = fail
    monkeypatch.setattr(ExperimentRecords, "__init__", initialize)
    endpoint = url(api, registry, test_id, "rerun")
    assert api[0].post(endpoint, json=cancel_body("rerun")).status_code == 503
    assert records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='test'").fetchone()[0] == 3 + int(after_commit)
    monkeypatch.setattr(ExperimentRecords, "__init__", original)
    first = api[0].post(endpoint, json=cancel_body("rerun"))
    assert first.status_code == 201 and api[0].post(endpoint, json=cancel_body("rerun")).json() == first.json()


@pytest.mark.parametrize("action", ["cancel", "rerun"])
def test_control_boundaries_and_required_reason_reject_before_storage(api, registry, action):
    client, _, calls = api
    endpoint = url(api, registry, "unknown", action)
    for body in ({}, cancel_body(reason=" "), cancel_body(reason=123), {**cancel_body(), "lease": "caller"}):
        assert client.post(endpoint, json=body).status_code == 400
    assert client.post(endpoint, json=cancel_body(), headers={"Origin": "https://untrusted.example"}).status_code == 400
    assert calls == []
    assert client.post(endpoint, json=cancel_body()).status_code == 404


def test_cli_cancel_and_rerun_report_current_business_status(api, registry, monkeypatch, capsys):
    mount_api(monkeypatch, api[0])
    test_id = register_plan(registry)["tests"][0]["record_id"]
    prefix = ["research", "experiments", "tests"]
    code, stopped = invoke(capsys, [*prefix, "cancel", registry[1], test_id, "--submission-identity", "cancel", "--reason", "owner stopped"])
    assert code == 0 and stopped["status"] == 200 and stopped["data"]["status"] == "cancelled"
    code, rerun = invoke(capsys, [*prefix, "rerun", registry[1], test_id, "--submission-identity", "rerun", "--reason", "separate investigation"])
    assert code == 0 and rerun["status"] == 201 and rerun["data"]["status"] == "created"
    assert rerun["data"]["test"]["value"]["rerun_of"] == test_id
    assert not rerun["data"]["execution_available"]


def test_cli_202_cancel_is_pending_until_service_finishes(api, registry, queued, monkeypatch, capsys):
    mount_api(monkeypatch, api[0])
    command = ["research", "experiments", "tests", "cancel", registry[1], queued.test_id,
               "--submission-identity", "pending", "--reason", "owner cancellation"]
    code, requested = invoke(capsys, command)
    assert code == 0 and requested["status"] == 202 and requested["data"]["cancel_pending"]
    queued.owner.tick()
    code, finished = invoke(capsys, command)
    assert code == 0 and finished["status"] == 200 and finished["data"]["status"] == "cancelled"
    assert finished["data"]["request"] == requested["data"]["request"]


@pytest.mark.parametrize("registry", [TRADE], indirect=True)
def test_evaluation_wait_cancellation_returns_to_service_without_replaying_episode(api, registry, queued):
    q = queued
    work = q.owner.tick()
    assert work.state == "waiting" and work.source_status == "evaluating"
    attempts = q.records.projection(q.test_id, "candidate_processes")
    account = q.records.projection(q.test_id, "account")
    response = api[0].post(url(api, registry, q.test_id, "cancel"), json=cancel_body())
    assert response.status_code == 202 and response.json()["queue_state"] == "queued"
    assert response.json()["status"] == "evaluating"
    assert q.owner.tick().source_status == "cancelled"
    assert q.records.projection(q.test_id, "candidate_processes") == attempts
    assert q.records.projection(q.test_id, "account") == account
    assert q.records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='evaluation'").fetchone()[0] == 0
