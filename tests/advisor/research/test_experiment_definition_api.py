import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from advisor.research.experiments.definitions import DefinitionResolutions
from advisor.research.experiments.data.bundles import HistoricalBundles
from advisor.research.experiments.records import ExperimentRecords
from advisor.research.experiments.resolution import digest, encode
from advisor.research.experiments.selection import ExperimentSelection
from advisor.web.api import create_app
from tests.advisor.research.test_experiment_api import api, registry
from tests.advisor.research.test_experiment_bundles import bundle, imported
from tests.advisor.research.test_experiment_candidate_api import upload
from tests.advisor.research.test_experiment_registration import candidate, plan_input
from tests.advisor.test_agent_cli import mount_api, invoke
from tests.advisor.test_research_web import _research_workspace


def register_inputs(api, registry, bundle):
    client, base, _ = api
    url = base + "/" + registry[1]
    imported = client.post(url + "/bundles/import", json={"root": str(bundle[2]), "submission_identity": "bundle"})
    assert imported.status_code == 201, imported.text
    body = {"config": bundle[3], "calendar_bundle_id": imported.json()["bundle"]["record_id"],
            "submission_identity": "definition"}
    return client, url, body, imported.json()


def test_actual_app_registration_workflow_keeps_execution_unavailable(bundle, tmp_path):
    root = _research_workspace(tmp_path)
    path = tmp_path / "api.sqlite"
    app = create_app(state_dir=tmp_path / "state", db_path=path, research_root=root,
                     research_config_path=root / "config/advisor.yaml")
    with TestClient(app) as client:
        base = "/api/research/experiments"
        draft = client.post(base, json={"config": bundle[3], "submission_identity": "draft"}).json()
        url = base + "/" + draft["experiment_id"]
        imported = client.post(url + "/bundles/import", json={"root": str(bundle[2]), "submission_identity": "bundle"})
        assert imported.status_code == 201, imported.text
        resolved = client.post(url + "/definitions/resolve", json={"config": bundle[3],
            "calendar_bundle_id": imported.json()["bundle"]["record_id"], "submission_identity": "definition"})
        assert resolved.status_code == 200 and resolved.json()["status"] == "resolved", resolved.text
        definition = resolved.json()["definition"]
        proposal = client.post(url + "/candidates", json=upload())
        assert proposal.status_code == 201
        selected = client.post(url + "/selection/initialize", json={"definition_id": definition["record_id"],
            "baseline_id": proposal.json()["proposal"]["record_id"]})
        assert selected.status_code == 201
        assert client.get(url + "/selection").json()["selection"]["record_id"] == selected.json()["selection"]["record_id"]
        assert client.post(url + "/candidates", json=upload("a", "baseline")).status_code == 201
        planned = client.post(url + "/plans", json={"definition_id": definition["record_id"],
            "plan": plan_input(definition, names=("baseline", "a")), "purpose": "tuning", "selection_id": None, "submission_identity": "plan"})
        assert planned.status_code == 201 and len(planned.json()["tests"]) == 6, planned.text
        comparison = client.post(url + "/comparisons", json={"plan_id": planned.json()["plan"]["record_id"],
            "baseline": "baseline", "candidate": "a", "submission_identity": "pair", "previous_comparison": None,
            "expected_selection_id": selected.json()["selection"]["record_id"], "runtime_conditions": None, "runtime_artifacts_base64": {}})
        assert comparison.status_code == 201, comparison.text
        overview = client.get(url + "/records", params={"kind": "test"}).json()
        assert {test["status"] for test in overview["items"]} == {"created"}
        test_id = planned.json()["tests"][0]["record_id"]
        stopped = client.post(url + "/tests/" + test_id + "/cancel", json={"submission_identity": "cancel", "reason": "stop before execution"})
        assert stopped.status_code == 200 and stopped.json()["status"] == "cancelled"
        rerun = client.post(url + "/tests/" + test_id + "/rerun", json={"submission_identity": "rerun", "reason": "separate sample"})
        assert rerun.status_code == 201 and rerun.json()["status"] == "created"
        assert rerun.json()["test"]["value"]["rerun_of"] == test_id
        for test in planned.json()["tests"][1:]:
            stopped = client.post(url + "/tests/" + test["record_id"] + "/cancel", json={"submission_identity": test["record_id"], "reason": "stop original sample"})
            assert stopped.status_code == 200
        completed = client.post(url + "/comparisons/" + comparison.json()["comparison"]["record_id"] + "/complete", json={})
        assert completed.status_code == 200 and completed.json()["feedback"]["decision"] == "inconclusive"
        assert completed.json()["feedback"]["mean_improvement"] is None and not completed.json()["execution_available"]
        frozen = client.post(url + "/selection/finalize-holdout", json={
            "expected_selection_id": selected.json()["selection"]["record_id"], "submission_identity": "freeze"})
        assert frozen.status_code == 409  # The separate rerun remains unfinished.
    with sqlite3.connect(path) as db:
        for table in ("research_work_queue", "research_requests", "ledger_transactions"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM lagent_events").fetchone()[0] == 6  # Owner cancellation only.


def test_slow_bundle_sealing_does_not_block_other_api_requests(api, registry, bundle, monkeypatch):
    client, base, _ = api
    entered, release = Event(), Event()
    seal = HistoricalBundles._seal_file
    def slow(self, *args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise OSError("fixture timed out waiting for independent API response")
        return seal(self, *args, **kwargs)
    monkeypatch.setattr(HistoricalBundles, "_seal_file", slow)
    with ThreadPoolExecutor(max_workers=2) as workers:
        importing = workers.submit(client.post, base + "/" + registry[1] + "/bundles/import",
            json={"root": str(bundle[2]), "submission_identity": "slow-import"})
        try:
            assert entered.wait(5)
            probe = workers.submit(client.get, base + "/presets/original-case")
            assert probe.result(timeout=2).status_code == 200
            assert not importing.done()
        finally:
            release.set()
        assert importing.result(timeout=5).status_code == 201


def test_import_resolve_and_complete_plan_preserve_original_conditions_without_queueing(api, registry, bundle):
    client, url, body, imported = register_inputs(api, registry, bundle)
    assert imported["bundle"]["value"]["source_acceptance"] == "fixture_only"
    assert client.post(url + "/bundles/import", json={"root": str(bundle[2]), "submission_identity": "bundle"}).json() == imported
    response = client.post(url + "/definitions/resolve", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "resolved" and result["errors"] == [] and not result["execution_available"]
    definition = result["definition"]
    sealed = definition["value"]["specification"]
    assert json.loads(sealed["raw_json"]) == body["config"]
    assert sealed["tasks"][0]["trading_dates"] == ["2026-08-03"]
    source = definition["value"]["calendar_source"]
    assert source["source_acceptance"] == "fixture_only" and source["origin"] == "fixture"
    assert list(source["period_cutoffs"].values()) == [sealed["tasks"][0]["initial_as_of"]]
    assert client.post(url + "/definitions/resolve", json=body).json() == result
    for name, parent in (("baseline", None), ("branch", "baseline")):
        assert client.post(url + "/candidates", json=upload(name, parent)).status_code == 201
    plan = {"definition_id": definition["record_id"], "plan": plan_input(definition, names=("baseline", "branch")),
            "purpose": "tuning", "selection_id": None, "submission_identity": "plan"}
    response = client.post(url + "/plans", json=plan)
    assert response.status_code == 201, response.text
    registered = response.json()
    assert len(registered["tests"]) == 6 and not registered["execution_available"]
    assert client.post(url + "/plans", json=plan).json() == registered
    records = registry[0].records
    assert all(records.status(test["record_id"]) == "created" for test in registered["tests"])
    assert [test["value"]["sample"] for test in registered["tests"]] == plan["plan"]["tests"]
    for table in ("research_work_queue", "research_requests", "ledger_transactions", "lagent_events"):
        assert records.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert client.get(url).json()["raw_config"] != body["config"]  # Draft remains the original multi-task input.


@pytest.mark.parametrize("failure", ["missing_day", "late_version", "wrong_ref", "too_short", "invalid_config"])
def test_resolution_failures_are_immutable_without_shortening_periods(api, registry, bundle, failure):
    if failure == "missing_day":
        bundle[4]["calendar"].pop(0)
    elif failure == "late_version":
        bundle[4]["calendar"][0]["version_public_at"] = "2026-08-03T00:00:00+08:00"
    elif failure == "wrong_ref":
        bundle[5]["calendar_ref"] = "different@1"
    elif failure == "too_short":
        bundle[3]["tasks"][0]["period"]["trading_days"] = 2
    elif failure == "invalid_config":
        bundle[3].pop("clock")
    bundle[6]()
    client, url, body, _ = register_inputs(api, registry, bundle)
    first = client.post(url + "/definitions/resolve", json=body)
    assert first.status_code == 200, first.text
    value = first.json()
    assert value["status"] == "blocked" and value["definition"] is None and value["errors"]
    assert value["resolution"]["value"]["request"]["config"] == body["config"]
    assert not value["execution_available"]
    # A retry reads the original decision even if its source is no longer readable.
    registry[0].artifacts._path_for(bundle[5]["files"][0]["sha256"]).unlink()
    assert client.post(url + "/definitions/resolve", json=body).json() == value
    changed = {**body, "config": {**body["config"], "name": "changed"}}
    assert client.post(url + "/definitions/resolve", json=changed).status_code == 409


@pytest.mark.parametrize("damage", ["index_rehashed", "deleted_index", "original_corrupt", "original_missing"])
def test_calendar_resolution_requires_original_bytes_and_matching_index(api, registry, bundle, damage):
    client, url, body, _ = register_inputs(api, registry, bundle)
    records = registry[0].records
    if damage == "index_rehashed":
        # Simulate offline corruption past the normal append-only SQL guard.
        records.db.execute("DROP TRIGGER lagent_bundle_rows_no_update")
        row = json.loads(encode(bundle[4]["calendar"][0]))
        row["payload"]["is_open"] = True
        records.db.execute("UPDATE lagent_bundle_rows SET value_json=?, value_hash=? WHERE bundle_id=? AND row_id=?",
                           (encode(row), digest(row), body["calendar_bundle_id"], row["row_id"]))
        records.db.commit()
    elif damage == "deleted_index":
        records.db.execute("DROP TRIGGER lagent_bundle_rows_no_delete")
        records.db.execute("DELETE FROM lagent_bundle_rows WHERE bundle_id=? AND row_id='calendar-1'", (body["calendar_bundle_id"],))
        records.db.commit()
    else:
        original = records.artifacts._path_for(bundle[5]["files"][0]["sha256"])
        if damage == "original_missing":
            original.unlink()
        else:
            original.write_bytes(b"corrupt sealed calendar")
    response = client.post(url + "/definitions/resolve", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "blocked" and response.json()["definition"] is None
    assert {error["code"] for error in response.json()["errors"]} == {"calendar_unavailable"}


@pytest.mark.parametrize("point", ["after_record", "after_artifact_references", "before_commit", "after_commit"])
def test_definition_and_resolution_commit_together_and_retry_after_lost_reply(bundle, point):
    bundles, experiment, _, config, *_ = bundle
    imported_record = imported(bundle)
    resolver = DefinitionResolutions(bundles.records)
    kwargs = {"calendar_bundle_id": imported_record["record_id"], "submission_identity": "definition"}
    def fail(actual):
        if actual == point:
            raise OSError("interrupted registration")
    bundles.records.fault = fail
    with pytest.raises(OSError):
        resolver.register(experiment, config, **kwargs)
    bundles.records.fault = lambda _: None
    counts = dict(bundles.records.db.execute("SELECT kind,COUNT(*) FROM lagent_records GROUP BY kind"))
    assert counts.get("definition", 0) == counts.get("preflight", 0) == int(point == "after_commit")
    first = resolver.register(experiment, config, **kwargs)
    assert first["status"] == "resolved" and resolver.register(experiment, config, **kwargs) == first


@pytest.mark.parametrize("damage", ["manifest", "hash", "row"])
def test_import_error_returns_durable_failure_identity_without_partial_bundle(api, registry, bundle, damage):
    if damage == "manifest":
        (bundle[2] / "manifest.json").write_text("{}")
    elif damage == "hash":
        (bundle[2] / "calendar.jsonl").write_text("changed")
    else:
        bundle[4]["minute"][0]["payload"]["volume"] = -1
        bundle[6]()
    client, base, _ = api
    body = {"root": str(bundle[2]), "submission_identity": "broken"}
    response = client.post(base + "/" + registry[1] + "/bundles/import", json=body)
    assert response.status_code == 400
    error = response.json()["detail"]
    record = registry[0].records.read(error["failure_record_id"])
    assert error["status"] == record["value"]["status"] == "blocked"
    assert error["issues"] == record["value"]["issues"]
    assert registry[0].records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='data_bundle'").fetchone()[0] == 0
    assert registry[0].records.db.execute("SELECT COUNT(*) FROM lagent_bundle_rows").fetchone()[0] == 0
    assert client.post(base + "/" + registry[1] + "/bundles/import", json=body).json() == response.json()


@pytest.mark.parametrize("change", ["partial", "grouped", "hash", "selection", "purpose", "missing_candidate"])
def test_plan_rejects_changed_sample_matrix_conditions_and_unfrozen_holdout(api, registry, change):
    service, experiment, definition, package, _ = registry
    candidate(service, experiment, package)
    candidate(service, experiment, package, name="branch", parent="baseline")
    body = {"definition_id": definition["record_id"], "plan": plan_input(definition, names=("baseline", "branch")),
            "purpose": "tuning", "selection_id": None, "submission_identity": "invalid"}
    if change == "partial": body["plan"]["tests"].pop()
    elif change == "grouped": body["plan"]["tests"].sort(key=lambda row: row["proposal_id"])
    elif change == "hash": body["plan"]["specification_hash"] = "0" * 64
    elif change == "selection": body["selection_id"] = "not-frozen"
    elif change == "purpose": body["purpose"] = "final_holdout"
    elif change == "missing_candidate":
        for row in body["plan"]["tests"]:
            if row["proposal_id"] == "branch": row["proposal_id"] = "missing"
    response = api[0].post(api[1] + "/" + experiment + "/plans", json=body)
    assert response.status_code in (400, 404, 409), response.text
    assert service.records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind IN ('test_plan','test')").fetchone()[0] == 0


def test_public_definitions_and_plans_honor_freeze_and_original_retry(api, registry, bundle):
    client, url, body, _ = register_inputs(api, registry, bundle)
    resolved = client.post(url + "/definitions/resolve", json=body).json()
    service, experiment, definition, package, _ = registry
    baseline = candidate(service, experiment, package)
    selector = ExperimentSelection(service.records)
    initial = selector.initialize(experiment, definition["record_id"], baseline["proposal"]["record_id"])
    selector.finalize(experiment, expected_selection_id=initial["record_id"], submission_identity="freeze")
    assert client.post(url + "/definitions/resolve", json=body).json() == resolved
    assert client.post(url + "/definitions/resolve", json={**body, "submission_identity": "new"}).status_code == 409
    plan = {"definition_id": definition["record_id"], "plan": plan_input(definition),
            "purpose": "tuning", "selection_id": None, "submission_identity": "after-freeze"}
    assert client.post(url + "/plans", json=plan).status_code == 409


def test_cli_import_resolve_and_register_preserve_bodies_and_created_tests(api, registry, bundle, monkeypatch, capsys, tmp_path):
    client, _, _ = api
    mount_api(monkeypatch, client)
    prefix = ["research", "experiments"]
    code, imported = invoke(capsys, [*prefix, "bundles", "import", registry[1], "--root", str(bundle[2]), "--submission-identity", "cli-bundle"])
    assert code == 0 and imported["status"] == 201
    path = tmp_path / "request.json"
    body = {"config": bundle[3], "calendar_bundle_id": imported["data"]["bundle"]["record_id"], "submission_identity": "cli-definition"}
    path.write_text(json.dumps(body))
    code, resolved = invoke(capsys, [*prefix, "definitions", "resolve", registry[1], "--json", str(path)])
    assert code == 0 and resolved["data"]["status"] == "resolved"
    definition = resolved["data"]["definition"]
    candidate(registry[0], registry[1], registry[3])
    body = {"definition_id": definition["record_id"], "plan": plan_input(definition), "purpose": "tuning",
            "selection_id": None, "submission_identity": "cli-plan"}
    path.write_text(json.dumps(body))
    code, planned = invoke(capsys, [*prefix, "plans", "register", registry[1], "--json", str(path)])
    assert code == 0 and planned["status"] == 201 and len(planned["data"]["tests"]) == 3
    assert not planned["data"]["execution_available"]
    assert all(registry[0].records.status(test["record_id"]) == "created" for test in planned["data"]["tests"])
    path.write_text(json.dumps({"config": {}, "calendar_bundle_id": imported["data"]["bundle"]["record_id"],
                                "submission_identity": "cli-blocked"}))
    code, blocked = invoke(capsys, [*prefix, "definitions", "resolve", registry[1], "--json", str(path)])
    assert code == 0 and blocked["ok"] and blocked["data"]["status"] == "blocked"
    assert blocked["data"]["definition"] is None and blocked["data"]["errors"]
    (bundle[2] / "manifest.json").write_text("{}")
    code, failed = invoke(capsys, [*prefix, "bundles", "import", registry[1], "--root", str(bundle[2]), "--submission-identity", "cli-invalid"])
    assert code == 4 and failed["status"] == 400
    failure = failed["error"]["detail"]["detail"]
    assert failure["status"] == "blocked" and failure["issues"]
    assert registry[0].records.read(failure["failure_record_id"])["kind"] == "bundle_import"


@pytest.mark.parametrize("suffix,body", [
    ("/bundles/import", {"root": "/explicit/local/bundle", "submission_identity": "local"}),
    ("/definitions/resolve", {"config": {}, "calendar_bundle_id": "bundle", "submission_identity": "local"}),
    ("/plans", {"definition_id": "definition", "plan": {}, "purpose": "tuning", "selection_id": None, "submission_identity": "local"}),
])
def test_new_writes_reject_cross_origin_before_storage(api, registry, suffix, body):
    client, base, calls = api
    response = client.post(base + "/" + registry[1] + suffix, json=body, headers={"Origin": "https://untrusted.example"})
    assert response.status_code == 400 and calls == []


def test_resolution_and_plan_references_cannot_cross_experiments_or_supply_calendar_claims(api, registry, bundle):
    client, url, body, _ = register_inputs(api, registry, bundle)
    foreign = imported(bundle)
    assert client.post(url + "/definitions/resolve", json={**body, "calendar_bundle_id": foreign["record_id"]}).status_code == 404
    assert client.post(url + "/definitions/resolve", json={**body, "calendar": {"complete": True}}).status_code == 400
    assert client.post(url + "/definitions/resolve", json={**body, "calendar_bundle_id": []}).status_code == 400
    resolved = DefinitionResolutions(bundle[0].records).register(bundle[1], bundle[3],
        calendar_bundle_id=foreign["record_id"], submission_identity="foreign")
    definition = resolved["definition"]
    plan = {"definition_id": definition["record_id"], "plan": plan_input(definition), "purpose": "tuning",
            "selection_id": None, "submission_identity": "foreign-plan"}
    assert client.post(url + "/plans", json=plan).status_code == 404
    assert client.post(url + "/bundles/import", json={"root": "relative/path", "submission_identity": "relative"}).status_code == 400


@pytest.mark.parametrize("after_commit", [False, True])
def test_plan_api_storage_failure_never_publishes_a_partial_matrix(api, registry, monkeypatch, after_commit):
    service, experiment, definition, package, _ = registry
    candidate(service, experiment, package)
    body = {"definition_id": definition["record_id"], "plan": plan_input(definition), "purpose": "tuning",
            "selection_id": None, "submission_identity": "plan"}
    original = ExperimentRecords.__init__
    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        def fail(point):
            if point == ("after_commit" if after_commit else "after_record"):
                raise sqlite3.OperationalError("plan reply lost")
        self.fault = fail
    monkeypatch.setattr(ExperimentRecords, "__init__", initialize)
    url = api[1] + "/" + experiment + "/plans"
    assert api[0].post(url, json=body).status_code == 503
    counts = dict(service.records.db.execute("SELECT kind,COUNT(*) FROM lagent_records GROUP BY kind"))
    assert counts.get("test_plan", 0) == int(after_commit) and counts.get("test", 0) == 3 * int(after_commit)
    monkeypatch.setattr(ExperimentRecords, "__init__", original)
    result = api[0].post(url, json=body)
    assert result.status_code == 201 and api[0].post(url, json=body).json() == result.json()
