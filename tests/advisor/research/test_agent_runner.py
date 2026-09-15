from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from advisor.research.catalog import ManifestCatalog
from advisor.research.artifacts import ArtifactStore
from advisor.research.codex.executor import CodexAttempt, CodexResult
from advisor.research.contracts import (
    AgentManifest,
    DataProductManifest,
    EvidenceRef,
    ExecutionPolicy,
    ResearchBoundary,
    ResearchFinding,
    ResearchScope,
    ResearchSubject,
    ResearchTeam,
    VersionRef,
    content_hash,
)
from advisor.research.data_products.engine import ProductResult, SnapshotResult
from advisor.research.agents.runner import (
    AgentFindingError,
    AgentRunner,
    _validate_market_finding,
    _validate_query_plan,
    invocation_key,
)


def _catalog() -> ManifestCatalog:
    return ManifestCatalog(
        products={"bars@1": DataProductManifest(product="bars@1", title="Bars", providers=("fixture",))},
        agents={
            "market@1": AgentManifest(
                agent="market@1",
                title="Market",
                instructions="Inspect the declared bars.",
                required_products=("bars@1",),
                details_schema={"type": "object", "required": ["trend"]},
                query_budget=4,
                max_result_rows=10,
            )
        },
        teams={"core@1": ResearchTeam(team="core@1", title="Core", agents=("market@1",))},
        pipelines={},
        execution_policies={},
    ).validate()


def _snapshot() -> SnapshotResult:
    subject = ResearchSubject(code="600519")
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))
    product = ProductResult(
        product=VersionRef.parse("bars@1"),
        payload={"rows": [{"close": 10}]},
        artifact_hash="a" * 64,
        provider="fixture",
        quality_status="passed",
        quality_message=None,
        attempts=(),
    )
    return SnapshotResult("snapshot-1", subject, boundary, {"bars@1": product}, "b" * 64)


class FakeExecutor:
    def __init__(self, output_factory):
        self.output_factory = output_factory
        self.calls = []
        self.prompts = []
        self.schemas = []

    def execute(self, capsule, policy, *, validator=None, **kwargs):
        self.calls.append(capsule)
        self.prompts.append(kwargs.get("prompt", ""))
        import json

        self.schemas.append(json.loads(capsule.root.joinpath("output-schema.json").read_text(encoding="utf-8")))
        output = self.output_factory(capsule)
        parsed = validator(output) if validator is not None else output
        return CodexResult(parsed, "c" * 64, "fake-1", 1, ())


class RetryingValidatorExecutor:
    """Small executor double that retries values rejected by its validator."""

    def __init__(self, output_factory):
        self.output_factory = output_factory
        self.validation_attempts = 0

    def execute(self, capsule, policy, *, validator=None, **kwargs):
        assert validator is not None
        for output in self.output_factory(capsule):
            self.validation_attempts += 1
            try:
                parsed = validator(output)
            except ValueError:
                continue
            return CodexResult(parsed, "c" * 64, "fake-1", 1, ())
        raise AssertionError("validator rejected every test output")


class QueryPlanningExecutor:
    supports_host_query_planning = True

    def __init__(self, *, with_inline=False, over_budget=False, repair_valid=True):
        self.manifests = []
        self.prompts = []
        self.with_inline = with_inline
        self.over_budget = over_budget
        self.repair_valid = repair_valid

    def execute(self, capsule, policy, *, validator=None, **kwargs):
        import json

        manifest = json.loads(capsule.manifest_path.read_text(encoding="utf-8"))
        self.manifests.append(manifest)
        self.prompts.append(kwargs.get("prompt", ""))
        if manifest["label"].endswith("-query-repair"):
            assert manifest["context"]["remaining_budget"]["rows"] == 1
            assert manifest["context"]["remaining_budget"]["queries"] == 1
            output = {"queries": [{
                "query_id": "sample", "product": "whole_market_daily_history@1",
                "operation": "filter",
                "params_json": '{"start_date":"2026-08-05","end_date":"2026-08-05"}'
                if self.repair_valid else '{"start_date":"2026-08-04","end_date":"2026-08-05"}',
            }]}
        elif manifest["label"].endswith("-query-plan"):
            output = {
                "queries": [
                    {
                        "query_id": "mean-close",
                        "product": "whole_market_daily_history@1",
                        "operation": "aggregate",
                        "params_json": '{"metric":"mean","value_field":"close"}',
                    }
                ]
            }
            if self.over_budget:
                output["queries"].append({
                    "query_id": "sample", "product": "whole_market_daily_history@1",
                    "operation": "filter", "params_json": '{"start_date":"2026-08-04","end_date":"2026-08-05"}',
                })
        else:
            query_result = manifest["context"]["query_results"][0]
            assert query_result["result"] == {
                "metric": "mean",
                "value_field": "close",
                "count": 2,
                "value": 15.0,
            }
            evidence_id = query_result["evidence_id"]
            if self.over_budget:
                results = manifest["context"]["query_results"]
                assert len(results) == 2
                assert len(results[1]["result"]) == 1
            output = {
                "agent": {"id": "market_breadth", "version": 1},
                "subject": {"scope": "market", "code": None, "name": None},
                "boundary": {"as_of": manifest["as_of"]},
                "summary": "历史数据表明，全市场样本均价为十五元。",
                "claims": [
                    {
                        "claim_id": "mean-close",
                        "statement": "固定样本的平均收盘价为十五元。",
                        "evidence_ids": [evidence_id],
                    }
                ],
                "evidence": [
                    {
                        "evidence_id": evidence_id,
                        "source": "受审计查询结果",
                        "locator": "query://mean-close",
                        "observed_at": manifest["as_of"],
                        "excerpt": "平均收盘价为15。",
                    }
                ],
                "risks": ["样本只覆盖两个观测值。"],
                "invalidation_conditions": ["扩大时间窗口后均值明显变化。"],
                "quality": {"status": "passed", "checks": ["query-audited"], "limitations": []},
                "details": {
                    "inputs": ["全市场历史日线"],
                    "time_windows": ["固定研究边界前"],
                    "criteria": "计算收盘价算术平均值。",
                },
            }
        if self.with_inline and not manifest["label"].endswith("-query-plan"):
            inline = json.loads((capsule.root / "products/whole_market_intraday_snapshot__1.json").read_text())
            assert inline["payload"]["breadth"]["up_count"] == 1
            # A final Finding must be allowed to combine declared inline
            # evidence with the separately audited historical query results.
            assert "宿主已执行查询计划；只能使用 manifest.json context.query_results" not in kwargs["prompt"]
            assert "仍可读取 products/" in kwargs["prompt"]
            inline_id = manifest["evidence_index"]["whole_market_intraday_snapshot@1"]["evidence_id"]
            output["claims"].append({
                "claim_id": "intraday", "statement": "盘中样本有一个上涨观测。",
                "evidence_ids": [inline_id],
            })
            output["evidence"].append({
                "evidence_id": inline_id, "source": "盘中快照", "locator": "artifact://intraday",
                "observed_at": manifest["as_of"], "excerpt": "上涨观测为一个。",
            })
        output_hash = content_hash(output)
        parsed = validator(output) if validator is not None else output
        attempt = CodexAttempt(1, "passed", 1, output_hash=output_hash)
        return CodexResult(parsed, output_hash, "fake-1", 1, (attempt,))


def _valid_output(capsule):
    import json

    manifest = json.loads(capsule.manifest_path.read_text(encoding="utf-8"))
    evidence_id = manifest["evidence_index"]["bars@1"]["evidence_id"]
    as_of = manifest["as_of"]
    return {
        "agent": {"id": "market", "version": 1},
        "subject": {"code": "600519", "name": None},
        "boundary": {"as_of": as_of},
        "summary": "样例数据显示，价格趋势向上。",
        "claims": [{"claim_id": "trend", "statement": "最新收盘价为十元。", "evidence_ids": [evidence_id]}],
        "evidence": [
            {
                "evidence_id": evidence_id,
                "source": "fixture",
                "locator": "artifact://bars",
                "observed_at": as_of,
                "excerpt": "最新收盘价为10元。",
            }
        ],
        "risks": ["样本数量较少，结论可能不稳定。"],
        "invalidation_conditions": ["后续收盘价跌破参考位置。"],
        "quality": {"status": "passed", "checks": ["evidence-linked"], "limitations": []},
        "details": {"trend": "up"},
    }


def _market_catalog() -> ManifestCatalog:
    return ManifestCatalog(
        products={
            "whole_market_intraday_snapshot@1": DataProductManifest(
                product="whole_market_intraday_snapshot@1",
                title="Whole Market Intraday",
                providers=("fixture",),
            )
        },
        agents={
            "market_macro_policy@1": AgentManifest(
                agent="market_macro_policy@1",
                scope="market",
                title="Market Macro Policy",
                instructions="Only discuss the whole market.",
                required_products=("whole_market_intraday_snapshot@1",),
                details_schema={
                    "type": "object",
                    "required": ["inputs", "time_windows", "criteria"],
                },
            )
        },
        teams={
            "overview@1": ResearchTeam(
                team="overview@1",
                scope="market",
                title="Overview",
                agents=("market_macro_policy@1",),
            )
        },
        pipelines={},
        execution_policies={},
    ).validate()


def _market_snapshot() -> SnapshotResult:
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))
    return SnapshotResult(
        "market-snapshot",
        ResearchSubject(scope="market"),
        boundary,
        {
            "whole_market_intraday_snapshot@1": ProductResult(
                product=VersionRef.parse("whole_market_intraday_snapshot@1"),
                payload={"rows": [{"code": "600519", "name": "贵州茅台"}]},
                artifact_hash="a" * 64,
                provider="fixture",
                quality_status="passed",
                quality_message=None,
                attempts=(),
            )
        },
        "b" * 64,
    )


def _market_outputs(capsule):
    import json

    manifest = json.loads(capsule.manifest_path.read_text(encoding="utf-8"))
    evidence_id = manifest["evidence_index"]["whole_market_intraday_snapshot@1"]["evidence_id"]
    as_of = manifest["as_of"]
    base = {
        "agent": {"id": "market_macro_policy", "version": 1},
        "subject": {"scope": "market", "code": None, "name": None},
        "boundary": {"as_of": as_of},
        "summary": "市场交投保持稳定。",
        "claims": [
            {
                "claim_id": "market",
                "statement": "全市场价格表现保持稳定。",
                "evidence_ids": [evidence_id],
            }
        ],
        "evidence": [
            {
                "evidence_id": evidence_id,
                "source": "fixture",
                "locator": "artifact://whole-market-intraday",
                "observed_at": as_of,
                "excerpt": "固定边界内的全市场快照。",
            }
        ],
        "risks": ["后续政策变化可能改变市场判断。"],
        "invalidation_conditions": ["市场整体价格结构明显改变。"],
        "quality": {"status": "passed", "checks": ["evidence-linked"], "limitations": []},
        "details": {
            "inputs": ["固定全市场快照"],
            "time_windows": ["当前边界"],
            "criteria": "仅判断全市场状态。",
        },
    }
    invalid = {**base, "summary": "建议买入600519。"}
    return invalid, base


def _policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        policy="codex@1",
        model="gpt-test",
        reasoning_effort="medium",
        timeout_seconds=30,
        max_agent_concurrency=2,
        max_stage_concurrency=2,
    )


def test_agent_runner_builds_only_declared_inputs_and_validates_evidence(tmp_path: Path):
    executor = FakeExecutor(_valid_output)
    runner = AgentRunner(_catalog(), executor)
    result = runner.run("market@1", _snapshot(), _policy())

    assert result.finding.agent == VersionRef.parse("market@1")
    assert result.finding.details == {"trend": "up"}
    assert executor.calls[0].declared_products == ("bars@1",)
    assert '"agent" 字段必须原样返回 "market@1"' in executor.prompts[0]
    assert "所有面向读者的文本必须使用简体中文" in executor.prompts[0]
    assert "使用短句和日常表达" in executor.prompts[0]
    schema = executor.schemas[0]
    assert schema["properties"]["agent"]["properties"]["id"]["enum"] == ["market"]
    assert schema["properties"]["agent"]["properties"]["version"]["enum"] == [1]
    assert schema["properties"]["subject"]["properties"]["code"]["enum"] == ["600519"]
    assert schema["properties"]["boundary"]["properties"]["as_of"]["enum"] == [
        "2026-08-06T08:30:00+00:00"
    ]
    assert invocation_key("market@1", _snapshot(), _policy()) == result.invocation_key


def test_agent_runner_rejects_evidence_outside_snapshot(tmp_path: Path):
    def output_with_unknown_evidence(capsule):
        output = _valid_output(capsule)
        output["claims"][0]["evidence_ids"] = ["product:not-in-snapshot"]
        output["evidence"][0]["evidence_id"] = "product:not-in-snapshot"
        return output

    runner = AgentRunner(_catalog(), FakeExecutor(output_with_unknown_evidence))
    with pytest.raises(AgentFindingError, match="Snapshot evidence"):
        runner.run("market@1", _snapshot(), _policy())


def test_agent_runner_reports_expected_and_returned_agent_refs():
    def output_with_wrong_agent(capsule):
        output = _valid_output(capsule)
        output["agent"] = {"id": "other", "version": 1}
        return output

    runner = AgentRunner(_catalog(), FakeExecutor(output_with_wrong_agent))
    with pytest.raises(AgentFindingError, match=r"other@1.*market@1"):
        runner.run("market@1", _snapshot(), _policy())


def test_blocked_finding_preserves_limitations_without_retrying_to_force_pass():
    from advisor.research.codex.executor import CodexExecutor

    class BlockedExecutor(CodexExecutor):
        calls = 0
        def _resolve_executable(self, policy):
            return Path('fake')
        def preflight(self, policy, **kwargs):
            return 'fake'
        def _invoke(self, executable, policy, capsule, prompt, **kwargs):
            import json
            self.calls += 1
            value = _valid_output(capsule)
            value['quality'] = {'status': 'blocked', 'limitations': ['成交额全部缺失，无法比较。']}
            return json.dumps(value)

    executor = BlockedExecutor()
    runner = AgentRunner(_catalog(), executor)
    with pytest.raises(AgentFindingError, match='成交额全部缺失') as error:
        runner.run('market@1', _snapshot(), _policy())
    assert executor.calls == 1
    assert error.value.error_class == 'quality_blocked'
    assert '成交额全部缺失' in error.value.attempts[0].error_message


def test_agent_runner_rejects_english_only_reader_text():
    def output_in_english(capsule):
        output = _valid_output(capsule)
        output["summary"] = "The fixture trend is upward."
        return output

    runner = AgentRunner(_catalog(), FakeExecutor(output_in_english))
    with pytest.raises(AgentFindingError, match="简体中文"):
        runner.run("market@1", _snapshot(), _policy())


def test_agent_runner_retries_a_schema_valid_market_finding_that_fails_semantic_safety():
    executor = RetryingValidatorExecutor(_market_outputs)
    runner = AgentRunner(_market_catalog(), executor)

    result = runner.run("market_macro_policy@1", _market_snapshot(), _policy())

    assert executor.validation_attempts == 2
    assert result.finding.summary == "市场交投保持稳定。"


@pytest.mark.parametrize("mode", ["history", "inline", "repair", "repair_exhausted", "repair_disabled"])
def test_query_backed_codex_execution_plans_then_runs_host_queries_before_final_finding(tmp_path: Path, mode):
    with_inline = mode == "inline"
    over_budget = mode.startswith("repair")
    store = ArtifactStore(tmp_path / "artifacts")
    history_payload = {
        "status": "passed",
        "rows": [
            {"code": "600001", "trade_date": "2026-08-04", "close": 10},
            {"code": "600002", "trade_date": "2026-08-05", "close": 20},
        ],
        "absences": [],
        "securities": [],
        "sessions": ["2026-08-04", "2026-08-05"],
    }
    history_ref = store.put_json(history_payload, media_type="application/json")
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))
    snapshot = SnapshotResult(
        "market-query-snapshot",
        ResearchSubject(scope="market"),
        boundary,
        {
            "whole_market_daily_history@1": ProductResult(
                product=VersionRef.parse("whole_market_daily_history@1"),
                payload=history_payload,
                artifact_hash=history_ref.content_hash,
                provider="fixture",
                quality_status="passed",
                quality_message=None,
                attempts=(),
            )
        },
        "b" * 64,
    )
    catalog = ManifestCatalog(
        products={
            "whole_market_daily_history@1": DataProductManifest(
                product="whole_market_daily_history@1",
                title="Whole Market Daily History",
                providers=("fixture",),
            )
        },
        agents={
            "market_breadth@1": AgentManifest(
                agent="market_breadth@1",
                scope="market",
                title="Market Breadth",
                instructions="Choose and disclose a bounded historical aggregation.",
                required_products=("whole_market_daily_history@1",),
                details_schema={
                    "type": "object",
                    "required": ["inputs", "time_windows", "criteria"],
                },
                query_budget=2,
                max_result_rows=10,
                max_result_bytes=20_000,
            )
        },
        teams={
            "market@1": ResearchTeam(
                team="market@1",
                scope="market",
                title="Market",
                agents=("market_breadth@1",),
            )
        },
        pipelines={},
        execution_policies={},
    ).validate()
    if with_inline:
        ref = "whole_market_intraday_snapshot@1"
        payload = {"status": "passed", "breadth": {"up_count": 1}}
        inline_ref = store.put_json(payload)
        snapshot.products[ref] = ProductResult(
            product=VersionRef.parse(ref), payload=payload, artifact_hash=inline_ref.content_hash,
            provider="fixture", quality_status="passed", quality_message=None, attempts=(),
        )
        catalog.products[ref] = DataProductManifest(product=ref, title="盘中快照", providers=("fixture",))
        catalog.agents["market_breadth@1"] = AgentManifest(
            agent="market_breadth@1", scope="market", title="Market Breadth",
            instructions="结合历史均值与当前盘中广度。",
            required_products=("whole_market_daily_history@1", ref),
            query_budget=2, max_result_rows=10, max_result_bytes=20_000,
        )
        catalog.validate()
    if over_budget:
        original = catalog.agents["market_breadth@1"]
        catalog.agents["market_breadth@1"] = original.model_copy(update={"query_budget": 2, "max_result_rows": 2, "max_result_bytes": 20_000})
        catalog.validate()
    executor = QueryPlanningExecutor(with_inline=with_inline, over_budget=over_budget, repair_valid=mode != "repair_exhausted")

    if mode in {"repair_exhausted", "repair_disabled"}:
        policy = _policy().model_copy(update={"max_retries": 0}) if mode == "repair_disabled" else _policy()
        with pytest.raises(AgentFindingError, match="row budget") as failed:
            AgentRunner(catalog, executor, artifact_store=store).run("market_breadth@1", snapshot, policy)
        assert len([m for m in executor.manifests if m["label"].endswith("-query-repair")]) == (0 if mode == "repair_disabled" else 1)
        assert len(failed.value.attempts) >= (2 if mode == "repair_disabled" else 3)
        assert failed.value.query_log_hash is not None
        audit = store.read_bytes(failed.value.query_log_hash).decode().splitlines()
        assert len(audit) == 1
        return

    result = AgentRunner(catalog, executor, artifact_store=store).run(
        "market_breadth@1", snapshot, _policy()
    )

    if over_budget:
        assert [m["label"] for m in executor.manifests] == [
            "research-agent-market_breadth@1-query-plan",
            "research-agent-market_breadth@1-query-repair",
            "research-agent-market_breadth@1",
        ]
        assert len(result.query_log) == 2
        assert result.query_log[-1]["total_returned_rows"] == 2
        assert result.query_log[-1]["total_query_count"] == 2
        assert [attempt.phase for attempt in result.attempts] == ["query_plan", "query_execution", "query_repair", "finding"]
        assert result.attempts[1].status != "passed"
        assert all(store.verify(a.capsule_hash) for a in result.attempts)
        return
    assert [manifest["label"] for manifest in executor.manifests] == [
        "research-agent-market_breadth@1-query-plan", "research-agent-market_breadth@1",
    ]
    assert executor.manifests[0]["query_backed_products"] == []
    assert executor.manifests[1]["query_backed_products"] == []
    assert "query_results" in executor.manifests[1]["context"]
    query_capsule_hash = executor.manifests[1]["context"]["query_audit"]["query_capsule_hash"]
    assert store.read_json(query_capsule_hash)["query_backed_products"] == [
        "whole_market_daily_history@1"
    ]
    assert "宿主已执行" in executor.prompts[1]
    assert len(result.query_log) == 1
    assert result.query_log[0]["operation"] == "aggregate"
    assert result.query_log_hash is not None and store.verify(result.query_log_hash)
    assert result.finding.claims[0].evidence_ids[0].startswith("query:mean-close:")
    if with_inline:
        assert len(result.finding.claims) == 2
    assert [attempt.attempt_number for attempt in result.attempts] == [1, 2]
    assert [attempt.phase for attempt in result.attempts] == ["query_plan", "finding"]
    assert result.attempts[0].capsule_hash != result.attempts[1].capsule_hash
    assert all(store.verify(attempt.capsule_hash) for attempt in result.attempts)
    assert all(store.verify(attempt.output_hash) for attempt in result.attempts)

    unsupported = FakeExecutor(lambda _capsule: pytest.fail("unsafe executor must not run"))
    with pytest.raises(AgentFindingError, match="host-mediated Research Query protocol") as denied:
        AgentRunner(catalog, unsupported, artifact_store=store).run(
            "market_breadth@1", snapshot, _policy()
        )
    assert denied.value.error_class == "query_interface_unavailable"
    assert unsupported.calls == []


@pytest.mark.parametrize(
    ("queries", "message"),
    [
        (
            [
                {
                    "query_id": "bad-json",
                    "product": "history@1",
                    "operation": "aggregate",
                    "params_json": '{"metric":"mean","value_field":"close","value_field":"amount"}',
                }
            ],
            "strict JSON",
        ),
        (
            [
                {
                    "query_id": "first",
                    "product": "history@1",
                    "operation": "aggregate",
                    "params_json": '{"metric":"count"}',
                },
                {
                    "query_id": "second",
                    "product": "history@1",
                    "operation": "aggregate",
                    "params_json": '{"metric":"count"}',
                },
            ],
            "duplicate request",
        ),
        (
            [
                {
                    "query_id": "too-wide",
                    "product": "history@1",
                    "operation": "rank",
                    "params_json": '{"field":"close","limit":11}',
                }
            ],
            "rank limit",
        ),
    ],
)
def test_query_plan_validation_rejects_ambiguous_or_budget_bypassing_requests(queries, message):
    with pytest.raises(ValueError, match=message):
        _validate_query_plan(
            {"queries": queries},
            queryable_products=("history@1",),
            query_budget=2,
            max_result_rows=10,
        )


def test_market_finding_validation_checks_long_nested_details_text_for_security_actions():
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))
    finding = ResearchFinding(
        agent="market_breadth@1",
        subject=ResearchSubject(scope=ResearchScope.market),
        boundary=boundary,
        summary="市场宽度结论仅供全市场观察。",
        evidence=(EvidenceRef(evidence_id="fixture", source="fixture", observed_at=boundary.as_of),),
        quality={"status": "passed"},
        details={
            "inputs": ["固定快照"],
            "time_windows": ["当前边界"],
            "criteria": {
                "reader_note": (
                    "600519 后接一段很长的方法阐述，覆盖市场宽度、成交额、行业分组和回撤验证，"
                    "长度超过旧有的短距离匹配限制。建议买入该证券。"
                ),
            },
        },
    )
    agent = AgentManifest(
        agent="market_breadth@1",
        scope=ResearchScope.market,
        title="市场宽度",
        instructions="只描述全市场观察。",
        required_products=("bars@1",),
    )

    with pytest.raises(AgentFindingError, match="security recommendation"):
        _validate_market_finding(finding, agent)


def test_agent_runner_uses_the_sealed_market_universe_to_reject_a_named_security_stance():
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))
    subject = ResearchSubject(scope=ResearchScope.market)
    snapshot = SnapshotResult(
        "market-snapshot",
        subject,
        boundary,
        {
            "whole_market_intraday_snapshot@1": ProductResult(
                product=VersionRef.parse("whole_market_intraday_snapshot@1"),
                payload={"rows": [{"code": "600519", "name": "贵州茅台"}]},
                artifact_hash="a" * 64,
                provider="fixture",
                quality_status="passed",
                quality_message=None,
                attempts=(),
            ),
        },
        "b" * 64,
    )
    finding = ResearchFinding(
        agent="market_breadth@1",
        subject=subject,
        boundary=boundary,
        summary="市场宽度结论仅供全市场观察。",
        evidence=(EvidenceRef(evidence_id="fixture", source="fixture", observed_at=boundary.as_of),),
        quality={"status": "passed"},
        details={
            "inputs": ["固定快照"],
            "time_windows": ["当前边界"],
            "criteria": "贵州茅台看多，适合继续持有。",
        },
    )
    agent = AgentManifest(
        agent="market_breadth@1",
        scope=ResearchScope.market,
        title="市场宽度",
        instructions="只描述全市场观察。",
        required_products=("whole_market_intraday_snapshot@1",),
    )

    with pytest.raises(AgentFindingError, match="security recommendation"):
        AgentRunner._validate_finding(finding, agent, snapshot, {"fixture"})


def test_agent_runner_checks_reader_visible_nested_method_keys_for_security_stances():
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))
    subject = ResearchSubject(scope=ResearchScope.market)
    snapshot = SnapshotResult(
        "market-snapshot-key",
        subject,
        boundary,
        {
            "whole_market_intraday_snapshot@1": ProductResult(
                product=VersionRef.parse("whole_market_intraday_snapshot@1"),
                payload={"rows": [{"code": "600519", "name": "贵州茅台"}]},
                artifact_hash="a" * 64,
                provider="fixture",
                quality_status="passed",
                quality_message=None,
                attempts=(),
            ),
        },
        "b" * 64,
    )
    finding = ResearchFinding(
        agent="market_breadth@1",
        subject=subject,
        boundary=boundary,
        summary="市场宽度结论仅供全市场观察。",
        evidence=(EvidenceRef(evidence_id="fixture", source="fixture", observed_at=boundary.as_of),),
        quality={"status": "passed"},
        details={
            "inputs": ["固定快照"],
            "time_windows": ["当前边界"],
            "criteria": {"贵州茅台看多，适合继续持有。": "仅作为方法备注。"},
        },
    )
    agent = AgentManifest(
        agent="market_breadth@1",
        scope=ResearchScope.market,
        title="市场宽度",
        instructions="只描述全市场观察。",
        required_products=("whole_market_intraday_snapshot@1",),
    )

    with pytest.raises(AgentFindingError, match="security recommendation"):
        AgentRunner._validate_finding(finding, agent, snapshot, {"fixture"})


def test_invocation_key_is_scoped_to_declared_product_artifacts():
    snapshot = _snapshot()
    extra = ProductResult(
        product=VersionRef.parse("extra@1"),
        payload={"value": 1},
        artifact_hash="d" * 64,
        provider="fixture",
        quality_status="passed",
        quality_message=None,
        attempts=(),
    )
    expanded = SnapshotResult(
        snapshot.snapshot_id,
        snapshot.subject,
        snapshot.boundary,
        {**snapshot.products, "extra@1": extra},
        snapshot.snapshot_hash,
    )

    assert invocation_key("market@1", snapshot, _policy(), input_products=("bars@1",)) == invocation_key(
        "market@1", expanded, _policy(), input_products=("bars@1",)
    )
    assert invocation_key("market@1", snapshot, _policy(), input_products=("bars@1",)) != invocation_key(
        "market@1", expanded, _policy(), input_products=("bars@1", "extra@1")
    )


def test_invocation_key_records_missing_declared_product_without_crashing():
    key = invocation_key("market@1", _snapshot(), _policy(), input_products=("missing@1",))
    assert key.startswith("invocation-")
