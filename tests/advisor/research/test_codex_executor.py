from __future__ import annotations

import json
from pathlib import Path
import subprocess
import stat
import sys

import pytest

from advisor.research.capsules import build_capsule
from advisor.research.codex.executor import CodexExecutionError, CodexExecutor, _bounded_error
from advisor.research.codex.policy import CAPSULE_PERMISSION_FILESYSTEM, CAPSULE_PERMISSION_PROFILE
from advisor.research.contracts import ExecutionPolicy, ResearchBoundary, ResearchSubject


def test_cli_usage_only_keeps_numeric_counts_and_sums_completed_turns():
    from advisor.research.codex.executor import _InvocationOutput
    output = _InvocationOutput('{}', '\n'.join([
        json.dumps({"type": "item.completed", "item": {"text": "private reasoning"}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3, "secret": "hidden"}}),
        'not-json',
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": True, "cached_input_tokens": 2}}),
    ]))
    assert output.usage == {"input_tokens": 15, "output_tokens": 3, "cached_input_tokens": 2}
    assert str(output) == '{}'


def test_validation_retry_receives_bounded_correction(tmp_path):
    class CorrectableExecutor(CodexExecutor):
        def _resolve_executable(self, policy):
            return Path('fake')

        def preflight(self, policy, **kwargs):
            return 'fake'

        def _invoke(self, executable, policy, capsule, prompt, **kwargs):
            self.prompts.append(prompt)
            return '{"metric":"mean"}' if 'use mean' in prompt else '{"metric":"avg"}'

    executor = CorrectableExecutor()
    executor.prompts = []
    capsule = build_capsule(label='correction', instructions='query',
        subject=ResearchSubject(code='000001'),
        boundary=ResearchBoundary(as_of='2026-09-07T00:00:00Z'), inputs={}, output_schema={},
        query_budget=0, max_result_rows=1)
    def validate(value):
        if value['metric'] != 'mean':
            raise ValueError('query q1: invalid metric avg; use mean')
        return value
    try:
        result = executor.execute(capsule, _policy('fake'), prompt='Plan a query.', validator=validate)
        assert result.output == {'metric': 'mean'}
        assert len(executor.prompts) == 2
        assert 'use mean' in executor.prompts[-1]
    finally:
        capsule.cleanup()


def _policy(codex_path: str) -> ExecutionPolicy:
    return ExecutionPolicy(
        policy="codex@1",
        model="gpt-test",
        reasoning_effort="medium",
        timeout_seconds=30,
        max_agent_concurrency=2,
        max_stage_concurrency=2,
        max_retries=1,
        codex_path=codex_path,
    )


def _fake_codex(tmp_path: Path) -> Path:
    path = tmp_path / "fake-codex.py"
    path.write_text(
        """
#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
log = Path(__file__).with_name("args.jsonl")
with log.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")
proxy_log = Path(__file__).with_name("proxy-env.jsonl")
with proxy_log.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        key: os.environ.get(key)
        for key in ("https_proxy", "http_proxy", "all_proxy")
    }) + "\\n")
if args == ["--version"]:
    print("codex-cli fake-1.0")
    raise SystemExit(0)
if args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
    raise SystemExit(0)
if "exec" not in args:
    raise SystemExit(2)
capsule_root = Path(args[args.index("-C") + 1])
manifest = json.loads((capsule_root / "manifest.json").read_text(encoding="utf-8"))
if manifest.get("label") == "codex-preflight-probe":
    schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text(encoding="utf-8"))
    expected = {"agent", "subject", "boundary", "summary", "claims", "evidence", "risks", "invalidation_conditions", "quality", "details"}
    if set(schema.get("properties", {})) != expected or schema.get("additionalProperties") is not False:
        print("preflight probe schema must exercise the ResearchFinding contract", file=sys.stderr)
        raise SystemExit(2)
    def contains_default(value):
        if isinstance(value, dict):
            return "default" in value or any(contains_default(item) for item in value.values())
        if isinstance(value, list):
            return any(contains_default(item) for item in value)
        return False
    if contains_default(schema):
        print("preflight probe schema must not contain Pydantic defaults", file=sys.stderr)
        raise SystemExit(2)
    Path(__file__).with_name("probe-schema.json").write_text(json.dumps(schema), encoding="utf-8")
    output_path = Path(args[args.index("--output-last-message") + 1])
    output_path.write_text(json.dumps({
        "agent": {"id": "preflight", "version": 1},
        "subject": {"scope": "security", "code": "000001", "name": None},
        "boundary": {"as_of": "2026-08-06T00:00:00+00:00"},
        "summary": "preflight",
        "claims": [],
        "evidence": [],
        "risks": [],
        "invalidation_conditions": [],
        "quality": {"status": "passed", "checks": [], "limitations": []},
        "details": {},
    }), encoding="utf-8")
    raise SystemExit(0)
count_path = Path(__file__).with_name("count")
count = int(count_path.read_text()) if count_path.exists() else 0
count_path.write_text(str(count + 1))
output_path = Path(args[args.index("--output-last-message") + 1])
if count == 0:
    output_path.write_text(json.dumps({"invalid": True}), encoding="utf-8")
else:
    output_path.write_text(json.dumps({"ok": True, "attempt": count + 1}), encoding="utf-8")
raise SystemExit(0)
""".lstrip(),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _schema_rejecting_codex(tmp_path: Path) -> Path:
    path = tmp_path / "schema-rejecting-codex.py"
    path.write_text(
        """
#!/usr/bin/env python3
import json
from pathlib import Path
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli fake-1.0")
    raise SystemExit(0)
if args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
    raise SystemExit(0)
with Path(__file__).with_name("schema-rejecting-calls.jsonl").open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")
print(
    "ERROR: Invalid schema for response_format 'codex_output_schema': "
    "context=('properties', 'scope'), $ref cannot have keywords {'default'}.",
    file=sys.stderr,
)
raise SystemExit(1)
""".lstrip(),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _capsule():
    return build_capsule(
        label="agent",
        instructions="return the requested JSON",
        subject=ResearchSubject(code="600519"),
        boundary=ResearchBoundary(as_of=__import__("datetime").datetime(2026, 8, 6, tzinfo=__import__("datetime").timezone.utc)),
        inputs={"bars@1": {"rows": [{"close": 10}]}},
        output_schema={"type": "object", "required": ["ok"]},
        query_budget=1,
        max_result_rows=10,
    )


def test_executor_uses_explicit_ephemeral_capsule_only_policy_and_accepts_first_valid_retry(
    tmp_path: Path,
):
    fake = _fake_codex(tmp_path)
    capsule = _capsule()
    try:
        executor = CodexExecutor(codex_path=fake)

        def validate(value):
            if value.get("ok") is not True:
                raise ValueError("missing ok")
            return value

        result = executor.execute(capsule, _policy(str(fake)), validator=validate)

        assert result.output == {"ok": True, "attempt": 2}
        assert len(result.attempts) == 2
        assert [attempt.status for attempt in result.attempts] == ["schema_invalid", "passed"]
        calls = [json.loads(line) for line in (tmp_path / "args.jsonl").read_text(encoding="utf-8").splitlines()]
        exec_args = calls[-1]
        for required in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check"):
            assert required in exec_args
        assert "--sandbox" not in exec_args
        assert "--strict-config" in exec_args
        config_values = [
            exec_args[index + 1]
            for index, argument in enumerate(exec_args[:-1])
            if argument == "-c"
        ]
        assert f'default_permissions="{CAPSULE_PERMISSION_PROFILE}"' in config_values
        assert CAPSULE_PERMISSION_FILESYSTEM in config_values
        assert "allow_login_shell=false" in config_values
        assert exec_args[exec_args.index("--model") + 1] == "gpt-test"
        assert exec_args[exec_args.index("-C") + 1] == str(capsule.root)
        assert "--search" not in exec_args
    finally:
        capsule.cleanup()


def test_local_codex_permission_profile_reads_only_the_capsule(tmp_path: Path):
    if sys.platform != "darwin":
        pytest.skip("Codex permission-profile probe is macOS-specific")
    codex = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    if not codex.is_file():
        pytest.skip("local Codex CLI is unavailable")
    capsule = _capsule()
    sibling_capsule = _capsule()
    outside = sibling_capsule.root / "outside-secret.txt"
    outside.write_text("must-not-be-readable", encoding="utf-8")
    base = [
        str(codex),
        "sandbox",
        "-c",
        CAPSULE_PERMISSION_FILESYSTEM,
        "-P",
        CAPSULE_PERMISSION_PROFILE,
        "-C",
        str(capsule.root),
        "/bin/cat",
    ]
    try:
        allowed = subprocess.run(
            [*base, str(capsule.manifest_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        denied = subprocess.run(
            [*base, str(outside)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        if allowed.returncode == 65 and "TIOCSTI" in allowed.stderr and "unbound variable" in allowed.stderr:
            pytest.skip("local Codex sandbox profile is incompatible with this macOS sandbox-exec")
        assert allowed.returncode == 0
        assert denied.returncode != 0
        assert "must-not-be-readable" not in denied.stdout
    finally:
        capsule.cleanup()
        sibling_capsule.cleanup()


def test_executor_uses_only_policy_configured_codex_proxy_variables(monkeypatch, tmp_path: Path):
    proxy = "http://127.0.0.1:7897"
    monkeypatch.setenv("https_proxy", "http://untrusted.example:8080")
    monkeypatch.setenv("http_proxy", "http://untrusted.example:8080")
    monkeypatch.setenv("all_proxy", "socks5://untrusted.example:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://untrusted.example:8080")
    fake = _fake_codex(tmp_path)
    capsule = _capsule()
    try:
        policy = ExecutionPolicy.model_validate(
            {
                **_policy(str(fake)).model_dump(),
                "proxy_environment": {
                    "https_proxy": proxy,
                    "http_proxy": proxy,
                    "all_proxy": "socks5://127.0.0.1:7897",
                },
            }
        )
        CodexExecutor(codex_path=fake).execute(
            capsule,
            policy,
            validator=lambda value: value if value.get("ok") is True else (_ for _ in ()).throw(ValueError("retry")),
        )

        environments = [
            json.loads(line)
            for line in (tmp_path / "proxy-env.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert environments
        assert all(
            environment
            == {
                "https_proxy": proxy,
                "http_proxy": proxy,
                "all_proxy": "socks5://127.0.0.1:7897",
            }
            for environment in environments
        )
    finally:
        capsule.cleanup()


def test_executor_missing_cli_is_a_blocking_error(tmp_path: Path):
    capsule = _capsule()
    try:
        with pytest.raises(CodexExecutionError) as error:
            CodexExecutor(codex_path=tmp_path / "missing-codex").execute(capsule, _policy("missing-codex"))
        assert error.value.kind == "missing_cli"
        assert not error.value.retryable
    finally:
        capsule.cleanup()


def test_executor_treats_remote_output_schema_rejection_as_non_retryable(tmp_path: Path):
    fake = _schema_rejecting_codex(tmp_path)
    capsule = _capsule()
    try:
        with pytest.raises(CodexExecutionError) as error:
            CodexExecutor(codex_path=fake).execute(capsule, _policy(str(fake)))

        assert error.value.kind == "schema_invalid"
        assert not error.value.retryable
        assert len(error.value.attempts) == 1
        calls = (tmp_path / "schema-rejecting-calls.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(calls) == 1
    finally:
        capsule.cleanup()


def test_executor_probe_checks_model_service_with_an_ephemeral_capsule(tmp_path: Path):
    fake = _fake_codex(tmp_path)
    executor = CodexExecutor(codex_path=fake)
    policy = _policy(str(fake))

    assert executor.preflight(policy, probe=True).startswith("codex-cli fake")
    schema = json.loads((tmp_path / "probe-schema.json").read_text(encoding="utf-8"))
    assert schema["$defs"]["ResearchSubject"]["properties"]["scope"] == {
        "$ref": "#/$defs/ResearchScope"
    }


def test_executor_caches_a_successful_probe_for_the_same_runtime_contract(tmp_path: Path):
    fake = _fake_codex(tmp_path)
    executor = CodexExecutor(codex_path=fake)
    policy = _policy(str(fake))

    executor.preflight(policy, probe=True)
    executor.preflight(policy, probe=True)

    calls = [json.loads(line) for line in (tmp_path / "args.jsonl").read_text(encoding="utf-8").splitlines()]
    probes = [args for args in calls if "exec" in args]
    assert len(probes) == 1


def test_model_probe_allows_slow_first_connection_but_remains_bounded(monkeypatch):
    executor = CodexExecutor(codex_path="/bin/true")
    policy = _policy("/bin/true").model_copy(update={"timeout_seconds": 900})
    observed = {}
    cancel_event = object()

    def fake_execute(capsule, probe_policy, **kwargs):
        observed["timeout_seconds"] = probe_policy.timeout_seconds
        observed["cancel_event"] = kwargs["cancel_event"]
        capsule.cleanup()

    monkeypatch.setattr(executor, "execute", fake_execute)

    executor._probe_model(policy, cancel_event=cancel_event)

    assert observed["timeout_seconds"] == 180
    assert observed["cancel_event"] is cancel_event


def test_executor_preserves_final_validator_diagnostic(tmp_path: Path):
    fake = _fake_codex(tmp_path)
    capsule = _capsule()
    try:
        policy = _policy(str(fake)).model_copy(update={"max_retries": 0})

        def validate(value):
            raise ValueError("finding.quality is missing")

        with pytest.raises(CodexExecutionError) as error:
            CodexExecutor(codex_path=fake).execute(capsule, policy, validator=validate)

        assert error.value.kind == "schema_invalid"
        assert "finding.quality is missing" in str(error.value)
    finally:
        capsule.cleanup()


def test_bounded_error_keeps_the_actionable_tail_of_cli_diagnostics():
    diagnostic = (
        "Reading prompt from stdin... OpenAI Codex v0.144.0-alpha.4 "
        "-------- workdir: /tmp/capsule mode: read-only model: gpt-5.4 "
        "ERROR: Invalid schema for response_format: object schema is missing "
        "required properties; please declare every property. "
        + "x" * 400
    )

    bounded = _bounded_error(diagnostic)

    assert len(bounded) <= CodexExecutor._ERROR_LIMIT
    assert bounded.startswith("ERROR:")
    assert "Invalid schema for response_format" in bounded
