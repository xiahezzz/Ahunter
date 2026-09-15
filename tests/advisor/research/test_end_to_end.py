from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

import pytest

from advisor.research import cli as research_cli
from advisor.research.cli import build_runtime, run_daily_batch, run_research_cycle
from advisor.research.codex.executor import CodexResult
from advisor.research.contracts import AgentManifest, FindingQuality, ResearchTeam, canonical_json
from advisor.research.data_products.engine import ProductRequest, ProviderObservation, ProviderRegistry


class FixtureProvider:
    provider_id = "public-a-share"

    def fetch(self, request: ProductRequest, *, dependencies=None):
        if request.product.id == "market_daily_bars":
            payload = {
                "rows": [],
                "raw_rows": [],
                "research_price_series": {"bars": [], "factor_set_hash": "f" * 64},
                "snapshot_proof": {"session_set_hash": "s" * 64, "factor_set_hash": "f" * 64},
            }
        elif request.product.id in {"company_news", "macro_news", "mx_events", "hot_stocks", "capital_flows", "concepts", "dragon_tiger", "lockup_calendar"}:
            payload = {"items": []}
        else:
            payload = {"code": request.subject.code, "status": "passed"}
        return ProviderObservation(
            provider=self.provider_id,
            payload=payload,
            observed_at=request.boundary.as_of,
            source_locator=f"fixture://{request.product}",
        )


class LocalFixtureProvider(FixtureProvider):
    provider_id = "local-market"


class LocalMxFixtureProvider(FixtureProvider):
    provider_id = "local-mx"

    def fetch(self, request: ProductRequest, *, dependencies=None):
        window = {
            "start_exclusive": (request.boundary.as_of - timedelta(days=7)).isoformat(),
            "end_inclusive": request.boundary.as_of.isoformat(),
        }
        feeds = []
        for rid in request.feed_rids:
            material = {"rid": rid, "window": window, "items": []}
            feeds.append({
                **material,
                "event_count": 0,
                "quality": {"status": "passed", "reason": ""},
                "content_hash": hashlib.sha256(canonical_json(material)).hexdigest(),
            })
        return ProviderObservation(
            provider=self.provider_id,
            payload={"status": "passed", "feeds": feeds},
            observed_at=request.boundary.as_of,
            source_locator=f"fixture://{request.product}",
        )


def _registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    products = (
        "company_identity@1", "market_daily_bars@1", "market_quote@1",
        "fundamental_snapshot@1", "financial_statements@1", "earnings_forecast@1",
        "industry_context@1", "insider_activity@1", "company_news@1", "macro_news@1",
        "mx_events@1", "mx_events@2", "hot_stocks@1", "capital_flows@1", "concepts@1",
        "dragon_tiger@1", "lockup_calendar@1",
    )
    for product in products:
        if product == "market_daily_bars@1":
            provider = LocalFixtureProvider()
        elif product == "mx_events@2":
            provider = LocalMxFixtureProvider()
        else:
            provider = FixtureProvider()
        registry.register(product, provider)
    return registry


def _evidence(capsule: Path) -> list[dict[str, Any]]:
    manifest = json.loads((capsule / "manifest.json").read_text(encoding="utf-8"))
    index = manifest.get("evidence_index", {})
    if "allowed_evidence" in index:
        allowed = index["allowed_evidence"]
        return [
            {"evidence_id": evidence_id, "source": "fixture", "observed_at": manifest["as_of"]}
            for evidence_id in allowed[:1]
        ]
    return [
        {
            "evidence_id": value["evidence_id"],
            "source": value["provider"],
            "observed_at": value["as_of"],
        }
        for value in index.values()
        if isinstance(value, dict) and value.get("evidence_id")
    ][:1]


class FixtureCodex:
    def __init__(self):
        self.calls = []

    def preflight(self, policy):
        return "fixture-codex@1"

    def execute(self, capsule, policy, *, validator=None, **kwargs):
        self.calls.append(capsule.root)
        manifest = json.loads(capsule.manifest_path.read_text(encoding="utf-8"))
        as_of = manifest["as_of"]
        label = manifest["label"]
        if label.startswith("research-agent-"):
            agent = label.removeprefix("research-agent-")
            agent_id, agent_version = agent.split("@", 1)
            evidence = _evidence(capsule.root)
            payload = {
                "agent": {"id": agent_id, "version": int(agent_version)},
                "subject": manifest["subject"],
                "boundary": {"as_of": as_of},
                "summary": f"{agent} 专家根据样例数据形成了研究判断。",
                "claims": [],
                "evidence": evidence,
                "risks": ["样例数据覆盖范围有限。"],
                "invalidation_conditions": ["后续证据与当前判断相反。"],
                "quality": {"status": "passed"},
                "details": {},
            }
        else:
            stage = label.removeprefix("decision-")
            evidence = _evidence(capsule.root)
            quality = {"status": "passed"}
            if stage == "portfolio_manager":
                team_id, team_version = manifest["context"]["team"].split("@", 1)
                payload = {
                    "team": {
                        "id": team_id,
                        "version": int(team_version),
                    },
                    "subject": manifest["subject"],
                    "boundary": {"as_of": as_of},
                    "stance": "hold",
                    "conviction": "medium",
                    "evidence_quality": quality,
                    "thesis": "现有样例证据支持持有观察，并严格控制风险。",
                    "evidence": evidence,
                    "key_risks": ["样例数据覆盖范围有限。"],
                    "invalidation_conditions": ["后续证据与当前判断相反。"],
                    "time_horizon": "未来一到十个交易日",
                    "position_limit": "仅保持较小的观察仓位。",
                }
            elif stage == "trader":
                payload = {
                    "stage": stage,
                    "stance": "hold",
                    "conviction": "medium",
                    "thesis": "现有样例证据支持继续观察，暂不采取行动。",
                    "evidence": evidence,
                    "key_risks": ["样例数据覆盖范围有限。"],
                    "invalidation_conditions": ["后续证据与当前判断相反。"],
                    "time_horizon": "未来一到十个交易日",
                    "quality": quality,
                }
            else:
                payload = {
                    "stage": stage,
                    "summary": f"{stage} 阶段认为当前样例证据需要谨慎解读。",
                    "claims": [],
                    "evidence": evidence,
                    "risks": ["样例数据覆盖范围有限。"],
                    "invalidation_conditions": ["后续证据与当前判断相反。"],
                    "quality": quality,
                    "details": {},
                }
        output = validator(payload) if validator is not None else payload
        return CodexResult(output, "f" * 64, "fixture-codex@1", 1, ())


def test_offline_full_cycle_publishes_all_findings_and_fixed_stages(tmp_path: Path):
    executor = FixtureCodex()
    runtime = build_runtime(
        root=Path.cwd(),
        db_path=tmp_path / "research.sqlite",
        artifact_dir=tmp_path / "artifacts",
        provider_registry=_registry(),
        executor=executor,
    )
    try:
        run = run_research_cycle(
            runtime,
            code="600519",
            subject_name="Fixture",
            as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc),
            team_refs=["a_share_core@1"],
            reports_root=tmp_path / "reports",
            cycle_id="cycle-e2e-1",
        )
        assert run.cycle.status.value == "passed"
        assert len(run.cycle.agent_results) == 7
        team = run.cycle.team_results["a_share_core@1"]
        assert team.status.value == "passed"
        assert set(team.conclusion.stages) == {
            "finding_quality", "bull_review", "bear_review", "research_manager", "trader",
            "aggressive_risk", "neutral_risk", "conservative_risk", "portfolio_manager",
            "publication_quality",
        }
        report_root = run.publication.root / "teams" / "a_share_core@1"
        assert (report_root / "conclusion.json").is_file()
        assert (report_root / "report.md").is_file()
        assert (run.publication.root / "complete.json").is_file()
        indicators = run.cycle.snapshot.product("market_indicators@1")
        assert indicators.payload["algorithm_version"] == "market-indicators@1"
        assert indicators.payload["input_artifacts"]["market_daily_bars@1"] == run.cycle.snapshot.product("market_daily_bars@1").artifact_hash
        assert len(executor.calls) == 15  # seven Agents plus eight generative Decision Stages
        connection = runtime.repository.connection
        assert connection.execute("SELECT count(*) FROM research_invocations").fetchone()[0] == 7
        assert connection.execute("SELECT count(*) FROM research_invocation_attempts").fetchone()[0] == 7
        assert connection.execute("SELECT count(*) FROM research_stage_runs").fetchone()[0] == 10
        assert connection.execute("SELECT count(*) FROM research_stage_attempts").fetchone()[0] == 8
        audit = connection.execute(
            "SELECT model, reasoning_effort, policy_json FROM research_stage_attempts LIMIT 1"
        ).fetchone()
        assert audit[0] == runtime.policy.model
        assert audit[1] == runtime.policy.reasoning_effort
        assert json.loads(audit[2]) == runtime.policy.model_dump(mode="json")
        assert connection.execute("SELECT count(*) FROM research_team_conclusions").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM research_reports").fetchone()[0] == 2
        assert FindingQuality.model_validate({"status": "passed"})
    finally:
        runtime.close()


def test_restart_reuses_sealed_snapshot_findings_and_stages_without_reexecution(tmp_path: Path):
    first = FixtureCodex()
    runtime = build_runtime(
        root=Path.cwd(),
        db_path=tmp_path / "research.sqlite",
        artifact_dir=tmp_path / "artifacts",
        provider_registry=_registry(),
        executor=first,
    )
    boundary = __import__("advisor.research.contracts", fromlist=["ResearchBoundary"]).ResearchBoundary(
        as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc)
    )
    subject = __import__("advisor.research.contracts", fromlist=["ResearchSubject"]).ResearchSubject(code="600519", name="Fixture")
    try:
        first_cycle = runtime.cycle_engine.run_cycle(
            "cycle-restart-1", subject, boundary, ["a_share_core@1"], runtime.policy
        )
        assert first_cycle.status.value == "passed"
    finally:
        runtime.close()

    class NoCallsProvider(FixtureProvider):
        def fetch(self, request, *, dependencies=None):
            raise AssertionError("sealed Snapshot should avoid Provider Adapter calls")

    class NoCallsExecutor:
        def execute(self, *args, **kwargs):
            raise AssertionError("completed Finding/Stage should avoid Codex calls")

    second_registry = ProviderRegistry()
    for product in (
        "company_identity@1", "market_daily_bars@1", "market_quote@1",
        "fundamental_snapshot@1", "financial_statements@1", "earnings_forecast@1",
        "industry_context@1", "insider_activity@1", "company_news@1", "macro_news@1",
        "mx_events@1", "hot_stocks@1", "capital_flows@1", "concepts@1",
        "dragon_tiger@1", "lockup_calendar@1",
    ):
        second_registry.register(product, NoCallsProvider())
    restarted = build_runtime(
        root=Path.cwd(),
        db_path=tmp_path / "research.sqlite",
        artifact_dir=tmp_path / "artifacts",
        provider_registry=second_registry,
        executor=NoCallsExecutor(),
    )
    try:
        replay = restarted.cycle_engine.run_cycle(
            "cycle-restart-1", subject, boundary, ["a_share_core@1"], restarted.policy
        )
        assert replay.status.value == "passed"
    finally:
        restarted.close()


def test_sync_cycle_retry_recovers_a_completed_publication_before_control_plane_persistence(tmp_path: Path, monkeypatch):
    executor = FixtureCodex()
    runtime = build_runtime(
        root=Path.cwd(),
        db_path=tmp_path / "research.sqlite",
        artifact_dir=tmp_path / "artifacts",
        provider_registry=_registry(),
        executor=executor,
    )
    reports_root = tmp_path / "reports"
    cycle_id = "cycle-sync-publication-recovery"
    try:
        def interrupted_persistence(*_args, **_kwargs):
            raise KeyboardInterrupt("simulated crash before publication persistence")

        with monkeypatch.context() as patched:
            patched.setattr(research_cli, "_persist_publication", interrupted_persistence)
            with pytest.raises(KeyboardInterrupt, match="before publication persistence"):
                run_research_cycle(
                    runtime,
                    code="600519",
                    as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc),
                    team_refs=["a_share_core@1"],
                    reports_root=reports_root,
                    cycle_id=cycle_id,
                )

        report_root = reports_root / "2026-08-06" / cycle_id
        assert (report_root / "complete.json").is_file()
        calls_before_retry = len(executor.calls)
        recovered = run_research_cycle(
            runtime,
            code="600519",
            as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc),
            team_refs=["a_share_core@1"],
            reports_root=reports_root,
            cycle_id=cycle_id,
        )

        assert recovered.publication.root == report_root
        assert len(executor.calls) == calls_before_retry
        assert runtime.repository.connection.execute(
            "SELECT count(*) FROM research_reports WHERE cycle_id = ?", (cycle_id,)
        ).fetchone()[0] == 2
    finally:
        runtime.close()


def test_daily_batch_retry_recovers_completed_publication_before_control_plane_persistence(tmp_path: Path, monkeypatch):
    executor = FixtureCodex()
    runtime = build_runtime(
        root=Path.cwd(),
        db_path=tmp_path / "research.sqlite",
        artifact_dir=tmp_path / "artifacts",
        provider_registry=_registry(),
        executor=executor,
    )
    reports_root = tmp_path / "reports"
    batch_id = "batch-publication-recovery"
    try:
        def interrupted_persistence(*_args, **_kwargs):
            raise KeyboardInterrupt("simulated crash before daily publication persistence")

        with monkeypatch.context() as patched:
            patched.setattr(research_cli, "_persist_publication", interrupted_persistence)
            with pytest.raises(KeyboardInterrupt, match="before daily publication persistence"):
                run_daily_batch(
                    runtime,
                    batch_id=batch_id,
                    codes=("600519",),
                    as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc),
                    team_refs=("a_share_core@1",),
                    reports_root=reports_root,
                )

        cycle_id = f"{batch_id}-600519"
        report_root = reports_root / "2026-08-06" / cycle_id
        assert (report_root / "complete.json").is_file()
        assert runtime.repository.connection.execute(
            "SELECT status FROM research_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()[0] == "running"
        calls_before_retry = len(executor.calls)
        recovered = run_daily_batch(
            runtime,
            batch_id=batch_id,
            codes=("600519",),
            as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc),
            team_refs=("a_share_core@1",),
            reports_root=reports_root,
        )

        assert recovered.publications["600519"].root == report_root
        assert len(executor.calls) == calls_before_retry
        assert runtime.repository.connection.execute(
            "SELECT status FROM research_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()[0] == "passed"
    finally:
        runtime.close()


def test_offline_cycle_can_publish_one_team_and_block_another_without_cross_team_output(tmp_path: Path):
    class MixedCodex(FixtureCodex):
        def execute(self, capsule, policy, *, validator=None, **kwargs):
            manifest = json.loads(capsule.manifest_path.read_text(encoding="utf-8"))
            if manifest["label"] == "research-agent-blocked_agent@1":
                raise RuntimeError("fixture Agent failure")
            return super().execute(capsule, policy, validator=validator, **kwargs)

    executor = MixedCodex()
    runtime = build_runtime(
        root=Path.cwd(),
        db_path=tmp_path / "research.sqlite",
        artifact_dir=tmp_path / "artifacts",
        provider_registry=_registry(),
        executor=executor,
    )
    runtime.catalog.agents["blocked_agent@1"] = AgentManifest(
        agent="blocked_agent@1",
        title="Fixture blocked Agent",
        instructions="fixture",
        required_products=("company_identity@1",),
    )
    runtime.catalog.teams["blocked_team@1"] = ResearchTeam(
        team="blocked_team@1", title="Fixture blocked Team", agents=("blocked_agent@1",)
    )
    try:
        result = runtime.cycle_engine.run_cycle(
            "cycle-e2e-mixed",
            __import__("advisor.research.contracts", fromlist=["ResearchSubject"]).ResearchSubject(code="600519", name="Fixture"),
            __import__("advisor.research.contracts", fromlist=["ResearchBoundary"]).ResearchBoundary(
                as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc)
            ),
            ["a_share_core@1", "blocked_team@1"],
            runtime.policy,
        )
        publication = __import__("advisor.research.reporting.cycle", fromlist=["CycleReporter"]).CycleReporter(
            tmp_path / "reports", artifact_store=runtime.artifact_store
        ).publish(result)
        assert result.status.value == "passed"
        assert result.team_results["a_share_core@1"].status.value == "passed"
        assert result.team_results["blocked_team@1"].status.value == "blocked"
        assert (publication.root / "teams" / "a_share_core@1" / "conclusion.json").is_file()
        assert (publication.root / "teams" / "blocked_team@1" / "status.json").is_file()
        assert not (publication.root / "teams" / "blocked_team@1" / "conclusion.json").exists()
        index = (publication.root / "index.md").read_text(encoding="utf-8").lower()
        assert "stance" not in index and "thesis" not in index
        failed_attempt = runtime.repository.connection.execute(
            "SELECT capsule_hash, output_hash FROM research_invocation_attempts WHERE status = 'failed' LIMIT 1"
        ).fetchone()
        assert failed_attempt is not None
        assert failed_attempt[1] is None
        assert runtime.artifact_store.verify(failed_attempt[0])
    finally:
        runtime.close()
