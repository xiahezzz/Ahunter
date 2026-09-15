from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from advisor.research.artifacts import ArtifactStore
from advisor.research.catalog import ManifestCatalog
from advisor.research.codex.executor import CodexResult
from advisor.research.contracts import DataProductManifest, ExecutionPolicy, ResearchBoundary, ResearchSubject, content_hash
from advisor.research.data_products.engine import DataProductEngine, ProviderObservation, ProviderRegistry
from advisor.research.lagent import LAgentRunner, LAgentSettings, SettingsConflict, settings_view, submit, trace, update_settings
from advisor.research.repository import ResearchRepository
from advisor.research.service import ResearchService
from advisor.web.api import create_app


class Provider:
    provider_id = "fixture"
    def __init__(self, blocked=False):
        self.calls = 0
        self.blocked = blocked

    def fetch(self, request, *, dependencies=None):
        self.calls += 1
        return ProviderObservation("fixture", {"items": [{"value": 42}]}, request.boundary.as_of,
            quality_status="blocked" if self.blocked else "passed", quality_message="fixture failure" if self.blocked else None)


class Executor:
    def __init__(self):
        self.calls = []

    def execute(self, capsule, policy, *, validator, **kwargs):
        context = json.loads(capsule.manifest_path.read_text())["context"]
        agent, history = context["agent_id"], context["history"]
        self.calls.append((agent, len(history), policy.model))
        if agent == "main" and not history:
            value = {"action": "delegate", "description": "委托交叉验证", "task": "核验样本", "instructions": "用数据验证"}
        elif agent != "main" and not history:
            value = {"action": "data", "description": "读取证据", "product": "sample@1"}
        elif agent != "main" and len(history) == 1:
            value = {"action": "query", "description": "查询证据", "product": "sample@1", "operation": "items"}
        else:
            value = {"action": "finish", "description": "提交研究结果", "report": "样本数据核验完成。", "evidence": [history[-1]["evidence_hash"]]}
        return CodexResult(validator(value), content_hash(value), "fixture", 3, (), model=policy.model, usage={"input_tokens": 10})


@pytest.fixture
def runtime(tmp_path):
    repository = ResearchRepository.open(tmp_path / "state.sqlite")
    store = ArtifactStore(tmp_path / "artifacts")
    catalog = ManifestCatalog(products={"sample@1": DataProductManifest(product="sample@1", title="样本", providers=("fixture",))},
        agents={}, teams={}, pipelines={}, execution_policies={})
    provider = Provider()
    registry = ProviderRegistry(); registry.register("sample@1", provider)
    policy = ExecutionPolicy(policy="codex@1", model="fixture-model", reasoning_effort="high", timeout_seconds=10,
        max_agent_concurrency=1, max_stage_concurrency=1)
    value = SimpleNamespace(repository=repository, artifact_store=store, catalog=catalog, root=tmp_path,
        policy=policy, executor=Executor(), product_engine=DataProductEngine(catalog, registry, store, repository), provider=provider)
    yield value
    repository.close(); store.close()


def accepted(runtime, identity="test-lagent"):
    return submit(runtime.repository, catalog=runtime.catalog, subject=ResearchSubject(code="600519"),
        task="验证数据", submission_identity=identity, expected_version=settings_view(runtime.repository)["version"])


def test_free_run_delegates_queries_publishes_and_replays_without_external_calls(runtime):
    request = accepted(runtime)
    assert request.mode == "lagent"
    boundary = ResearchBoundary(as_of=request.accepted_at)
    progress = []
    runner = LAgentRunner(runtime, request, boundary, runtime.policy, lambda *args: progress.append(args), lambda: False)
    result = runner.execute()
    assert result.status == "passed", trace(runtime.repository, request.request_id)
    assert runtime.provider.calls == 1
    assert [call[0] for call in runtime.executor.calls] == ["main", "subagent-1", "subagent-1", "subagent-1", "main"]
    assert runtime.artifact_store.read_json(result.report_json_hash)["mode"] == "lagent"
    events = trace(runtime.repository, request.request_id)["events"]
    assert any(event["kind"] == "query" and event["status"] == "passed" for event in events)
    assert any(event["parent_id"] == "main" for event in events)
    assert any(event["detail"].get("usage", {}).get("input_tokens") == 10 for event in events)
    runtime.executor.calls.clear()
    recovered = LAgentRunner(runtime, request, boundary, runtime.policy, lambda *args: None, lambda: False).execute()
    assert recovered.report_json_hash == result.report_json_hash
    assert runtime.provider.calls == 1 and runtime.executor.calls == []


def test_configuration_is_versioned_pinned_and_submission_idempotent(runtime):
    request = accepted(runtime)
    config = LAgentSettings(max_subagents=0).model_dump(mode="json")
    updated = update_settings(runtime.repository, expected_version=0, config=config, catalog=runtime.catalog)
    assert updated["version"] == 1
    assert trace(runtime.repository, request.request_id)["config"]["max_subagents"] is None
    duplicate = submit(runtime.repository, catalog=runtime.catalog, subject=request.subject, task="验证数据",
        submission_identity="test-lagent", expected_version=0)
    assert duplicate.request_id == request.request_id
    with pytest.raises(SettingsConflict):
        update_settings(runtime.repository, expected_version=0, config=config, catalog=runtime.catalog)
    with pytest.raises(ValueError):
        submit(runtime.repository, catalog=runtime.catalog, subject=request.subject, task="不同任务", submission_identity="test-lagent", expected_version=0)


@pytest.mark.parametrize("kind", ["quality", "limit", "access", "query_limit"])
def test_failure_blocks_publication_and_is_observable(runtime, kind):
    options = {"max_subagents": 0} if kind == "limit" else {"allowed_products": []} if kind == "access" else {"max_queries": 0} if kind == "query_limit" else {}
    update_settings(runtime.repository, expected_version=0, config=LAgentSettings(**options).model_dump(mode="json"), catalog=runtime.catalog)
    runtime.provider.blocked = kind == "quality"
    request = accepted(runtime)
    result = LAgentRunner(runtime, request, ResearchBoundary(as_of=request.accepted_at), runtime.policy, lambda *args: None, lambda: False).execute()
    assert result.status == "blocked" and result.report_json_hash is None
    assert trace(runtime.repository, request.request_id)["events"][-1]["status"] == "blocked"


def test_queue_completion_cancellation_and_rerun_preserve_configuration(runtime):
    request = accepted(runtime)
    from advisor.research.cli import _execute_service_request
    service = ResearchService(runtime.repository,
        executor=lambda req, boundary, progress, cancelled: _execute_service_request(runtime, req, boundary, progress, cancelled, reports_root=runtime.root / "reports"))
    try:
        assert service.start()
        service.tick()
        assert runtime.repository.get_request(request.request_id).status == "passed"
        assert runtime.repository.record_for_request(request.request_id).mode == "lagent"
        with runtime.repository.transaction():
            rerun = runtime.repository.rerun_request(request.request_id, submission_identity="rerun-lagent")
        assert trace(runtime.repository, rerun.request_id)["config"] == trace(runtime.repository, request.request_id)["config"]
        with runtime.repository.transaction():
            runtime.repository.request_cancel(rerun.request_id)
        assert runtime.repository.get_request(rerun.request_id).status == "cancelled"
    finally:
        service.stop()


def test_web_settings_submit_trace_and_same_origin(tmp_path):
    from tests.advisor.test_research_web import _research_workspace
    root = _research_workspace(tmp_path)
    client = TestClient(create_app(state_dir=tmp_path / "state", db_path=tmp_path / "api.sqlite", research_root=root))
    settings = client.get("/api/research/lagent/settings").json()
    body = {"scope": "market", "code": None, "task": "自由研究", "submission_identity": "api-lagent", "expected_version": 0}
    response = client.post("/api/research/lagent/requests", json=body)
    assert response.status_code == 202, response.text
    request = response.json()["request"]
    assert request["mode"] == "lagent"
    observed = client.get(f"/api/research/lagent/requests/{request['request_id']}/trace?after=0&limit=1")
    assert observed.status_code == 200
    assert observed.json()["config"] == {**settings["config"], "mx_rids": []}
    assert client.post("/api/research/lagent/requests", json=body, headers={"Origin": "https://evil.example"}).status_code == 400
    saved = client.put("/api/research/lagent/settings", json={"expected_version": 0, "config": settings["config"]})
    assert saved.status_code == 200
    assert client.put("/api/research/lagent/settings", json={"expected_version": 0, "config": settings["config"]}).status_code == 409
    repository = ResearchRepository.open(tmp_path / "api.sqlite")
    store = ArtifactStore(root / "data/advisor/research-artifacts")
    try:
        artifact = store.put_json({"items": [{"value": 42}]})
        with repository.transaction():
            repository.connection.execute("INSERT INTO research_lagent_events(request_id,event_key,agent_id,kind,status,payload_json,created_at) VALUES (?,?,?,?,?,?,?)",
                (request["request_id"], "fixture", "main", "data", "passed", json.dumps({"artifact_hash": artifact.content_hash}), datetime.now(timezone.utc).isoformat()))
        download = client.get(f"/api/research/lagent/requests/{request['request_id']}/artifacts/{artifact.content_hash}")
        assert download.status_code == 200 and download.json() == {"items": [{"value": 42}]}
        assert client.get(f"/api/research/lagent/requests/another-request/artifacts/{artifact.content_hash}").status_code == 404
    finally:
        repository.close(); store.close()


def test_mx_authorization_is_pinned_and_cannot_be_inferred(runtime):
    (runtime.root / "config").mkdir()
    rid_file = runtime.root / "config/allowed-rids.yaml"
    rid_file.write_text("allowed_rids: [123]\n")
    request = submit(runtime.repository, catalog=runtime.catalog, subject=ResearchSubject(scope="market"),
        task="验证数据", submission_identity="mx-pinned", expected_version=0, root=runtime.root)
    rid_file.write_text("allowed_rids: [123, 456]\n")
    assert trace(runtime.repository, request.request_id)["config"]["mx_rids"] == [123]
    with pytest.raises(ValueError):
        update_settings(runtime.repository, expected_version=0, config=LAgentSettings(mx_rids=(789,)).model_dump(mode="json"), catalog=runtime.catalog, root=runtime.root)


def test_optional_intraday_failure_is_durable_and_blocks_only_when_selected(runtime):
    from advisor.research.data_products.engine import ProductUnavailable
    intraday = "whole_market_intraday_snapshot@1"
    runtime.catalog.products[intraday] = DataProductManifest(product=intraday, title="盘中快照", providers=("fixture",))
    broken = Provider(blocked=True)
    runtime.product_engine.registry.register(intraday, broken)
    request = accepted(runtime)
    boundary = ResearchBoundary(as_of=request.accepted_at)
    runner = LAgentRunner(runtime, request, boundary, runtime.policy, lambda *args: None, lambda: False)
    result = runner.execute()
    assert result.status == "passed"
    assert broken.calls == 1
    with pytest.raises(ProductUnavailable):
        runner.data(intraday, ResearchSubject(scope="market"), "main")
    assert broken.calls == 1


def test_cancellation_during_model_call_records_terminal_trace(runtime):
    from advisor.research.codex.executor import CodexExecutionError
    from advisor.research.service import ResearchServiceCancelled
    cancelled = [False]
    def execute(*args, **kwargs):
        cancelled[0] = True
        raise CodexExecutionError("cancelled", kind="cancelled", retryable=False)
    runtime.executor.execute = execute
    request = accepted(runtime)
    runner = LAgentRunner(runtime, request, ResearchBoundary(as_of=request.accepted_at), runtime.policy, lambda *args: None, lambda: cancelled[0])
    with pytest.raises(ResearchServiceCancelled):
        runner.execute()
    assert trace(runtime.repository, request.request_id)["events"][-1]["status"] == "cancelled"


def test_report_publication_uses_the_same_gate_as_report_reading(runtime):
    original = runtime.executor.execute
    def execute(*args, **kwargs):
        result = original(*args, **kwargs)
        if result.output.action == "finish":
            object.__setattr__(result.output, "report", "样本结论包含不可发布路径 file:///tmp/fixture")
        return result
    runtime.executor.execute = execute
    request = accepted(runtime)
    result = LAgentRunner(runtime, request, ResearchBoundary(as_of=request.accepted_at), runtime.policy, lambda *args: None, lambda: False).execute()
    assert result.status == "blocked" and result.report_json_hash is None
