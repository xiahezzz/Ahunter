from datetime import timedelta
import json
import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from advisor.research.artifacts import ArtifactStore
from advisor.research.repository import ResearchRepository
from advisor.research.experiments.api import register_experiment_routes
from advisor.research.experiments.records import ExperimentRecords
from advisor.research.experiments.repository import ExperimentStore
from advisor.research.experiments.sources import SourceRetention
from advisor.research.experiments.contracts import original_case
from advisor.web.api import create_app
from tests.advisor.research.test_experiment_registration import registry, hidden_test, register_plan
from tests.advisor.test_research_web import _research_workspace


@pytest.fixture
def api(registry):
    service, experiment, *_ = registry
    path = service.records.db.execute("PRAGMA database_list").fetchone()[2]
    calls = []
    def repository_factory(*, writable):
        calls.append(("repository", writable))
        return ResearchRepository(sqlite3.connect(path))
    def catalog_factory():
        calls.append(("catalog", False))
        return SimpleNamespace(products={})
    app = FastAPI()
    register_experiment_routes(app, repository_factory=repository_factory, catalog_factory=catalog_factory,
                               artifact_store_factory=lambda **_: ArtifactStore(service.artifacts.root))
    with TestClient(app) as client:
        yield client, "/api/research/experiments", calls


def exposures(registry):
    return registry[0].records.page(experiment_id=registry[1], kind="exposure", limit=100)["items"]


def secret_record(registry):
    service, experiment, *_ = registry
    test = hidden_test(registry)
    artifact = service.artifacts.put_bytes(b"SECRET-FUTURE-RESULT\x00\xff")
    record = service.records.put(experiment_id=experiment, kind="evaluation", record_id="private-eval",
        submission_identity="private-eval", value={"result": "SECRET-FUTURE-RESULT"},
        links=(("test", test["record_id"]),), artifact_hashes=(artifact.content_hash,))
    return test, record, artifact


def test_actual_app_exposes_presets_drafts_and_blocked_preflight_without_queue_work(tmp_path):
    root = _research_workspace(tmp_path)
    path = tmp_path / "api.sqlite"
    app = create_app(state_dir=tmp_path / "state", db_path=path, research_root=root,
                     research_config_path=root / "config/advisor.yaml")
    with TestClient(app) as client:
        base = "/api/research/experiments"
        preset = client.get(base + "/presets/original-case")
        assert preset.status_code == 200 and not path.exists()
        assert preset.json()["config"] == original_case()
        created = client.post(base, json={"config": preset.json()["config"], "submission_identity": "app-create"})
        assert created.status_code == 201
        value = created.json()
        assert not value["execution_available"] and value["status"] == "draft"
        report = client.post(base + "/" + value["experiment_id"] + "/preflight", json={"submission_identity": "preflight"})
        assert report.status_code == 200 and report.json()["status"] == "blocked"
        assert report.json()["model_calls"] == report.json()["orders_created"] == 0
        assert report.json()["return"] is None
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM research_requests").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM research_work_queue").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='test'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM ledger_transactions").fetchone()[0] == 0


def test_draft_identity_preserves_incomplete_input_and_rejects_changed_retries(api):
    client, base, _ = api
    body = {"config": {"name": "尚未填写", "unknown": "preserve me"}, "submission_identity": "empty"}
    first = client.post(base, json=body)
    assert first.status_code == 201 and first.json()["validation_errors"]
    assert first.json()["raw_config"] == body["config"]
    assert client.post(base, json=body).json() == first.json()
    assert client.get(base + "/" + first.json()["experiment_id"]).json() == first.json()
    assert client.post(base, json={**body, "config": {}}).status_code == 409
    assert client.get(base + "/missing").status_code == 404


@pytest.mark.parametrize("body", [{}, {"config": {}, "submission_identity": ""},
    {"config": {}, "submission_identity": True}, {"config": [], "submission_identity": "bad"},
    {"config": {}, "submission_identity": "bad", "viewer": "optimizer"},
    {"config": {}, "submission_identity": "a" * 257}])
def test_bad_create_body_does_not_register_a_draft(api, registry, body):
    client, base, _ = api
    assert client.post(base, json=body).status_code == 400
    assert registry[0].records.db.execute("SELECT COUNT(*) FROM lagent_experiments").fetchone()[0] == 1


@pytest.mark.parametrize("headers,content", [({"Origin": "https://outside.example", "Content-Type": "application/json"}, "{}"),
    ({"Sec-Fetch-Site": "cross-site", "Content-Type": "application/json"}, "{}"),
    ({"Content-Type": "text/plain"}, "{}"), ({"Content-Type": "application/json"}, "[]"),
    ({"Content-Type": "application/json"}, '{"config":{"number":NaN},"submission_identity":"nan"}'),
    ({"Content-Type": "application/json"}, '{"config":{},"submission_identity":"' + "x" * 1_000_000 + '"}')],
    ids=["cross-origin", "cross-site", "content-type", "non-object", "non-finite", "oversized"])
def test_local_json_and_size_boundaries_fail_before_storage(api, headers, content):
    client, base, calls = api
    assert client.post(base, content=content, headers=headers).status_code == 400
    assert calls == []


def test_remote_peer_cannot_submit_even_with_local_host(api):
    client, base, calls = api
    with TestClient(client.app, base_url="http://127.0.0.1", client=("192.0.2.2", 3210)) as remote:
        assert remote.post(base, json={"config": {}, "submission_identity": "remote"}).status_code == 400
    assert calls == []


def test_draft_and_preflight_pagination_and_original_result_retry(api, registry, monkeypatch):
    client, base, calls = api
    for index in range(3):
        assert client.post(base, json={"config": {}, "submission_identity": "draft-" + str(index)}).status_code == 201
    first = client.get(base, params={"limit": 2}).json()
    second = client.get(base, params={"limit": 2, "offset": first["next_offset"]}).json()
    assert len({item["experiment_id"] for item in first["items"] + second["items"]}) == 4
    assert second["next_offset"] is None
    url = base + "/" + registry[1] + "/preflight"
    original = client.post(url, json={"submission_identity": "first"})
    assert original.status_code == 200 and original.json()["status"] == "blocked"
    assert [name for name, _ in calls].count("catalog") == 1
    assert client.post(url, json={"submission_identity": "first"}).json() == original.json()
    assert [name for name, _ in calls].count("catalog") == 1
    assert client.post(url, json={"submission_identity": "second"}).json()["record_id"] != original.json()["record_id"]
    page = client.get(url + "s", params={"limit": 1}).json()
    assert page["items"] == [original.json()] and page["next_offset"] == 1
    def fail(*args, **kwargs): raise sqlite3.OperationalError("fixture write failed")
    monkeypatch.setattr(ExperimentStore, "record_preflight", fail)
    assert client.post(url, json={"submission_identity": "failure"}).status_code == 503
    assert client.post(url, json={"submission_identity": "first"}).json() == original.json()


def test_record_overview_hides_values_and_cursor_retains_original_ceiling(api, registry):
    client, base, _ = api
    secret_record(registry)
    url = base + "/" + registry[1] + "/records"
    page = client.get(url, params={"limit": 1, "kind": "test"}).json()
    assert "SECRET" not in json.dumps(page) and page["items"][0]["detail_hidden"] is True
    assert page["items"][0]["status"] == "created" and "value" not in page["items"][0]
    assert exposures(registry) == []
    cursor = page["next_cursor"]
    register_plan(registry, task="august-selection", purpose="selection_validation", plan_id="later-plan")
    assert client.get(url, params={"cursor": cursor, "kind": "evaluation"}).status_code == 400
    more = client.get(url, params={"cursor": cursor, "kind": "test", "limit": 100}).json()
    assert len(more["items"]) == 2 and more["next_cursor"] is None
    assert len(client.get(url, params={"kind": "test"}).json()["items"]) == 6
    assert client.get(url, params={"cursor": "invalid", "kind": "test"}).status_code == 400
    assert client.get(url, params={"kind": "not-a-kind"}).status_code == 400
    assert client.get(url, params={"limit": 201}).status_code == 422


def test_hidden_record_read_commits_exposure_once_and_rejects_cross_scope_or_rebinding(api, registry):
    client, base, _ = api
    test, record, _ = secret_record(registry)
    root = base + "/" + registry[1] + "/records/"
    url = root + record["record_id"] + "/detail"
    assert client.get(url).status_code == 405
    assert client.post(url, json={}).status_code == 400 and exposures(registry) == []
    first = client.post(url, json={"audit_identity": "review-1"})
    assert first.status_code == 200 and first.json() == record
    assert client.post(url, json={"audit_identity": "review-1"}).json() == record
    assert len(exposures(registry)) == 1 and exposures(registry)[0]["value"]["action"] == "record_detail"
    assert client.post(root + test["record_id"] + "/detail", json={"audit_identity": "review-1"}).status_code == 409
    other = client.post(base, json={"config": {}, "submission_identity": "other"}).json()["experiment_id"]
    assert client.post(base + "/" + other + "/records/" + record["record_id"] + "/detail",
                       json={"audit_identity": "cross"}).status_code == 404
    assert len(exposures(registry)) == 1


def test_failed_exposure_commit_never_releases_hidden_detail_or_bytes(api, registry, monkeypatch):
    client, base, _ = api
    _, record, artifact = secret_record(registry)
    original = ExperimentRecords._insert
    def fail(self, prepared):
        if prepared["kind"] == "exposure": raise sqlite3.OperationalError("audit fixture failure")
        return original(self, prepared)
    monkeypatch.setattr(ExperimentRecords, "_insert", fail)
    root = base + "/" + registry[1] + "/records/" + record["record_id"]
    for suffix in ("/detail", "/export", "/artifacts/" + artifact.content_hash):
        response = client.post(root + suffix, json={"audit_identity": "cannot-release"})
        assert response.status_code == 503 and "SECRET" not in response.text
    assert exposures(registry) == []


def test_test_event_pagination_and_audit_action_binding(api, registry):
    client, base, _ = api
    test = hidden_test(registry)
    records = registry[0].records
    records.transition(test["record_id"], "preflight", action_id="one")
    records.transition(test["record_id"], "blocked", action_id="two", reason="fixture_source_missing")
    url = base + "/" + registry[1] + "/tests/" + test["record_id"] + "/events"
    first = client.post(url, params={"limit": 1}, json={"audit_identity": "events-1"})
    assert first.status_code == 200
    event = first.json()["items"][0]
    second = client.post(url, params={"limit": 1, "after": event["sequence"]}, json={"audit_identity": "events-2"})
    assert second.status_code == 200 and second.json()["items"][0]["sequence"] > event["sequence"]
    assert len(exposures(registry)) == 2
    assert client.post(url, params={"after": event["sequence"]}, json={"audit_identity": "events-1"}).status_code == 409
    assert client.post(url, params={"after": -1}, json={"audit_identity": "bad"}).status_code == 422


def test_manifest_and_binary_download_use_original_links_and_hashes(api, registry):
    client, base, _ = api
    test, record, artifact = secret_record(registry)
    root = base + "/" + registry[1] + "/records/" + record["record_id"]
    exported = client.post(root + "/export", json={"audit_identity": "export"})
    assert exported.status_code == 200 and "attachment" in exported.headers["content-disposition"]
    manifest = exported.json()
    assert manifest["record"] == record and manifest["record_replay"] == "exact_bytes"
    assert manifest["artifacts"][0]["content_hash"] == artifact.content_hash
    downloaded = client.post(root + "/artifacts/" + artifact.content_hash, json={"audit_identity": "bytes"})
    assert downloaded.status_code == 200 and downloaded.content == b"SECRET-FUTURE-RESULT\x00\xff"
    assert downloaded.headers["x-content-sha256"] == artifact.content_hash
    wrong = base + "/" + registry[1] + "/records/" + test["record_id"] + "/artifacts/" + artifact.content_hash
    assert client.post(wrong, json={"audit_identity": "unlinked"}).status_code == 404
    assert len(exposures(registry)) == 2
    assert client.post(root + "/artifacts/invalid", json={"audit_identity": "invalid"}).status_code == 400
    registry[0].artifacts._path_for(artifact.content_hash).write_bytes(b"tampered")
    assert client.post(root + "/artifacts/" + artifact.content_hash, json={"audit_identity": "bad-bytes"}).status_code == 409
    manifest = client.post(root + "/export", json={"audit_identity": "corrupt-manifest"}).json()
    assert manifest["record_replay"] == "integrity_failure"


def test_expired_source_returns_metadata_but_never_original_bytes(api, registry):
    client, base, _ = api
    test = hidden_test(registry)
    records = registry[0].records
    artifact = records.artifacts.put_text("expired secret")
    at = records._now()
    source = SourceRetention(records).register(registry[1], test["record_id"], {
        "source_id": "expired", "content_hash": artifact.content_hash, "source_ref": "fixture", "title": "expired fixture",
        "url": None, "fetched_at": at - timedelta(days=2), "expires_at": at + timedelta(days=1), "retention_policy_ref": "fixture"},
        submission_identity="source")
    registry[4][0] += timedelta(days=2)
    SourceRetention(records).expire(source["record_id"], submission_identity="expire")
    root = base + "/" + registry[1] + "/records/" + source["record_id"]
    response = client.post(root + "/artifacts/" + artifact.content_hash, json={"audit_identity": "expired-bytes"})
    assert response.status_code == 410 and "expired secret" not in response.text
    exported = client.post(root + "/export", json={"audit_identity": "expired-manifest"}).json()
    assert exported["record_replay"] == "metadata_and_hash_only"


def test_aggregate_feedback_does_not_release_trace_or_holdout(api, registry):
    client, base, _ = api
    test = hidden_test(registry)
    records = registry[0].records
    summary = {"baseline_mean_return": "0", "candidate_mean_return": "0.1", "mean_improvement": "0.1",
               "worst_paired_difference": "0", "positive_repeats": 3, "planned_repeats": 3, "decision": "eligible", "trace": "SECRET"}
    records.put(experiment_id=registry[1], kind="comparison", record_id="compare", submission_identity="compare",
                value={"status": "completed", "public_summary": summary, "trace": "SECRET"}, links=(("test", test["record_id"]),))
    url = base + "/" + registry[1] + "/comparisons/compare/feedback"
    response = client.get(url)
    assert response.status_code == 200 and response.json()["decision"] == "eligible" and "SECRET" not in response.text
    assert exposures(registry) == []
    records.put(experiment_id=registry[1], kind="test", record_id="holdout", submission_identity="holdout",
                value={"registration_version": 1, "definition_id": registry[2]["record_id"], "task_id": "august-holdout"})
    records.put(experiment_id=registry[1], kind="comparison", record_id="final", submission_identity="final",
                value={"status": "completed", "public_summary": summary}, links=(("test", "holdout"),))
    assert client.get(url.replace("/compare/", "/final/")).status_code == 403
