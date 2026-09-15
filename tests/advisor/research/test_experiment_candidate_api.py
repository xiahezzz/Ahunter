import base64
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from advisor.research.experiments.candidates import seal_candidate, seal_candidate_upload
from advisor.research.experiments.records import ExperimentRecords
from advisor.research.experiments.selection import ExperimentSelection
from advisor.web.api import create_app
from tests.advisor.research.test_experiment_api import api, registry, exposures
from tests.advisor.test_agent_cli import mount_api, invoke
from tests.advisor.test_research_web import _research_workspace


def b64(value):
    return base64.b64encode(value).decode("ascii")


def upload(name="baseline", parent=None):
    return {"manifest": {"source_paths": ["main.py"], "prompt_paths": ["prompt.md"],
                         "dependency_lock_paths": ["requirements.lock"], "contract_paths": ["io.json"],
                         "entrypoint": "main.py", "input_contract": "phase@1", "output_contract": "actions@1", "allowed_config": {}},
            "files_base64": {"main.py": b64(b"raise RuntimeError('must not execute on registration')\n"),
                             "prompt.md": b64("研究提示\r\n".encode()), "requirements.lock": b64(b"\x00\xff\n"),
                             "io.json": b64(b"{}\n")},
            "proposal": {"proposal_id": name, "parent_proposal_id": parent, "hypothesis": "preserve original bytes", "source": "manual"},
            "diff_base64": None, "submission_identity": "submit-" + name}


def candidate_counts(registry):
    return dict(registry[0].records.db.execute("SELECT kind,COUNT(*) FROM lagent_records WHERE kind IN "
                                             "('candidate_package','candidate_proposal','test') GROUP BY kind"))


def test_upload_and_local_capture_have_identical_package_hashes_and_original_bytes(registry, tmp_path):
    body = upload()
    root = tmp_path / "original"
    root.mkdir()
    for name, value in body["files_base64"].items(): (root / name).write_bytes(base64.b64decode(value))
    artifacts = registry[0].artifacts
    local = seal_candidate(root, body["manifest"], artifacts=artifacts)
    remote = seal_candidate_upload(body["manifest"], body["files_base64"], artifacts=artifacts)
    assert local == remote
    for item in remote.files:
        assert artifacts.read_bytes(item.content_hash) == (root / item.path).read_bytes()
    (root / "main.py").write_text("changed after capture")
    assert seal_candidate_upload(body["manifest"], body["files_base64"], artifacts=artifacts) == remote
    assert seal_candidate(root, body["manifest"], artifacts=artifacts).package_hash != remote.package_hash


def test_api_register_list_show_and_duplicate_package_preserve_distinct_lineage(api, registry):
    client, base, _ = api
    url = base + "/" + registry[1] + "/candidates"
    root = client.post(url, json=upload())
    assert root.status_code == 201 and root.json()["execution_available"] is False
    first = root.json()
    assert client.post(url, json=upload()).json() == first
    child_body = upload("branch", "baseline")
    child_body["proposal"]["source"] = "coding_task"
    child_body["diff_base64"] = b64(b"original diff\x00\xff\r\n")
    child = client.post(url, json=child_body)
    assert child.status_code == 201
    value = child.json()
    assert value["duplicate_content"] is True
    assert value["package"] == first["package"] and value["proposal"]["record_id"] != first["proposal"]["record_id"]
    assert candidate_counts(registry) == {"candidate_package": 1, "candidate_proposal": 2}
    shown = client.get(url + "/branch").json()
    assert shown["package"] == first["package"] and shown["proposal"] == value["proposal"]
    assert shown["links"] == [{"relation": "package", "target_id": first["package"]["record_id"]},
                              {"relation": "parent", "target_id": first["proposal"]["record_id"]}]
    assert exposures(registry) == []
    diff_hash = value["proposal"]["value"]["proposal"]["diff_artifact_hash"]
    result = client.post(base + "/" + registry[1] + "/records/" + value["proposal"]["record_id"] + "/artifacts/" + diff_hash,
                         json={"audit_identity": "download-diff"})
    assert result.content == b"original diff\x00\xff\r\n"
    page = client.get(url, params={"limit": 1}).json()
    assert page["items"] == [first["proposal"]]
    assert client.post(url, json=upload("later", "branch")).status_code == 201
    second = client.get(url, params={"limit": 1, "cursor": page["next_cursor"]}).json()
    assert second["items"] == [value["proposal"]] and second["next_cursor"] is None
    assert len(client.get(url).json()["items"]) == 3
    assert client.get(url, params={"cursor": "bad"}).status_code == 400
    assert client.get(url, params={"limit": 0}).status_code == 422


@pytest.mark.parametrize("changed", ["bytes", "hypothesis", "diff", "identity"])
def test_registration_conflicts_never_overwrite_original_candidate(api, registry, changed):
    client, base, _ = api
    url = base + "/" + registry[1] + "/candidates"
    body = upload()
    first = client.post(url, json=body).json()
    if changed == "bytes": body["files_base64"]["main.py"] = b64(b"changed code")
    elif changed == "hypothesis": body["proposal"]["hypothesis"] = "changed explanation"
    elif changed == "diff": body["diff_base64"] = b64(b"changed diff")
    else: body["submission_identity"] = "another-registration"
    assert client.post(url, json=body).status_code == 409
    assert client.get(url + "/baseline").json()["proposal"] == first["proposal"]
    assert candidate_counts(registry) == {"candidate_package": 1, "candidate_proposal": 1}


@pytest.mark.parametrize("bad", ["missing_file", "extra_file", "base64", "base64_padding", "nonstring", "diff", "self_parent",
                                  "platform_config", "package_hash", "missing_kind", "entrypoint", "path", "nul", "prefix", "proposal_name"])
def test_invalid_upload_is_rejected_without_candidate_records(api, registry, bad):
    client, base, _ = api
    body = upload()
    if bad == "missing_file": body["files_base64"].pop("prompt.md")
    elif bad == "extra_file": body["files_base64"]["unlisted.py"] = b64(b"extra")
    elif bad == "base64": body["files_base64"]["prompt.md"] = "not base64!"
    elif bad == "base64_padding": body["files_base64"]["prompt.md"] = "Zh=="  # Decodes like Zg==, but has nonzero padding bits.
    elif bad == "nonstring": body["files_base64"]["main.py"] = 3
    elif bad == "diff": body["diff_base64"] = "bad diff"
    elif bad == "self_parent": body["proposal"]["parent_proposal_id"] = "baseline"
    elif bad == "platform_config": body["manifest"]["allowed_config"]["budget"] = 0
    elif bad == "package_hash": body["proposal"]["package_hash"] = "0" * 64
    elif bad == "missing_kind": body["manifest"]["contract_paths"] = []
    elif bad == "entrypoint": body["manifest"]["entrypoint"] = "prompt.md"
    elif bad == "proposal_name": body["proposal"]["proposal_id"] = "invalid name"
    else:
        path = "../outside.py" if bad == "path" else "nul\x00.py" if bad == "nul" else "prompt.md/main.py"
        body["files_base64"][path] = body["files_base64"].pop("main.py")
        body["manifest"]["source_paths"] = [path]
        body["manifest"]["entrypoint"] = path
    result = client.post(base + "/" + registry[1] + "/candidates", json=body)
    assert result.status_code == 400
    assert candidate_counts(registry) == {}


def test_bad_file_capture_does_not_seal_an_earlier_valid_file(registry):
    body = upload()
    body["files_base64"]["requirements.lock"] = "bad"
    artifacts = registry[0].artifacts
    before = set(artifacts.root.glob("*/*"))
    with pytest.raises(ValueError): seal_candidate_upload(body["manifest"], body["files_base64"], artifacts=artifacts)
    assert set(artifacts.root.glob("*/*")) == before


def test_parent_scope_and_frozen_selection_remain_enforced(api, registry):
    client, base, _ = api
    url = base + "/" + registry[1] + "/candidates"
    assert client.post(url, json=upload("orphan", "missing")).status_code == 404
    assert candidate_counts(registry) == {}
    first = client.post(url, json=upload()).json()
    other = client.post(base, json={"config": {}, "submission_identity": "another-experiment"}).json()["experiment_id"]
    other_url = base + "/" + other + "/candidates"
    assert client.post(other_url, json=upload("child", "baseline")).status_code == 404
    assert client.get(other_url + "/baseline").status_code == 404
    selector = ExperimentSelection(registry[0].records)
    initial = selector.initialize(registry[1], registry[2]["record_id"], first["proposal"]["record_id"])
    selector.finalize(registry[1], expected_selection_id=initial["record_id"], submission_identity="freeze")
    assert client.post(url, json=upload("after-freeze", "baseline")).status_code == 409
    assert client.post(url, json=upload()).json()["proposal"] == first["proposal"]
    assert candidate_counts(registry) == {"candidate_package": 1, "candidate_proposal": 1}


@pytest.mark.parametrize("after_commit", [False, True])
def test_storage_failure_retries_the_same_proposal_without_partial_registration(api, registry, monkeypatch, after_commit):
    client, base, _ = api
    url = base + "/" + registry[1] + "/candidates"
    original = ExperimentRecords.__init__
    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        def fail(point):
            if point == ("after_commit" if after_commit else "after_record"):
                raise sqlite3.OperationalError("registration reply lost")
        self.fault = fail
    monkeypatch.setattr(ExperimentRecords, "__init__", initialize)
    assert client.post(url, json=upload()).status_code == 503
    assert candidate_counts(registry) == ({"candidate_package": 1, "candidate_proposal": 1} if after_commit else {})
    monkeypatch.setattr(ExperimentRecords, "__init__", original)
    first = client.post(url, json=upload())
    assert first.status_code == 201 and client.post(url, json=upload()).json() == first.json()
    assert candidate_counts(registry) == {"candidate_package": 1, "candidate_proposal": 1}


def test_candidate_show_detects_original_byte_corruption_without_exposing_unscoped_records(api, registry):
    client, base, _ = api
    url = base + "/" + registry[1] + "/candidates"
    created = client.post(url, json=upload()).json()
    file = created["package"]["value"]["package"]["files"][0]
    registry[0].artifacts._path_for(file["content_hash"]).write_bytes(b"damaged")
    assert client.get(url + "/baseline").status_code == 409
    assert client.get(url).status_code == 200  # Listing original proposal metadata does not load code bytes.
    assert exposures(registry) == []


def test_cli_registration_round_trips_bytes_and_reads_local_proposal_ids(api, registry, monkeypatch, capsys, tmp_path):
    client, _, _ = api
    mount_api(monkeypatch, client)
    body = upload("candidate@1")
    path = tmp_path / "upload.json"
    path.write_text(json.dumps(body, ensure_ascii=False))
    prefix = ["research", "experiments", "candidates"]
    code, result = invoke(capsys, [*prefix, "register", registry[1], "--json", str(path)])
    assert code == 0 and result["status"] == 201 and result["data"]["execution_available"] is False
    code, shown = invoke(capsys, [*prefix, "show", registry[1], "candidate@1"])
    assert code == 0 and shown["data"]["proposal"] == result["data"]["proposal"]
    code, page = invoke(capsys, [*prefix, "list", registry[1], "--limit", "1"])
    assert code == 0 and page["data"]["items"] == [result["data"]["proposal"]]
    for file in shown["data"]["package"]["value"]["package"]["files"]:
        assert registry[0].artifacts.read_bytes(file["content_hash"]) == base64.b64decode(body["files_base64"][file["path"]])


def test_first_candidate_creates_artifact_store_in_actual_app_without_execution(tmp_path):
    root = _research_workspace(tmp_path)
    path = tmp_path / "api.sqlite"
    app = create_app(state_dir=tmp_path / "state", db_path=path, research_root=root,
                     research_config_path=root / "config/advisor.yaml")
    marker = tmp_path / "must-not-run"
    body = upload()
    body["files_base64"]["main.py"] = b64(f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n".encode())
    with TestClient(app) as client:
        base = "/api/research/experiments"
        experiment = client.post(base, json={"config": {}, "submission_identity": "draft"}).json()["experiment_id"]
        assert not (root / "data/advisor/research-artifacts").exists()
        response = client.post(base + "/" + experiment + "/candidates", json=body)
        assert response.status_code == 201
        assert client.get(base + "/" + experiment + "/candidates/baseline").status_code == 200
    assert not marker.exists()
    with sqlite3.connect(path) as db:
        for table in ("research_requests", "research_work_queue", "ledger_transactions"):
            assert db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='test'").fetchone()[0] == 0
