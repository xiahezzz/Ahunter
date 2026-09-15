from __future__ import annotations

from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from requests.adapters import BaseAdapter

from advisor.cli.catalog import COMMANDS
from advisor.cli.client import ApiClient
from advisor.cli.main import build_parser, main, request_body
from advisor.cli.maintenance import REFERENCE, coverage_errors, render_reference


def test_every_web_operation_and_query_field_has_a_cli_and_current_skill_reference():
    assert coverage_errors() == []
    assert REFERENCE.read_text(encoding="utf-8") == render_reference()


def test_lagent_market_cli_serializes_nullable_code_and_configuration_version(http_api, capsys):
    base_url, calls, _reply = http_api
    result = main(["--base-url", base_url, "research", "lagent", "requests", "create", "--scope", "market",
                   "--task", "验证研究", "--expected-version", "0", "--submission-identity", "lagent-cli-test"])
    assert result == 0
    assert calls[0]["body"] == {"scope": "market", "code": None, "task": "验证研究", "expected_version": 0, "submission_identity": "lagent-cli-test"}


def test_coverage_detects_new_routes_and_query_parameters(tmp_path):
    api = tmp_path / "api.py"
    api.write_text('@app.get("/api/health")\ndef health(new_filter: str): pass\n'
                   '@app.post("/api/new-operation")\ndef operation(): pass\n')
    errors = coverage_errors(api)
    assert any("missing CLI" in error and "new-operation" in error for error in errors)
    assert any("Query parameters differ" in error and "new_filter" in error for error in errors)


@pytest.fixture
def http_api():
    calls = []
    reply = {"status": 200, "body": None, "headers": {}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def handle_request(self):
            size = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(size)
            call = {"method": self.command, "path": urlsplit(self.path).path,
                    "query": parse_qs(urlsplit(self.path).query),
                    "body": json.loads(raw) if raw else None,
                    "headers": dict(self.headers)}
            calls.append(call)
            data = reply["body"]
            if data is None:
                data = json.dumps(call).encode()
            self.send_response(reply["status"])
            self.send_header("Content-Type", reply["headers"].get("Content-Type", "application/json"))
            for key, value in reply["headers"].items():
                if key != "Content-Type":
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls, reply
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def invoke(capsys, argv):
    code = main(argv)
    output = capsys.readouterr()
    assert not (output.out and output.err)
    return code, json.loads(output.out or output.err)


def test_real_http_filters_encoding_and_proxy_bypass(http_api, monkeypatch, capsys):
    base, calls, _ = http_api
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NETRC", "/nonexistent/credentials")
    code, result = invoke(capsys, ["--base-url", base, "mx", "events", "list", "--rid", "111", "--rid", "222",
                                   "--q", "中文 & ?", "--cursor", "opaque+/=", "--has-media", "false", "--limit", "3"])
    assert code == 0
    assert result["data"]["query"] == {"rid": ["111", "222"], "q": ["中文 & ?"], "cursor": ["opaque+/="], "has_media": ["false"], "limit": ["3"]}
    assert len(calls) == 1
    assert calls[0]["body"] is None
    assert "Authorization" not in calls[0]["headers"]


def test_named_submission_preserves_code_and_identity_and_returns_accepted(http_api, capsys):
    base, calls, reply = http_api
    reply["status"] = 202
    code, result = invoke(capsys, ["research", "requests", "create", "--base-url", base,
                                   "--team-ref", "example@1", "--scope", "security", "--code", "000001", "--submission-identity", "stable-id"])
    assert code == 0 and result["status"] == 202
    assert calls[0]["body"] == {"team_ref": "example@1", "scope": "security", "code": "000001", "submission_identity": "stable-id"}
    assert calls[0]["headers"]["Content-Type"] == "application/json"
    assert len(calls) == 1  # Does not start a service, poll, or retry.
    code, _ = invoke(capsys, ["--base-url", base, "research", "requests", "create",
                              "--team-ref", "market@1", "--scope", "market", "--submission-identity", "stable-market"])
    assert code == 0 and calls[-1]["body"]["code"] is None


@pytest.mark.parametrize("status", [307, 400, 404, 409, 422, 503])
def test_http_errors_are_structured_and_never_retried_or_redirected(http_api, capsys, status):
    base, calls, reply = http_api
    reply.update(status=status, body=b'{"detail":"conflict or unavailable"}', headers={"Location": base + "/api/health"})
    code, result = invoke(capsys, ["--base-url", base, "mx", "listener", "start"])
    assert code == 4 and result["ok"] is False and result["status"] == status
    assert result["error"]["detail"]["detail"] == "conflict or unavailable"
    assert len(calls) == 1
    assert calls[0]["body"] == {}


def test_transport_failure_does_not_retry_or_print_traceback(monkeypatch, capsys):
    calls = []
    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise requests.Timeout("private transport details")
    monkeypatch.setattr(requests.Session, "request", fail)
    code, result = invoke(capsys, ["mx", "listener", "start"])
    assert code == 3 and result["error"]["code"] == "transport_error"
    assert "private" not in json.dumps(result)
    assert len(calls) == 1


def test_download_is_byte_preserving_and_does_not_overwrite(http_api, capsys, tmp_path):
    base, calls, reply = http_api
    data = b"\x89PNG\r\n\x00\xfforiginal-image"
    reply.update(body=data, headers={"Content-Type": "image/png"})
    target = tmp_path / "chart.png"
    args = ["--base-url", base, "charts", "download", "chart-1", "--output", str(target)]
    code, result = invoke(capsys, args)
    assert code == 0 and target.read_bytes() == data
    assert result["data"] == {"path": str(target), "bytes": len(data), "sha256": sha256(data).hexdigest(), "content_type": "image/png"}
    code, result = invoke(capsys, args)
    assert code == 5 and result["error"]["code"] == "output_exists"
    assert len(calls) == 1 and target.read_bytes() == data
    assert list(tmp_path.glob(".ahunter-*")) == []


def test_failed_download_leaves_no_file(http_api, capsys, tmp_path):
    base, _, reply = http_api
    reply.update(status=404, body=b'{"detail":"not found"}')
    target = tmp_path / "missing.png"
    code, _ = invoke(capsys, ["--base-url", base, "mx", "media", "download", "event", "media", "--output", str(target)])
    assert code == 4 and not target.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("argv", [
    ["--base-url", "https://example.com", "health"],
    ["--base-url", "http://127.0.0.1:8000/api", "health"],
    ["--base-url", "http://user:secret@127.0.0.1", "health"],
    ["--base-url", "http://127.0.0.1:0", "health"],
    ["--timeout", "nan", "health"],
    ["--timeout", "0", "health"],
    ["mx", "chrome", "start", "--url", "https://example.com"],
    ["mx", "events", "get", "../escape"],
    ["mx", "rids", "replace", "--version", "a" * 64],
    ["ledger", "transactions", "add", "--amount", "nan"],
    ["charts", "download", "asset"],
])
def test_invalid_inputs_fail_before_contacting_api(monkeypatch, capsys, argv):
    def unexpected(*_args, **_kwargs):
        pytest.fail("Invalid input contacted API")
    monkeypatch.setattr(requests.Session, "request", unexpected)
    code, result = invoke(capsys, argv)
    assert code == 2 and result["ok"] is False


def test_json_stdin_file_body_flags_and_instruction_file(http_api, capsys, monkeypatch, tmp_path):
    base, calls, _ = http_api
    monkeypatch.setattr("sys.stdin", io.StringIO('{"version":"' + "a" * 64 + '","rids":[]}'))
    code, _ = invoke(capsys, ["--base-url", base, "mx", "rids", "replace", "--json", "-"])
    assert code == 0 and calls[-1]["body"]["rids"] == []
    instructions = tmp_path / "instructions.md"
    instructions.write_text("分析规则\n第二行", encoding="utf-8")
    code, _ = invoke(capsys, ["--base-url", base, "research", "agents", "revise-instructions", "example@1", "--instructions-file", str(instructions)])
    assert code == 0 and calls[-1]["body"] == {"instructions": "分析规则\n第二行"}
    transaction = tmp_path / "transactions.json"
    transaction.write_text('[{"code":"000001"}]')
    code, _ = invoke(capsys, ["--base-url", base, "ledger", "import", "--json", str(transaction)])
    assert code == 0 and calls[-1]["body"] == [{"code": "000001"}]
    code, _ = invoke(capsys, ["mx", "rids", "replace", "--json", str(transaction), "--version", "a" * 64])
    assert code == 2 and len(calls) == 3


@pytest.mark.parametrize("raw", ['{"rids":[],"rids":[1]}', '{"rids":NaN}', '[]', '{bad'])
def test_bad_json_stdin_is_structured_usage_error(monkeypatch, capsys, raw):
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    code, result = invoke(capsys, ["mx", "rids", "replace", "--json", "-"])
    assert code == 2 and result["ok"] is False


def test_discovery_and_all_command_help_work_offline(capsys, monkeypatch):
    def unexpected(*_args, **_kwargs):
        pytest.fail("Discovery contacted API")
    monkeypatch.setattr(requests.Session, "request", unexpected)
    code, result = invoke(capsys, ["commands"])
    assert code == 0 and len(result["data"]["commands"]) == len(COMMANDS)
    for command in COMMANDS:
        with pytest.raises(SystemExit) as stopped:
            build_parser().parse_args([*command.command.split(), "--help"])
        assert stopped.value.code == 0
        assert command.help in capsys.readouterr().out.replace("\n", " ")


class AsgiAdapter(BaseAdapter):
    def __init__(self, client):
        self.client = client

    def send(self, request, **_kwargs):
        result = self.client.request(request.method, request.url, headers=dict(request.headers), content=request.body)
        response = requests.Response()
        response.status_code = result.status_code
        response.headers.update(result.headers)
        response._content = result.content
        response._content_consumed = True
        response.request = request
        response.url = request.url
        return response

    def close(self):
        pass


def mount_api(monkeypatch, client):
    original = ApiClient.__init__
    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.session.mount("http://", AsgiAdapter(client))
    monkeypatch.setattr(ApiClient, "__init__", initialize)


def test_real_mx_api_version_conflicts_and_explicit_chrome_separation(tmp_path, monkeypatch, capsys):
    from tests.advisor.test_mx_web import _client
    api, manager, launcher = _client(tmp_path)
    mount_api(monkeypatch, api)
    code, before = invoke(capsys, ["mx", "rids", "get"])
    assert code == 0
    version = before["data"]["version"]
    code, result = invoke(capsys, ["mx", "rids", "replace", "--version", version, "--rid", "111", "--rid", "222"])
    assert code == 0 and result["data"]["rids"] == [111, 222]
    code, conflict = invoke(capsys, ["mx", "rids", "replace", "--version", version, "--rid", "333"])
    assert code == 4 and conflict["status"] == 409
    code, _ = invoke(capsys, ["mx", "listener", "start"])
    assert code == 0 and manager.calls == ["start"] and launcher.calls == 0
    code, _ = invoke(capsys, ["mx", "chrome", "start"])
    assert code == 0 and launcher.calls == 1
    code, _ = invoke(capsys, ["mx", "listener", "stop"])
    assert code == 0 and manager.calls == ["start", "stop"] and launcher.calls == 1


def test_request_body_supports_every_command_with_json_or_named_fields(tmp_path):
    # Smoke the entire parser tree, including complete array/object bodies.
    for spec in COMMANDS:
        argv = [*spec.command.split(), *["id@1" for _ in spec.path_fields]]
        if spec.body or spec.json_only:
            sample = {f.name: ([1] if f.kind == "int" else ["sample"]) if f.repeated else
                      [] if f.kind == "array" else 1 if f.kind in {"int", "float"} else "sample" for f in spec.body}
            if spec.json_only == "array":
                sample = []
            file = tmp_path / "body.json"
            file.write_text(json.dumps(sample))
            argv += ["--json", str(file)]
        if spec.download:
            argv += ["--output", str(tmp_path / "new.png")]
        args = build_parser().parse_args(argv)
        assert args.spec == spec
        body = request_body(spec, args)
        assert body is None if spec.method == "GET" else isinstance(body, (dict, list))


def test_real_research_api_team_publication_request_dedup_cancel_and_rerun(tmp_path, monkeypatch, capsys):
    from fastapi.testclient import TestClient
    from advisor.web.api import create_app
    from tests.advisor.test_research_web import _research_workspace
    root = _research_workspace(tmp_path)
    api = TestClient(create_app(state_dir=tmp_path / "state", db_path=tmp_path / "advisor.sqlite",
                                research_root=root, research_config_path=root / "config/advisor.yaml"))
    mount_api(monkeypatch, api)
    code, team = invoke(capsys, ["research", "teams", "create", "--team-id", "cli_team", "--title", "CLI 团队",
                                 "--scope", "security", "--agent-id", "news"])
    assert code == 0 and team["status"] == 201
    ref = team["data"]["team"]["team_ref"]
    code, enabled = invoke(capsys, ["research", "daily-teams", "enable", ref])
    assert code == 0 and enabled["data"]["daily_teams"] == [ref]
    create = ["research", "requests", "create", "--team-ref", ref, "--scope", "security", "--code", "000001", "--submission-identity", "same-submission"]
    code, accepted = invoke(capsys, create)
    assert code == 0 and accepted["status"] == 202
    request_id = accepted["data"]["request"]["request_id"]
    code, repeated = invoke(capsys, create)
    assert code == 0 and repeated["data"]["request"]["request_id"] == request_id
    code, cancelled = invoke(capsys, ["research", "requests", "cancel", request_id])
    assert code == 0 and cancelled["data"]["request"]["status"] == "cancelled"
    code, status = invoke(capsys, ["research", "requests", "get", request_id])
    assert code == 0 and status["data"]["request"]["status"] == "cancelled"
    code, rerun = invoke(capsys, ["research", "requests", "rerun", request_id, "--submission-identity", "new-submission"])
    assert code == 0 and rerun["data"]["request"]["request_id"] != request_id
    code, records = invoke(capsys, ["research", "records", "list", "--status", "cancelled"])
    assert code == 0 and records["data"]["records"][0]["status"] == "cancelled"
    code, disabled = invoke(capsys, ["research", "daily-teams", "disable", ref])
    assert code == 0 and disabled["data"]["daily_teams"] == []


def test_real_ledger_api_accepts_named_numeric_fields_and_preserves_ids(tmp_path, monkeypatch, capsys):
    from fastapi.testclient import TestClient
    from advisor.db.migrate import migrate_database
    from advisor.web.api import create_app
    database = tmp_path / "advisor.sqlite"
    migrate_database(database)
    mount_api(monkeypatch, TestClient(create_app(state_dir=tmp_path / "state", db_path=database, research_root=tmp_path)))
    args = ["ledger", "transactions", "add", "--transaction-id", "cli-deposit", "--trade-date", "2026-08-06",
            "--transaction-type", "cash_deposit", "--quantity", "0", "--price", "0", "--amount", "1000", "--fees", "0"]
    code, result = invoke(capsys, args)
    assert code == 0 and result["status"] == 201
    assert result["data"]["transactions"][0]["amount"] == 1000
    code, duplicate = invoke(capsys, args)
    assert code == 4 and duplicate["status"] == 409  # Backend rejects duplicate transaction IDs.
    code, result = invoke(capsys, ["ledger", "transactions", "list"])
    assert code == 0 and len(result["data"]["transactions"]) == 1


def test_interrupted_download_removes_partial_file(http_api, monkeypatch, capsys, tmp_path):
    base, _, _ = http_api
    def interrupted(self, **kwargs):
        yield b"partial"
        raise requests.ConnectionError("disconnected")
    monkeypatch.setattr(requests.Response, "iter_content", interrupted)
    target = tmp_path / "image.png"
    code, _ = invoke(capsys, ["--base-url", base, "charts", "download", "asset", "--output", str(target)])
    assert code == 3 and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("status,expected", [(200, 5), (500, 4)])
def test_malformed_api_json_has_stable_error_output(http_api, capsys, status, expected):
    base, _, reply = http_api
    reply.update(status=status, body=b'{"detail":NaN}')
    code, result = invoke(capsys, ["--base-url", base, "health"])
    assert code == expected and result["ok"] is False
