import json
from hashlib import sha256

from advisor.cli.catalog import COMMANDS
from advisor.cli.maintenance import coverage_errors, render_reference, REFERENCE
from tests.advisor.test_agent_cli import mount_api, invoke, http_api
from tests.advisor.research.test_experiment_api import api, registry, secret_record


def test_cli_draft_preflight_and_conflict_preserve_api_business_status(api, monkeypatch, capsys, tmp_path):
    client, _, _ = api
    mount_api(monkeypatch, client)
    config = tmp_path / "draft.json"
    config.write_text(json.dumps({"config": {}, "submission_identity": "cli-draft"}))
    command = ["research", "experiments", "create", "--json", str(config)]
    code, created = invoke(capsys, command)
    assert code == 0 and created["status"] == 201
    experiment = created["data"]["experiment_id"]
    assert created["data"]["validation_errors"] and not created["data"]["execution_available"]
    assert invoke(capsys, command)[1] == created
    code, shown = invoke(capsys, ["research", "experiments", "show", experiment])
    assert code == 0 and shown["data"] == created["data"]
    code, preflight = invoke(capsys, ["research", "experiments", "preflight", experiment, "--submission-identity", "cli-preflight"])
    assert code == 0 and preflight["ok"] and preflight["status"] == 200
    assert preflight["data"]["status"] == "blocked" and preflight["data"]["return"] is None
    config.write_text(json.dumps({"config": {"name": "changed"}, "submission_identity": "cli-draft"}))
    code, conflict = invoke(capsys, command)
    assert code == 4 and conflict["status"] == 409 and not conflict["ok"]


def test_cli_audited_manifest_and_binary_download_are_exact_and_never_overwrite(api, registry, monkeypatch, capsys, tmp_path):
    client, _, _ = api
    _, record, artifact = secret_record(registry)
    mount_api(monkeypatch, client)
    prefix = ["research", "experiments", "records"]
    code, shown = invoke(capsys, [*prefix, "show", registry[1], record["record_id"], "--audit-identity", "cli-show"])
    assert code == 0 and shown["data"] == record
    output = tmp_path / "manifest.json"
    code, exported = invoke(capsys, [*prefix, "export", registry[1], record["record_id"],
                                     "--audit-identity", "cli-export", "--output", str(output)])
    assert code == 0 and json.loads(output.read_bytes())["record"] == record
    assert exported["data"]["sha256"] == sha256(output.read_bytes()).hexdigest()
    binary = tmp_path / "original.bin"
    command = [*prefix, "artifact", registry[1], record["record_id"], artifact.content_hash,
               "--audit-identity", "cli-bytes", "--output", str(binary)]
    code, download = invoke(capsys, command)
    assert code == 0 and binary.read_bytes() == b"SECRET-FUTURE-RESULT\x00\xff"
    assert download["data"]["sha256"] == artifact.content_hash
    code, duplicate = invoke(capsys, command)
    assert code == 5 and duplicate["error"]["code"] == "output_exists"
    assert binary.read_bytes() == b"SECRET-FUTURE-RESULT\x00\xff"


def test_cli_real_http_event_cursor_audit_body_and_no_retry(http_api, capsys):
    base, calls, reply = http_api
    command = ["--base-url", base, "research", "experiments", "tests", "events", "experiment", "test id",
               "--audit-identity", "read page 2", "--after", "17", "--limit", "3"]
    code, result = invoke(capsys, command)
    assert code == 0
    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "/api/research/experiments/experiment/tests/test%20id/events"
    assert calls[0]["query"] == {"after": ["17"], "limit": ["3"]}
    assert calls[0]["body"] == {"audit_identity": "read page 2"}
    reply.update(status=503, body=b'{"detail":"audit unavailable"}')
    code, failure = invoke(capsys, command)
    assert code == 4 and failure["status"] == 503 and len(calls) == 2


def test_experiment_commands_and_mutating_read_metadata_match_api_and_generated_reference():
    commands = [command for command in COMMANDS if command.command.startswith("research experiments ")]
    assert len(commands) == 32
    assert coverage_errors() == [] and REFERENCE.read_text() == render_reference()
    for command in commands:
        if any(field.name == "audit_identity" for field in command.body):
            assert command.method == "POST" and command.describe()["mutates"] is True
