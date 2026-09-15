"""Lineage pagination and observation boundaries; promotions use consumer fixtures."""
import base64
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from advisor.research.experiments.registration import record_identity
from advisor.research.experiments.selection import ExperimentSelection
from advisor.web.api import create_app
from tests.advisor.research.test_experiment_api import api, registry, exposures
from tests.advisor.research.test_experiment_registration import candidate, plan_input
from tests.advisor.research.test_experiment_baseline_reuse import template, reuse, freeze_reuse
from tests.advisor.research.test_experiment_promotion import setup, pair, accepted_samples, apply
from tests.advisor.test_agent_cli import mount_api, invoke
from tests.advisor.test_research_web import _research_workspace


def base(api, registry):
    return api[1] + "/" + registry[1]


def get(api, url, **params):
    response = api[0].get(url, params=params)
    assert response.status_code == 200, response.text
    assert not response.json()["execution_available"]
    return response.json()


def branches(registry):
    service, experiment, definition, package, _ = registry
    root = candidate(service, experiment, package)["proposal"]
    initial = ExperimentSelection(service.records).initialize(experiment, definition["record_id"], root["record_id"])
    for name, parent in (("failed-parent", "baseline"), ("retry-child", "failed-parent"), ("peer", "baseline")):
        candidate(service, experiment, package, name, parent)
    plan = service.test_plan(experiment, definition["record_id"],
        plan_input(definition, names=("failed-parent",), plan_id="failed"), submission_identity="failed", purpose="tuning")
    records = service.records
    records.transition(plan["tests"][0]["record_id"], "preflight", action_id="preflight")
    records.transition(plan["tests"][0]["record_id"], "failed", action_id="failed")
    records.transition(plan["tests"][1]["record_id"], "cancelled", action_id="cancel")
    service.rerun(experiment, plan["tests"][0]["record_id"], rerun_identity="retry", reason="independent linked retry")
    return initial, plan


def test_empty_tree_and_selection_history_are_non_mutating_even_without_artifacts(api, registry):
    for suffix in ("/tree", "/selection/history"):
        data = get(api, base(api, registry) + suffix)
        assert data["items"] == [] and data["total"] == 0 and data["selection"] is None and data["next_cursor"] is None
    assert exposures(registry) == []
    assert api[2] == [("repository", False), ("repository", False)]


def test_tree_keeps_proposal_parentage_duplicate_content_and_all_statuses(api, registry):
    initial, plan = branches(registry)
    data = get(api, base(api, registry) + "/tree")
    nodes = {item["proposal_id"]: item for item in data["items"]}
    assert len(nodes) == data["total"] == 4
    assert len({item["package_hash"] for item in nodes.values()}) == 1
    assert nodes["retry-child"]["parent_record_id"] == nodes["failed-parent"]["record_id"]
    assert nodes["retry-child"]["parent_proposal_id"] == "failed-parent"
    assert nodes["failed-parent"]["test_count"] == 4
    assert nodes["failed-parent"]["test_status_counts"] == {"cancelled": 1, "created": 2, "failed": 1}
    assert nodes["failed-parent"]["task_role_counts"] == {"tuning": 4}
    assert nodes["baseline"]["current_baseline"] and nodes["baseline"]["initial_baseline"]
    assert not nodes["peer"]["current_baseline"]
    assert data["selection"]["record_id"] == initial["record_id"]
    assert all(key not in json.dumps(data) for key in ("nav_diagnostic", "terminal_nav", "supplier_bill", "rerun_reason"))
    assert exposures(registry) == []


def test_tree_pages_pin_both_candidate_and_test_status_ceilings(api, registry):
    _, plan = branches(registry)
    url = base(api, registry) + "/tree"
    first = get(api, url, limit=1)
    records = registry[0].records
    records.transition(plan["tests"][2]["record_id"], "cancelled", action_id="later-cancel")
    candidate(registry[0], registry[1], registry[3], "later", "baseline")
    second = get(api, url, limit=1, cursor=first["next_cursor"])
    assert second["snapshot"] == first["snapshot"] and second["total"] == first["total"] == 4
    assert second["items"][0]["test_status_counts"] == {"cancelled": 1, "created": 2, "failed": 1}
    names = [first["items"][0]["proposal_id"], second["items"][0]["proposal_id"]]
    page = second
    while page["next_cursor"]:
        page = get(api, url, limit=1, cursor=page["next_cursor"])
        names += [item["proposal_id"] for item in page["items"]]
    assert names == ["baseline", "failed-parent", "retry-child", "peer"]
    current = get(api, url)
    assert current["total"] == 5
    assert current["items"][1]["test_status_counts"] == {"cancelled": 2, "created": 1, "failed": 1}


def test_node_tests_page_every_original_and_rerun_once_without_trace_details(api, registry):
    _, plan = branches(registry)
    url = base(api, registry) + "/candidates/failed-parent/tests"
    page = get(api, url, limit=2)
    records = page["items"] + get(api, url, limit=2, cursor=page["next_cursor"])["items"]
    assert len(records) == len({item["record_id"] for item in records}) == page["total"] == 4
    rerun = next(item for item in records if item["rerun_of"])
    assert rerun["rerun_of"] == plan["tests"][0]["record_id"] and not rerun["eligible_for_original_comparison"]
    assert rerun["status"] == "created" and rerun["purpose"] == "tuning"
    assert all(item["role"] == "tuning" and not item["detail_hidden"] for item in records)
    assert all(key not in json.dumps(records) for key in ("reason", "value", "events", "cost"))


def test_node_comparison_opponent_is_separate_from_parent_and_late_result_is_not_backfilled(api, registry):
    selector, initial = setup(registry)
    # b's parent is baseline, but its comparison opponent is the promoted a.
    comparer, plan, frozen = pair(registry, initial, name="a")
    accepted_samples(registry, plan, frozen)
    promoted = apply(selector, initial, comparer.complete(frozen["record_id"]))
    second, plan2, frozen2 = pair(registry, promoted, name="b", opponent="a")
    third, plan3, frozen3 = pair(registry, promoted, name="b", opponent="a", plan_id="b-second")
    url = base(api, registry) + "/candidates/b/comparisons"
    first = get(api, url, limit=1)
    for test in plan3["tests"]: registry[0].records.transition(test["record_id"], "cancelled", action_id="cancel")
    result3 = third.complete(frozen3["record_id"])
    page2 = get(api, url, limit=1, cursor=first["next_cursor"])
    assert page2["items"][0]["result_id"] is None and page2["items"][0]["feedback"] is None
    fresh = get(api, url)
    assert fresh["items"][1]["result_id"] == result3["record_id"]
    assert fresh["items"][1]["feedback"]["decision"] == "inconclusive"
    assert fresh["items"][1]["feedback"]["mean_improvement"] is None
    assert all(item["baseline"] == "a" and item["candidate"] == "b" for item in fresh["items"])
    node = next(item for item in get(api, base(api, registry) + "/tree")["items"] if item["proposal_id"] == "b")
    assert node["parent_proposal_id"] == "baseline"
    assert "samples" not in json.dumps(fresh) and exposures(registry) == []


def test_selection_history_keeps_retained_decisions_and_baseline_pointer_separate(api, registry):
    selector, initial = setup(registry)
    comparer, plan, frozen = pair(registry, initial, name="a")
    accepted_samples(registry, plan, frozen)
    promoted = apply(selector, initial, comparer.complete(frozen["record_id"]))
    comparer2, plan2, frozen2 = pair(registry, promoted, name="b", opponent="a")
    retained = apply(selector, promoted, comparer2.complete(frozen2["record_id"]), "retain")
    history = get(api, base(api, registry) + "/selection/history", limit=2)
    later = get(api, base(api, registry) + "/selection/history", limit=2, cursor=history["next_cursor"])
    assert [item["operation"] for item in history["items"] + later["items"]] == ["initialize", "promote", "retain"]
    assert later["items"][0]["record_id"] == retained["record_id"]
    assert later["items"][0]["reason"] == "required_samples_invalid"
    assert later["selection"]["record_id"] == promoted["record_id"]
    nodes = get(api, base(api, registry) + "/tree")["items"]
    assert [item["proposal_id"] for item in nodes if item["current_baseline"]] == ["a"]
    assert [item["proposal_id"] for item in nodes if item["initial_baseline"]] == ["baseline"]
    assert exposures(registry) == []


def test_snapshot_baseline_marker_does_not_change_between_pages(api, registry):
    selector, initial = setup(registry)
    comparer, plan, frozen = pair(registry, initial, name="a")
    accepted_samples(registry, plan, frozen)
    first = get(api, base(api, registry) + "/tree", limit=1)
    promoted = apply(selector, initial, comparer.complete(frozen["record_id"]))
    second = get(api, base(api, registry) + "/tree", limit=1, cursor=first["next_cursor"])
    assert second["selection"]["record_id"] == initial["record_id"]
    assert not second["items"][0]["current_baseline"]
    assert get(api, base(api, registry) + "/tree")["selection"]["record_id"] == promoted["record_id"]


def test_final_holdout_node_metadata_does_not_expose_results_or_consume_unseen_status(api, registry):
    service, experiment, definition, package, _ = registry
    root = candidate(service, experiment, package)["proposal"]
    selector = ExperimentSelection(service.records)
    initial = selector.initialize(experiment, definition["record_id"], root["record_id"])
    frozen = selector.finalize(experiment, expected_selection_id=initial["record_id"], submission_identity="final")
    plan = service.test_plan(experiment, definition["record_id"], plan_input(definition, task="august-holdout"),
                             purpose="final_holdout", selection_id=frozen["record_id"], submission_identity="holdout")
    test_id = plan["tests"][0]["record_id"]
    service.records.put(experiment_id=experiment, kind="evaluation", record_id="secret", submission_identity="secret",
                        value={"secret": "FUTURE-PRICE"}, links=(("test", test_id),))
    page = get(api, base(api, registry) + "/candidates/baseline/tests")
    assert page["total"] == 3 and all(item["detail_hidden"] for item in page["items"])
    history = get(api, base(api, registry) + "/selection/history")
    tree = get(api, base(api, registry) + "/tree")
    assert all(secret not in json.dumps([page, tree, history]) for secret in ("FUTURE-PRICE", "exposure_at_freeze", "holdout_dates"))
    assert exposures(registry) == []
    detail = api[0].post(base(api, registry) + "/records/secret/detail", json={"audit_identity": "explicit-read"})
    assert detail.status_code == 200 and exposures(registry)


@pytest.mark.parametrize("damage", ["scope", "view", "proposal", "future_records", "future_events", "negative", "boolean", "invalid_base64"])
def test_cursor_is_bound_to_view_scope_and_bounded_original_watermarks(api, registry, damage):
    branches(registry)
    url = base(api, registry) + "/tree"
    first = get(api, url, limit=1)
    cursor = json.loads(base64.urlsafe_b64decode(first["next_cursor"]))
    if damage == "scope": cursor["experiment_id"] = "other"
    elif damage == "view": cursor["view"] = "selection"
    elif damage == "proposal": cursor["proposal_id"] = "baseline"
    elif damage == "future_records": cursor["records_through"] += 10000
    elif damage == "future_events": cursor["events_through"] += 10000
    elif damage == "negative": cursor["after"] = -1
    elif damage == "boolean": cursor["events_through"] = True
    encoded = "%%%" if damage == "invalid_base64" else base64.urlsafe_b64encode(json.dumps(cursor).encode()).decode()
    assert api[0].get(url, params={"cursor": encoded}).status_code == 400


def test_node_cursor_cannot_move_to_another_proposal(api, registry):
    branches(registry)
    first = get(api, base(api, registry) + "/candidates/failed-parent/tests", limit=1)
    assert api[0].get(base(api, registry) + "/candidates/baseline/tests", params={"cursor": first["next_cursor"]}).status_code == 400


def test_history_remains_available_when_original_package_bytes_are_unavailable(api, registry):
    branches(registry)
    registry[0].artifacts._path_for(registry[3].files[0].content_hash).unlink()
    assert get(api, base(api, registry) + "/tree")["total"] == 4
    assert api[0].get(base(api, registry) + "/candidates/baseline").status_code == 409


@pytest.mark.parametrize("damage", ["parent", "event"])
def test_corrupt_lineage_or_original_status_events_fail_closed(api, registry, damage):
    _, plan = branches(registry)
    db = registry[0].records.db
    if damage == "parent":
        db.execute("DROP TRIGGER lagent_record_links_no_update")  # Isolated corruption fixture only.
        child = record_identity(registry[1], "candidate", "retry-child")
        db.execute("UPDATE lagent_record_links SET target_id=? WHERE record_id=? AND relation='parent'", (child, child))
    else:
        db.execute("DROP TRIGGER lagent_events_no_update")
        db.execute("UPDATE lagent_events SET value_hash=? WHERE test_id=?", ("0" * 64, plan["tests"][0]["record_id"]))
    db.commit()
    assert api[0].get(base(api, registry) + "/tree").status_code == 409


def test_cli_tree_node_queries_and_history_match_paged_public_responses(api, registry, monkeypatch, capsys):
    branches(registry)
    mount_api(monkeypatch, api[0])
    prefix = ["research", "experiments"]
    code, first = invoke(capsys, [*prefix, "tree", registry[1], "--limit", "1"])
    assert code == 0 and first["data"]["total"] == 4
    code, next_page = invoke(capsys, [*prefix, "tree", registry[1], "--limit", "1", "--cursor", first["data"]["next_cursor"]])
    assert code == 0 and next_page["data"]["items"][0]["proposal_id"] == "failed-parent"
    for command in (["candidates", "tests", registry[1], "failed-parent"], ["candidates", "comparisons", registry[1], "baseline"],
                    ["selection", "history", registry[1]]):
        assert invoke(capsys, [*prefix, *command])[0] == 0


def test_actual_app_tree_on_existing_empty_draft_needs_no_artifact_directory(tmp_path):
    root = _research_workspace(tmp_path)
    path = tmp_path / "api.sqlite"
    app = create_app(state_dir=tmp_path / "state", db_path=path, research_root=root, research_config_path=root / "config/advisor.yaml")
    with TestClient(app) as client:
        draft = client.post("/api/research/experiments", json={"config": {}, "submission_identity": "empty"}).json()
        url = "/api/research/experiments/" + draft["experiment_id"]
        assert client.get(url + "/tree").json()["items"] == []
        assert client.get(url + "/selection/history").json()["items"] == []
        assert client.get(url + "/candidates/unknown/tests").status_code == 404
        assert client.get(url + "/tree", params={"limit": 201}).status_code == 422
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM lagent_records").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM research_work_queue").fetchone()[0] == 0


def test_reused_baseline_samples_are_counted_once_while_all_comparisons_remain_reachable(api, registry):
    _, initial = setup(registry)
    comparer, original, frozen = pair(registry, initial)
    accepted_samples(registry, original, frozen)
    comparer.complete(frozen["record_id"])
    resolved = reuse(registry, initial, template(registry, original))
    freeze_reuse(registry, initial, resolved)
    tree = get(api, base(api, registry) + "/tree")
    root = next(item for item in tree["items"] if item["proposal_id"] == "baseline")
    assert root["test_count"] == 3 and root["test_status_counts"] == {"completed": 3}
    tests = get(api, base(api, registry) + "/candidates/baseline/tests")
    assert tests["total"] == 3 and {item["record_id"] for item in tests["items"]} == set(resolved["reused_test_ids"])
    comparisons = get(api, base(api, registry) + "/candidates/baseline/comparisons")
    assert comparisons["total"] == 2 and {item["candidate"] for item in comparisons["items"]} == {"a", "b"}
    assert sum(item["test_count"] for item in tree["items"]) == 9


def test_unknown_comparison_scope_cannot_leak_through_node_feedback(api, registry):
    root = candidate(registry[0], registry[1], registry[3])["proposal"]
    registry[0].records.put(experiment_id=registry[1], kind="comparison", record_id="unknown-comparison",
        submission_identity="unknown", value={"comparison_version": 1, "record_type": "plan", "secret": "FUTURE"},
        links=(("candidate", root["record_id"]),))
    response = api[0].get(base(api, registry) + "/candidates/baseline/comparisons")
    assert response.status_code == 403 and "FUTURE" not in response.text
    assert exposures(registry) == []
