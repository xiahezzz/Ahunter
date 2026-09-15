"""Public calibration contracts use invented prices and metered fixture Episodes."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import shutil
import sqlite3
from threading import Event

import pytest
from fastapi.testclient import TestClient

from advisor.research.experiments.records import ExperimentRecords
from advisor.research.experiments.calibration import Calibrations
from advisor.web.api import create_app, load_advisor_config, resolve_research_artifact_dir
from tests.advisor.research.test_experiment_api import api, registry, exposures
from tests.advisor.research.test_experiment_calibration import calibration, complete_test
from tests.advisor.research.test_experiment_registration import candidate, plan_input
from tests.advisor.research.test_experiment_selection_api import initialize
from tests.advisor.test_agent_cli import mount_api, invoke
from tests.advisor.test_research_web import _research_workspace


def prepared(api, registry, calibration):
    initial, _ = initialize(api, registry)
    service, plan, table, envelopes, _, _ = calibration
    refs = {table.source_hash, *(t.usage_semantics_hash for t in table.tariffs), *(e.replay_evidence_hash for e in envelopes)}
    body = {"plan_id": plan["plan"]["record_id"], "expected_selection_id": initial["record_id"],
            "table": table.model_dump(mode="json"), "envelopes": [e.model_dump(mode="json") for e in envelopes],
            "artifacts_base64": {h: base64.b64encode(service.artifacts.read_bytes(h)).decode("ascii") for h in refs}}
    return api[0], api[1] + "/" + registry[1] + "/calibrations", body


def freeze(client, url, body, registry):
    response = client.post(url, json=body)
    assert response.status_code == 201, response.text
    result = response.json()
    assert not result["execution_available"] and not result["calibration"]["formal_ready"]
    assert set(result["calibration"]) == {"record_id", "content_hash", "sequence", "created_at", "cost_record_type", "formal_ready"}
    return registry[0].records.read(result["calibration"]["record_id"])


def count(registry, subtype):
    return registry[0].records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE json_extract(value_json,'$.cost_record_type')=?", (subtype,)).fetchone()[0]


def test_public_campaign_is_canonical_pins_initial_baseline_and_does_not_execute(api, registry, calibration):
    client, url, body = prepared(api, registry, calibration)
    first = freeze(client, url, body, registry)
    assert freeze(client, url, body, registry) == first
    assert first["value"]["selection_id"] == body["expected_selection_id"]
    assert first["value"]["test_ids"] == [t["record_id"] for t in calibration[1]["tests"]]
    assert first["value"]["table"] == body["table"]
    assert exposures(registry) == []
    for table in ("research_work_queue", "research_requests", "lagent_events", "ledger_transactions"):
        assert registry[0].records.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("damage", ["missing", "extra", "wrong_bytes", "base64", "not_map", "envelopes_object", "formal_flag"])
def test_malformed_upload_cannot_seal_bytes_or_grant_links_by_existing_hash(api, registry, calibration, damage):
    client, url, body = prepared(api, registry, calibration)
    key = next(iter(body["artifacts_base64"]))
    if damage == "missing": body["artifacts_base64"].pop(key)
    elif damage == "extra": body["artifacts_base64"]["0" * 64] = ""
    elif damage == "wrong_bytes": body["artifacts_base64"][key] = "YWJj"
    elif damage == "base64": body["artifacts_base64"][key] = "%%%"
    elif damage == "not_map": body["artifacts_base64"] = []
    elif damage == "envelopes_object": body["envelopes"] = {}
    else: body["table"]["formal_ready"] = True
    before = set(registry[0].artifacts.root.glob("*/*"))
    assert client.post(url, json=body).status_code == 400
    assert set(registry[0].artifacts.root.glob("*/*")) == before
    assert count(registry, "calibration_campaign") == 0


def test_fresh_original_evidence_is_uploaded_and_verified_against_declarations(api, registry, calibration):
    client, url, body = prepared(api, registry, calibration)
    # A newly supplied JSON spelling is preserved byte-for-byte, not reserialized.
    original = json.dumps({k: v for k, v in body["envelopes"][0].items() if k != "replay_evidence_hash"}, indent=2).encode() + b"\r\n"
    key = hashlib.sha256(original).hexdigest()
    body["artifacts_base64"].pop(body["envelopes"][0]["replay_evidence_hash"])
    body["envelopes"][0]["replay_evidence_hash"] = key
    body["artifacts_base64"][key] = base64.b64encode(original).decode("ascii")
    assert not registry[0].artifacts._path_for(key).exists()
    freeze(client, url, body, registry)
    assert registry[0].artifacts.read_bytes(key) == original


@pytest.mark.parametrize("damage", ["missing_task", "wrong_scale", "other_root", "late_start", "missing_package"])
def test_campaign_rejects_incomplete_conditions_other_baseline_and_late_freeze(api, registry, calibration, damage):
    client, url, body = prepared(api, registry, calibration)
    if damage == "missing_task":
        removed = body["envelopes"].pop()
        body["artifacts_base64"].pop(removed["replay_evidence_hash"])
    elif damage == "wrong_scale": body["envelopes"][0]["environment_units"]["cpu"]["seconds"] = "3"
    elif damage == "other_root":
        candidate(registry[0], registry[1], registry[3], "other")
        plan = registry[0].test_plan(registry[1], registry[2]["record_id"], plan_input(registry[2], names=("other",), plan_id="other"),
                                    submission_identity="other", purpose="calibration")
        body["plan_id"] = plan["plan"]["record_id"]
    elif damage == "late_start": registry[0].records.transition(calibration[1]["tests"][0]["record_id"], "preflight", action_id="early")
    else: registry[0].artifacts._path_for(registry[3].files[0].content_hash).unlink()
    assert client.post(url, json=body).status_code == (410 if damage == "missing_package" else 409)
    assert count(registry, "calibration_campaign") == 0


@pytest.mark.parametrize("frozen_first", [False, True])
def test_final_selection_freeze_rejects_new_campaign_but_preserves_original_retry(api, registry, calibration, frozen_first):
    client, url, body = prepared(api, registry, calibration)
    prior = freeze(client, url, body, registry) if frozen_first else None
    for test in calibration[1]["tests"]:
        registry[0].records.transition(test["record_id"], "cancelled", action_id="cancel")
    final = client.post(api[1] + "/" + registry[1] + "/selection/finalize-holdout", json={
        "expected_selection_id": body["expected_selection_id"], "submission_identity": "final"})
    assert final.status_code == 200, final.text
    response = client.post(url, json=body)
    assert response.status_code == (201 if frozen_first else 409)
    if prior: assert response.json()["calibration"]["record_id"] == prior["record_id"]


@pytest.mark.parametrize("cause,reason", [("incomplete", "calibration_incomplete"), ("unknown", "calibration_usage_unsettled"),
    ("missing_phase", "calibration_research_cost_coverage_incomplete"), ("overrun", "calibration_costs_invalid")])
def test_public_complete_retains_blocked_reason_and_never_selects_cheaper_repeats(api, registry, calibration, cause, reason):
    client, url, body = prepared(api, registry, calibration)
    campaign = freeze(client, url, body, registry)
    complete_test(calibration, 0, campaign, amount=1)
    complete_test(calibration, 1, campaign, amount=2)
    if cause != "incomplete":
        complete_test(calibration, 2, campaign, amount=3, unknown=cause == "unknown", missing_phase=cause == "missing_phase", overrun=cause == "overrun")
    response = client.post(url + "/" + campaign["record_id"] + "/complete", json={})
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {"status": "blocked", "reason": reason, "execution_available": False}
    assert count(registry, "calibration_result") == 0 and exposures(registry) == []


def test_complete_derives_original_maximum_once_and_keeps_details_behind_audit(api, registry, calibration):
    client, url, body = prepared(api, registry, calibration)
    campaign = freeze(client, url, body, registry)
    for index, amount in enumerate((1, 3, 2)): complete_test(calibration, index, campaign, amount=amount)
    response = client.post(url + "/" + campaign["record_id"] + "/complete", json={})
    assert response.status_code == 200, response.text
    first = response.json()
    assert first["calibration"]["cost_record_type"] == "calibration_result" and not first["calibration"]["formal_ready"]
    assert not first["execution_available"]
    assert all(key not in response.text for key in ("measurements", "allocations", "maximum_research_cost", "envelopes"))
    assert client.post(url + "/" + campaign["record_id"] + "/complete", json={}).json() == first
    assert freeze(client, url, body, registry) == campaign
    assert exposures(registry) == []
    record_id = first["calibration"]["record_id"]
    detail = client.post(api[1] + "/" + registry[1] + "/records/" + record_id + "/detail", json={"audit_identity": "read-costs"})
    assert detail.status_code == 200, detail.text
    value = detail.json()["value"]
    assert value["maximum_research_cost"] == "4.8" and len(value["measurements"]) == 3
    assert all(item["total_limit"] == "7" for item in value["allocations"].values())
    assert exposures(registry)


@pytest.mark.parametrize("operation", ["freeze", "complete"])
@pytest.mark.parametrize("after_commit", [False, True])
def test_storage_faults_recover_one_canonical_record_without_partial_result(api, registry, calibration, monkeypatch, operation, after_commit):
    client, url, body = prepared(api, registry, calibration)
    subtype = "calibration_campaign"
    if operation == "complete":
        campaign = freeze(client, url, body, registry)
        for index in range(3): complete_test(calibration, index, campaign)
        url, body = url + "/" + campaign["record_id"] + "/complete", {}
        subtype = "calibration_result"
    original = ExperimentRecords.__init__
    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        def fault(point):
            if point == ("after_commit" if after_commit else "after_record"): raise sqlite3.OperationalError("fixture lost write")
        self.fault = fault
    monkeypatch.setattr(ExperimentRecords, "__init__", init)
    assert client.post(url, json=body).status_code == 503
    assert count(registry, subtype) == int(after_commit)
    monkeypatch.setattr(ExperimentRecords, "__init__", original)
    response = client.post(url, json=body)
    assert response.status_code in (200, 201) and client.post(url, json=body).json() == response.json()
    assert count(registry, subtype) == 1


def test_scope_json_and_result_upload_boundaries(api, registry, calibration):
    client, url, body = prepared(api, registry, calibration)
    assert client.post(url, json=body, headers={"Origin": "https://untrusted.example"}).status_code == 400
    assert client.post(url, json={**body, "submission_identity": "renamed-campaign"}).status_code == 400
    foreign = client.post(api[1], json={"config": {}, "submission_identity": "foreign"}).json()["experiment_id"]
    assert client.post(api[1] + "/" + foreign + "/calibrations", json=body).status_code == 404
    campaign = freeze(client, url, body, registry)
    complete = url + "/" + campaign["record_id"] + "/complete"
    assert client.post(complete, json={"allocations": {}, "formal_ready": True}).status_code == 400
    assert client.post(api[1] + "/" + foreign + "/calibrations/" + campaign["record_id"] + "/complete", json={}).status_code == 404
    assert client.post(url + "/" + registry[2]["record_id"] + "/complete", json={}).status_code == 404


def test_cli_preserves_blocked_state_and_canonical_complete_without_json(api, registry, calibration, monkeypatch, capsys, tmp_path):
    client, _, body = prepared(api, registry, calibration)
    mount_api(monkeypatch, client)
    path = tmp_path / "calibration.json"; path.write_text(json.dumps(body))
    prefix = ["research", "experiments", "calibrations"]
    code, frozen = invoke(capsys, [*prefix, "freeze", registry[1], "--json", str(path)])
    assert code == 0 and frozen["status"] == 201
    campaign = registry[0].records.read(frozen["data"]["calibration"]["record_id"])
    command = [*prefix, "complete", registry[1], campaign["record_id"]]
    code, blocked = invoke(capsys, command)
    assert code == 4 and blocked["status"] == 409
    for index in range(3): complete_test(calibration, index, campaign)
    code, result = invoke(capsys, command)
    assert code == 0 and result["status"] == 200 and not result["data"]["execution_available"]
    assert invoke(capsys, command)[1] == result


def test_actual_app_uses_its_configured_store_for_calibration(api, registry, calibration, tmp_path):
    _, url, body = prepared(api, registry, calibration)
    root = _research_workspace(tmp_path)
    config = root / "config/advisor.yaml"
    artifacts = resolve_research_artifact_dir(load_advisor_config(config), root)
    shutil.copytree(registry[0].artifacts.root, artifacts, dirs_exist_ok=True)
    path = registry[0].records.db.execute("PRAGMA database_list").fetchone()[2]
    app = create_app(state_dir=tmp_path / "state", db_path=path, research_root=root, research_config_path=config)
    with TestClient(app) as client:
        first = client.post(url, json=body)
        assert first.status_code == 201, first.text
        assert client.post(url, json=body).json() == first.json()
        complete = client.post(url + "/" + first.json()["calibration"]["record_id"] + "/complete", json={})
        assert complete.status_code == 409 and complete.json()["detail"]["reason"] == "calibration_incomplete"
    assert count(registry, "calibration_campaign") == 1 and count(registry, "calibration_result") == 0


@pytest.mark.parametrize("operation", ["freeze", "complete"])
def test_slow_calibration_work_does_not_block_other_requests(api, registry, calibration, monkeypatch, operation):
    client, url, body = prepared(api, registry, calibration)
    if operation == "complete":
        campaign = freeze(client, url, body, registry)
        url, body = url + "/" + campaign["record_id"] + "/complete", {}
    entered, release = Event(), Event()
    original = getattr(Calibrations, operation)
    def slow(self, *args, **kwargs):
        entered.set()
        if not release.wait(5): raise OSError("fixture wait timed out")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Calibrations, operation, slow)
    with ThreadPoolExecutor(max_workers=2) as workers:
        pending = workers.submit(client.post, url, json=body)
        try:
            assert entered.wait(5)
            probe = workers.submit(client.get, api[1] + "/presets/original-case")
            assert probe.result(timeout=2).status_code == 200 and not pending.done()
        finally:
            release.set()
        assert pending.result(timeout=5).status_code == (201 if operation == "freeze" else 409)
