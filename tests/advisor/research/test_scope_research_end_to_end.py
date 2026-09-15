from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import threading
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest
import yaml

from advisor.research.cli import _execute_service_request, build_runtime
from advisor.research.codex.executor import CodexAttempt, CodexResult
from advisor.research.contracts import ResearchBoundary, ResearchScope, ResearchSubject, content_hash
from advisor.research.data_products.engine import ProductRequest, ProviderObservation, ProviderRegistry
from advisor.research.reporting.cycle import CycleReporter
from advisor.research.service import ResearchService
from advisor.research.state_machine import ResearchCycleCancelled
from advisor.web.api import create_app


ROOT = Path(__file__).resolve().parents[3]
# The API assigns accepted_at itself.  Keep the fake execution clock just
# ahead of that real acceptance instant so the durable boundary's monotonicity
# invariant is exercised without using a real provider clock.
BOUNDARY = datetime.now(timezone.utc) + timedelta(days=1)
MARKET_TEAM = "a_share_market_overview@1"


class FixtureMarketProvider:
    def __init__(self, provider_id: str, *, forbid_calls: bool = False) -> None:
        self.provider_id = provider_id
        self.forbid_calls = forbid_calls
        self.calls = 0

    def fetch(self, request: ProductRequest, *, dependencies=None) -> ProviderObservation:
        self.calls += 1
        if self.forbid_calls:
            raise AssertionError("recovered Market Snapshot must not refetch a Provider")
        product = request.product.id
        if product == "whole_market_intraday_snapshot":
            payload = {
                "expected_universe": {"codes": ["600000"], "count": 1, "content_hash": "a" * 64},
                "observation_window": {
                    "started_at": request.boundary.as_of.isoformat(),
                    "ended_at": request.boundary.as_of.isoformat(),
                    "seconds": 0,
                    "capture_boundary_at": request.boundary.as_of.isoformat(),
                },
                "rows": [{"code": "600000", "name": "样例公司", "last": 10.0, "volume": 1000}],
                "legal_absences": [],
                "coverage": {"overall": 1.0, "unknown_gap_count": 0},
                "page_proof": {"page_count": 1, "pages": [1]},
                "content_hash": "b" * 64,
            }
        elif product == "whole_market_daily_history":
            payload = {
                "rows": [],
                "absences": [],
                "securities": ["600000"],
                "sessions": ["2026-08-06"],
                "snapshot_proof": {"fixture": True},
            }
        elif product == "industry_sector_taxonomy":
            payload = {
                "industries": [{"industry_id": "BK0001", "name": "样例行业", "members": ["600000"]}],
                "taxonomy_hash": "c" * 64,
                "coverage": 1.0,
                "age_trading_days": 0,
            }
        elif product == "market_information":
            payload = {
                "items": [],
                "schema_version": "fixture-information@1",
                "fetched_at": request.boundary.as_of.isoformat(),
            }
        else:  # pragma: no cover - the catalog list is asserted by the test path
            raise AssertionError(f"unexpected Market Product: {request.product}")
        return ProviderObservation(
            provider=self.provider_id,
            payload=payload,
            observed_at=request.boundary.as_of,
            fetched_at=request.boundary.as_of,
            source_locator=f"fixture://{request.product}",
            coverage=1.0,
            schema_version=f"fixture-{request.product}",
        )


def _registry(*, forbid_calls: bool = False) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(
        "whole_market_intraday_snapshot@1",
        FixtureMarketProvider("sina-whole-market", forbid_calls=forbid_calls),
    )
    registry.register(
        "whole_market_daily_history@1",
        FixtureMarketProvider("local-market", forbid_calls=forbid_calls),
    )
    registry.register(
        "industry_sector_taxonomy@1",
        FixtureMarketProvider("eastmoney-industry-taxonomy", forbid_calls=forbid_calls),
    )
    registry.register(
        "market_information@1",
        FixtureMarketProvider("market-information", forbid_calls=forbid_calls),
    )
    return registry


class FixtureMarketCodex:
    def __init__(
        self,
        *,
        failing_agents: set[str] | None = None,
        forbid_calls: bool = False,
        host_query_planning: bool = True,
    ) -> None:
        self.failing_agents = failing_agents or set()
        self.forbid_calls = forbid_calls
        self.supports_host_query_planning = host_query_planning
        self.calls: list[str] = []

    def preflight(self, _policy):
        return "fixture-market-codex@1"

    def execute(self, capsule, _policy, *, validator=None, **_kwargs):
        manifest = json.loads(capsule.manifest_path.read_text(encoding="utf-8"))
        agent = manifest["label"].removeprefix("research-agent-")
        if agent.endswith("-query-plan"):
            agent = agent.removesuffix("-query-plan")
            self.calls.append(f"{agent}:query-plan")
            payload = {
                "queries": [
                    {
                        "query_id": "history-count",
                        "product": "whole_market_daily_history@1",
                        "operation": "aggregate",
                        "params_json": '{"metric":"count"}',
                    }
                ]
            }
            output_hash = content_hash(payload)
            output = validator(payload) if validator is not None else payload
            return CodexResult(
                output,
                output_hash,
                "fixture-market-codex@1",
                1,
                (CodexAttempt(1, "passed", 1, output_hash=output_hash),),
            )
        self.calls.append(agent)
        if self.forbid_calls:
            raise AssertionError("recovered passed Market Finding must not invoke Codex")
        if agent in self.failing_agents:
            raise RuntimeError("fixture Agent failure")
        evidence_index = manifest["evidence_index"]
        query_results = manifest.get("context", {}).get("query_results", [])
        if query_results:
            query_result = query_results[0]
            evidence = evidence_index[f"query:{query_result['query_id']}"]
        else:
            query_result = None
            evidence = next(
                value
                for value in evidence_index.values()
                if isinstance(value, dict) and isinstance(value.get("evidence_id"), str)
            )
        details = {
            "inputs": ["固定离线快照"],
            "time_windows": ["当前研究边界"],
            "criteria": "只按本次快照的完整性和时间边界分析。",
        }
        if agent == "sector_rotation@1":
            details["grouping_or_ranking"] = "按一级行业汇总并比较相对表现。"
        payload = {
            "agent": {"id": agent.split("@", 1)[0], "version": 1},
            "subject": manifest["subject"],
            "boundary": {"as_of": manifest["as_of"]},
            "summary": "固定离线证据显示市场状态需要持续观察。",
            "claims": (
                [{
                    "claim_id": "history-count",
                    "statement": "固定边界内的历史样本数量已经核验。",
                    "evidence_ids": [evidence["evidence_id"]],
                }]
                if query_result is not None
                else []
            ),
            "evidence": [{
                "evidence_id": evidence["evidence_id"],
                "source": evidence["provider"],
                "observed_at": evidence["as_of"],
                "excerpt": "固定离线快照已经完成边界校验。",
            }],
            "risks": ["样例数据范围有限。"],
            "invalidation_conditions": ["后续同边界证据与当前观察不一致。"],
            "quality": {"status": "passed"},
            "details": details,
        }
        output_hash = content_hash(payload)
        output = validator(payload) if validator is not None else payload
        return CodexResult(
            output,
            output_hash,
            "fixture-market-codex@1",
            1,
            (CodexAttempt(1, "passed", 1, output_hash=output_hash),),
        )


class CancelOnCompletedAgentCodex(FixtureMarketCodex):
    def __init__(self, cancel_event: threading.Event) -> None:
        super().__init__()
        self.cancel_event = cancel_event

    def execute(self, capsule, policy, *, validator=None, **kwargs):
        result = super().execute(capsule, policy, validator=validator, **kwargs)
        manifest = json.loads(capsule.manifest_path.read_text(encoding="utf-8"))
        if manifest["label"] == "research-agent-market_macro_policy@1":
            # Set cancellation after one worker has a valid result but before
            # the coordinator necessarily consumes that completed Future.
            self.cancel_event.set()
        return result


def _workspace(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "workspace"
    shutil.copytree(ROOT / "config" / "research", root / "config" / "research")
    config = {
        "market": {"primary": "A股"},
        "schedule": {"premarket_time": "08:30", "review_time": "22:30"},
        "storage": {
            "database": "data/advisor/advisor.sqlite",
            "chart_dir": "data/advisor/charts",
            "profile_dir": "data/advisor/profiles",
        },
        "data_sources": {"allow_tushare": False, "free_sources": []},
        "quality": {"max_market_data_staleness_minutes": 1440, "require_trading_calendar": True},
        "research": {
            "catalog_dir": "config/research",
            "artifact_dir": "data/advisor/research-artifacts",
            "default_teams": [MARKET_TEAM],
            "execution_policy": "codex@1",
            "max_subjects": 20,
        },
    }
    config_path = root / "config" / "advisor.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return (
        root,
        config_path,
        root / "data" / "advisor" / "advisor.sqlite",
        root / "data" / "advisor" / "research-artifacts",
    )


def _service(runtime, reports_root: Path) -> ResearchService:
    return ResearchService(
        runtime.repository,
        lambda request, boundary, progress, cancelled: _execute_service_request(
            runtime,
            request,
            boundary,
            progress,
            cancelled,
            reports_root=reports_root,
        ),
        owner_id="fixture-market-service",
        clock=lambda: BOUNDARY,
    )


def test_market_request_from_api_runs_through_service_snapshot_pipeline_and_report_detail(tmp_path: Path):
    root, config_path, database, artifacts = _workspace(tmp_path)
    executor = FixtureMarketCodex()
    runtime = build_runtime(
        root=root,
        config_path=config_path,
        db_path=database,
        artifact_dir=artifacts,
        provider_registry=_registry(),
        executor=executor,
    )
    client = TestClient(
        create_app(
            state_dir=root / "data" / "advisor",
            db_path=database,
            research_root=root,
            research_config_path=config_path,
        )
    )
    try:
        submitted = client.post(
            "/api/research/requests",
            json={"team_ref": MARKET_TEAM, "scope": "market", "code": None, "submission_identity": "market-e2e"},
        )
        assert submitted.status_code == 202
        request_id = submitted.json()["request"]["request_id"]

        completed = _service(runtime, root / "reports").tick()
        assert completed is not None and completed.status == "passed"
        assert completed.boundary is not None and completed.boundary.as_of == BOUNDARY
        assert sorted(executor.calls) == [
            "market_breadth@1",
            "market_breadth@1:query-plan",
            "market_macro_policy@1",
            "sector_rotation@1",
            "sector_rotation@1:query-plan",
        ]
        assert runtime.repository.connection.execute(
            "SELECT count(*) FROM research_scope_snapshots"
        ).fetchone()[0] == 1
        assert runtime.repository.connection.execute(
            "SELECT count(*) FROM research_scope_invocations WHERE status = 'passed'"
        ).fetchone()[0] == 3
        assert runtime.repository.connection.execute(
            "SELECT count(*) FROM research_scope_invocation_attempts WHERE status = 'passed'"
        ).fetchone()[0] == 5

        records = client.get("/api/research/records")
        assert records.status_code == 200
        listed = records.json()["records"][0]
        assert listed["quality_summary"] == {
            "status": "passed", "limitations_count": 0, "blocked_insights": 0,
        }
        record_id = listed["record_id"]
        detail = client.get(f"/api/research/records/{record_id}")
        assert detail.status_code == 200
        payload = detail.json()
        assert payload["request_id"] == request_id
        assert payload["scope"] == "market"
        assert payload["subject"]["code"] is None
        assert payload["report"]["json"]["status"] == "passed"
        assert len(payload["report"]["json"]["insights"]) == 3
        assert payload["quality_summary"]["status"] == "passed"
        cycle_id = runtime.repository.get_request(request_id).cycle_id
        assert isinstance(cycle_id, str)
    finally:
        runtime.close()

    restarted = build_runtime(
        root=root,
        config_path=config_path,
        db_path=database,
        artifact_dir=artifacts,
        provider_registry=_registry(forbid_calls=True),
        executor=FixtureMarketCodex(forbid_calls=True),
    )
    try:
        replay = restarted.cycle_engine.run_cycle(
            cycle_id,
            ResearchSubject(scope=ResearchScope.market),
            ResearchBoundary(as_of=BOUNDARY),
            [MARKET_TEAM],
            restarted.policy,
        )
        assert replay.status.value == "passed"
        assert replay.team_results[MARKET_TEAM].conclusion.report.status == "passed"
    finally:
        restarted.close()


def test_query_planning_and_finding_phases_are_persisted_with_distinct_capsules(tmp_path: Path):
    root, config_path, database, artifacts = _workspace(tmp_path)
    executor = FixtureMarketCodex(host_query_planning=True)
    runtime = build_runtime(
        root=root,
        config_path=config_path,
        db_path=database,
        artifact_dir=artifacts,
        provider_registry=_registry(),
        executor=executor,
    )
    try:
        with runtime.repository.transaction():
            runtime.repository.submit_request(
                team_ref=MARKET_TEAM,
                subject=ResearchSubject(scope=ResearchScope.market),
                origin="web",
                submission_identity="market-query-phase-audit",
                requested_at=BOUNDARY,
                accepted_at=BOUNDARY,
            )
        completed = _service(runtime, root / "reports").tick()
        assert completed is not None and completed.status == "passed"
        assert sorted(call for call in executor.calls if call.endswith(":query-plan")) == [
            "market_breadth@1:query-plan",
            "sector_rotation@1:query-plan",
        ]

        rows = runtime.repository.connection.execute(
            """
            SELECT invocations.agent_ref, attempts.attempt_number, attempts.status,
                   attempts.capsule_hash, attempts.output_hash, attempts.policy_json
            FROM research_scope_invocation_attempts AS attempts
            JOIN research_scope_invocations AS invocations
              ON invocations.invocation_key = attempts.invocation_key
            ORDER BY invocations.agent_ref, attempts.attempt_number
            """
        ).fetchall()
        by_agent: dict[str, list[tuple]] = {}
        for row in rows:
            by_agent.setdefault(row[0], []).append(tuple(row[1:]))
        assert {
            agent: [json.loads(row[4])["execution_phase"] for row in attempts]
            for agent, attempts in by_agent.items()
        } == {
            "market_breadth@1": ["query_plan", "finding"],
            "market_macro_policy@1": ["finding"],
            "sector_rotation@1": ["query_plan", "finding"],
        }
        for agent in ("market_breadth@1", "sector_rotation@1"):
            attempts = by_agent[agent]
            assert [row[0] for row in attempts] == [1, 2]
            assert all(row[1] == "passed" for row in attempts)
            assert attempts[0][2] != attempts[1][2]
            assert all(runtime.artifact_store.verify(row[2]) for row in attempts)
            assert all(runtime.artifact_store.verify(row[3]) for row in attempts)
            planning_manifest = runtime.artifact_store.read_json(attempts[0][2])
            finding_manifest = runtime.artifact_store.read_json(attempts[1][2])
            assert planning_manifest["query_backed_products"] == []
            assert finding_manifest["query_backed_products"] == []
            query_capsule_hash = finding_manifest["context"]["query_audit"]["query_capsule_hash"]
            assert runtime.artifact_store.read_json(query_capsule_hash)["query_backed_products"] == [
                "whole_market_daily_history@1"
            ]
        assert runtime.repository.connection.execute(
            "SELECT count(*) FROM research_artifacts WHERE media_type = 'application/vnd.a-hunter.query-plan+json'"
        ).fetchone()[0] == 1
    finally:
        runtime.close()


def test_blocked_quality_keeps_attempt_diagnostics_and_partial_reason(tmp_path: Path):
    from advisor.research.codex.executor import CodexExecutionError

    class BlockedSector(FixtureMarketCodex):
        def execute(self, capsule, policy, **kwargs):
            manifest = json.loads(capsule.manifest_path.read_text())
            if manifest['label'] == 'research-agent-sector_rotation@1':
                raise CodexExecutionError(
                    'Agent Finding quality is blocked: 成交额全部缺失',
                    kind='quality_blocked', retryable=False,
                    attempts=(CodexAttempt(1, 'quality_blocked', 1,
                        error_class='quality_blocked', error_message='成交额全部缺失'),),
                )
            return super().execute(capsule, policy, **kwargs)

    root, config_path, database, artifacts = _workspace(tmp_path)
    runtime = build_runtime(root=root, config_path=config_path, db_path=database,
        artifact_dir=artifacts, provider_registry=_registry(), executor=BlockedSector())
    try:
        with runtime.repository.transaction():
            runtime.repository.submit_request(team_ref=MARKET_TEAM,
                subject=ResearchSubject(scope=ResearchScope.market), origin='web',
                submission_identity='blocked-diagnostics', requested_at=BOUNDARY, accepted_at=BOUNDARY)
        completed = _service(runtime, root / 'reports').tick()
        assert completed.status == 'partial'
        report = runtime.artifact_store.read_json(completed.report_json_hash)
        sector = next(x for x in report['insights'] if x['insight_id'] == 'sector_rotation')
        assert sector['reason_code'] == 'quality_blocked'
        hashes = runtime.repository.connection.execute(
            "SELECT content_hash FROM research_artifacts WHERE media_type = 'application/vnd.a-hunter.attempt-diagnostics+json'"
        ).fetchall()
        assert len(hashes) == 1
        diagnostic = runtime.artifact_store.read_json(hashes[0][0])
        assert diagnostic['attempts'][0]['message'] == '成交额全部缺失'
        assert diagnostic['attempts'][0]['phase'] == 'finding'
        assert runtime.repository.connection.execute(
            'SELECT agent_ref FROM research_scope_invocations WHERE invocation_key=?',
            (diagnostic['invocation_key'],)).fetchone()[0] == 'sector_rotation@1'
    finally:
        runtime.close()


def test_cancellation_after_a_worker_finishes_drains_every_agent_audit(tmp_path: Path):
    root, config_path, database, artifacts = _workspace(tmp_path)
    cancel_event = threading.Event()
    runtime = build_runtime(
        root=root,
        config_path=config_path,
        db_path=database,
        artifact_dir=artifacts,
        provider_registry=_registry(),
        executor=CancelOnCompletedAgentCodex(cancel_event),
    )
    try:
        with pytest.raises(ResearchCycleCancelled):
            runtime.cycle_engine.run_cycle(
                "cycle-cancel-after-worker",
                ResearchSubject(scope=ResearchScope.market),
                ResearchBoundary(as_of=BOUNDARY),
                [MARKET_TEAM],
                runtime.policy,
                cancel_event=cancel_event,
            )

        invocation_statuses = [
            row[0]
            for row in runtime.repository.connection.execute(
                "SELECT status FROM research_scope_invocations ORDER BY agent_ref"
            ).fetchall()
        ]
        assert len(invocation_statuses) == 3
        assert set(invocation_statuses) <= {"passed", "cancelled"}
        assert "running" not in invocation_statuses
        attempt_statuses = [
            row[0]
            for row in runtime.repository.connection.execute(
                "SELECT status FROM research_scope_invocation_attempts"
            ).fetchall()
        ]
        assert attempt_statuses
        assert "running" not in attempt_statuses
    finally:
        runtime.close()


def test_missing_success_audit_artifact_fails_closed_before_market_reporting(
    tmp_path: Path,
    monkeypatch,
):
    root, config_path, database, artifacts = _workspace(tmp_path)
    runtime = build_runtime(
        root=root,
        config_path=config_path,
        db_path=database,
        artifact_dir=artifacts,
        provider_registry=_registry(),
        executor=FixtureMarketCodex(),
    )
    try:
        monkeypatch.setattr(runtime.cycle_engine, "_artifact_exists", lambda _content_hash: False)
        completed = runtime.cycle_engine.run_cycle(
            "cycle-missing-success-audit",
            ResearchSubject(scope=ResearchScope.market),
            ResearchBoundary(as_of=BOUNDARY),
            [MARKET_TEAM],
            runtime.policy,
        )

        assert completed.status.value == "blocked"
        assert completed.agent_results == {}
        team_result = completed.team_results[MARKET_TEAM]
        assert team_result.status.value == "blocked"
        assert team_result.conclusion is None
        invocation_rows = runtime.repository.connection.execute(
            "SELECT status, finding_hash FROM research_scope_invocations ORDER BY agent_ref"
        ).fetchall()
        assert [row[0] for row in invocation_rows] == ["failed", "failed", "failed"]
        assert all(row[1] is None for row in invocation_rows)
    finally:
        runtime.close()


def test_market_restart_preserves_failed_agent_as_a_partial_blocked_insight(tmp_path: Path):
    root, config_path, database, artifacts = _workspace(tmp_path)
    runtime = build_runtime(
        root=root,
        config_path=config_path,
        db_path=database,
        artifact_dir=artifacts,
        provider_registry=_registry(),
        executor=FixtureMarketCodex(failing_agents={"sector_rotation@1"}),
    )
    try:
        with runtime.repository.transaction():
            request = runtime.repository.submit_request(
                team_ref=MARKET_TEAM,
                subject=ResearchSubject(scope=ResearchScope.market),
                origin="web",
                submission_identity="market-partial-recovery",
                requested_at=BOUNDARY,
                accepted_at=BOUNDARY,
            )
        completed = _service(runtime, root / "reports").tick()
        assert completed is not None and completed.status == "partial"
        cycle_id = runtime.repository.get_request(request.request_id).cycle_id
        assert isinstance(cycle_id, str)
        status = runtime.repository.connection.execute(
            "SELECT status, query_log_hash FROM research_scope_invocations WHERE agent_ref = 'sector_rotation@1'"
        ).fetchone()
        assert status is not None and status[0] == "failed"
        assert isinstance(status[1], str) and runtime.artifact_store.verify(status[1])
        attempts = runtime.repository.connection.execute(
            """
            SELECT attempts.status, attempts.policy_json, attempts.capsule_hash
            FROM research_scope_invocation_attempts AS attempts
            JOIN research_scope_invocations AS invocations
              ON invocations.invocation_key = attempts.invocation_key
            WHERE invocations.agent_ref = 'sector_rotation@1'
            ORDER BY attempts.attempt_number
            """
        ).fetchall()
        assert [attempt[0] for attempt in attempts] == ["passed", "failed"]
        assert [json.loads(attempt[1])["execution_phase"] for attempt in attempts] == [
            "query_plan",
            "finding",
        ]
        final_manifest = runtime.artifact_store.read_json(attempts[1][2])
        query_capsule_hash = final_manifest["context"]["query_audit"]["query_capsule_hash"]
        assert runtime.artifact_store.verify(query_capsule_hash)
        assert runtime.repository.connection.execute(
            "SELECT media_type FROM research_artifacts WHERE content_hash = ?",
            (status[1],),
        ).fetchone()[0] == "application/vnd.a-hunter.query-log+jsonl"
        assert runtime.repository.connection.execute(
            "SELECT media_type FROM research_artifacts WHERE content_hash = ?",
            (query_capsule_hash,),
        ).fetchone()[0] == "application/vnd.a-hunter.run-capsule+json"
    finally:
        runtime.close()

    restarted = build_runtime(
        root=root,
        config_path=config_path,
        db_path=database,
        artifact_dir=artifacts,
        provider_registry=_registry(forbid_calls=True),
        executor=FixtureMarketCodex(forbid_calls=True),
    )
    try:
        replay = restarted.cycle_engine.run_cycle(
            cycle_id,
            ResearchSubject(scope=ResearchScope.market),
            ResearchBoundary(as_of=BOUNDARY),
            [MARKET_TEAM],
            restarted.policy,
        )
        report = replay.team_results[MARKET_TEAM].conclusion.report
        assert replay.status.value == "passed"
        assert report.status == "partial"
        blocked = [item for item in report.insights if item.status == "blocked"]
        assert [(item.insight_id, item.reason_code) for item in blocked] == [
            ("sector_rotation", "quality_blocked")
        ]
    finally:
        restarted.close()


def test_fully_blocked_market_request_publishes_only_a_diagnostic_cycle(tmp_path: Path):
    root, config_path, database, artifacts = _workspace(tmp_path)
    reports_root = root / "reports"
    runtime = build_runtime(
        root=root,
        config_path=config_path,
        db_path=database,
        artifact_dir=artifacts,
        provider_registry=_registry(),
        executor=FixtureMarketCodex(
            failing_agents={"market_breadth@1", "market_macro_policy@1", "sector_rotation@1"}
        ),
    )
    try:
        with runtime.repository.transaction():
            request = runtime.repository.submit_request(
                team_ref=MARKET_TEAM,
                subject=ResearchSubject(scope=ResearchScope.market),
                origin="web",
                submission_identity="market-fully-blocked-diagnostic",
                requested_at=BOUNDARY,
                accepted_at=BOUNDARY,
            )

        completed = _service(runtime, reports_root).tick()

        assert completed is not None and completed.status == "blocked"
        cycle_id = f"cycle-{request.request_id.removeprefix('request-')}"
        report_dir = (
            reports_root
            / BOUNDARY.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
            / cycle_id
        )
        assert (report_dir / "cycle.json").is_file()
        assert (report_dir / "index.md").is_file()
        assert (report_dir / "complete.json").is_file()
        assert (report_dir / "teams" / MARKET_TEAM / "status.json").is_file()
        assert not (report_dir / "teams" / MARKET_TEAM / "report.md").exists()
        assert not (report_dir / "teams" / MARKET_TEAM / "conclusion.json").exists()
    finally:
        runtime.close()


def test_service_restart_recovers_an_exact_completed_report_after_crash_before_request_completion(tmp_path: Path):
    root, config_path, database, artifacts = _workspace(tmp_path)
    reports_root = root / "reports"
    runtime = build_runtime(
        root=root,
        config_path=config_path,
        db_path=database,
        artifact_dir=artifacts,
        provider_registry=_registry(),
        executor=FixtureMarketCodex(),
    )
    try:
        with runtime.repository.transaction():
            request = runtime.repository.submit_request(
                team_ref=MARKET_TEAM,
                subject=ResearchSubject(scope=ResearchScope.market),
                origin="web",
                submission_identity="crash-after-publication",
                requested_at=BOUNDARY,
                accepted_at=BOUNDARY,
            )

        def crash_before_completion(*_args, **_kwargs):
            raise KeyboardInterrupt("simulated process crash after publication")

        runtime.repository.complete_request = crash_before_completion  # type: ignore[method-assign]
        crashed = ResearchService(
            runtime.repository,
            lambda current, boundary, progress, cancelled: _execute_service_request(
                runtime, current, boundary, progress, cancelled, reports_root=reports_root
            ),
            owner_id="crashed-worker",
            clock=lambda: BOUNDARY,
        )
        with pytest.raises(KeyboardInterrupt, match="simulated process crash"):
            crashed.tick()
        report_dir = reports_root / BOUNDARY.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat() / f"cycle-{request.request_id.removeprefix('request-')}"
        assert (report_dir / "complete.json").is_file()
    finally:
        runtime.close()

    restarted = build_runtime(
        root=root,
        config_path=config_path,
        db_path=database,
        artifact_dir=artifacts,
        provider_registry=_registry(forbid_calls=True),
        executor=FixtureMarketCodex(forbid_calls=True),
    )
    try:
        resumed = ResearchService(
            restarted.repository,
            lambda current, boundary, progress, cancelled: _execute_service_request(
                restarted, current, boundary, progress, cancelled, reports_root=reports_root
            ),
            owner_id="recovery-worker",
            clock=lambda: BOUNDARY + timedelta(seconds=31),
        )

        completed = resumed.tick()

        assert completed is not None and completed.status == "passed"
        assert restarted.repository.get_request(request.request_id).status == "passed"
        assert (report_dir / "complete.json").is_file()
    finally:
        restarted.close()


def test_service_restart_recovers_an_exact_blocked_diagnostic_after_crash_before_request_completion(tmp_path: Path):
    root, config_path, database, artifacts = _workspace(tmp_path)
    reports_root = root / "reports"
    runtime = build_runtime(
        root=root,
        config_path=config_path,
        db_path=database,
        artifact_dir=artifacts,
        provider_registry=_registry(),
        executor=FixtureMarketCodex(
            failing_agents={"market_breadth@1", "market_macro_policy@1", "sector_rotation@1"}
        ),
    )
    try:
        with runtime.repository.transaction():
            request = runtime.repository.submit_request(
                team_ref=MARKET_TEAM,
                subject=ResearchSubject(scope=ResearchScope.market),
                origin="web",
                submission_identity="crash-after-blocked-publication",
                requested_at=BOUNDARY,
                accepted_at=BOUNDARY,
            )

        def crash_before_completion(*_args, **_kwargs):
            raise KeyboardInterrupt("simulated process crash after blocked publication")

        runtime.repository.complete_request = crash_before_completion  # type: ignore[method-assign]
        with pytest.raises(KeyboardInterrupt, match="simulated process crash"):
            _service(runtime, reports_root).tick()
        report_dir = (
            reports_root
            / BOUNDARY.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
            / f"cycle-{request.request_id.removeprefix('request-')}"
        )
        published = {
            str(path.relative_to(report_dir)): path.read_bytes()
            for path in report_dir.rglob("*")
            if path.is_file()
        }
        assert "complete.json" in published
        assert "teams/a_share_market_overview@1/status.json" in published
    finally:
        runtime.close()

    restarted = build_runtime(
        root=root,
        config_path=config_path,
        db_path=database,
        artifact_dir=artifacts,
        provider_registry=_registry(forbid_calls=True),
        executor=FixtureMarketCodex(forbid_calls=True),
    )
    try:
        resumed = ResearchService(
            restarted.repository,
            lambda current, boundary, progress, cancelled: _execute_service_request(
                restarted, current, boundary, progress, cancelled, reports_root=reports_root
            ),
            owner_id="blocked-recovery-worker",
            clock=lambda: BOUNDARY + timedelta(seconds=31),
        )

        completed = resumed.tick()

        assert completed is not None and completed.status == "blocked"
        assert restarted.repository.get_request(request.request_id).status == "blocked"
        recovered = {
            str(path.relative_to(report_dir)): path.read_bytes()
            for path in report_dir.rglob("*")
            if path.is_file()
        }
        assert recovered == published
    finally:
        restarted.close()


def test_service_restart_retries_a_crash_after_staged_cycle_json_without_refetching(tmp_path: Path, monkeypatch):
    root, config_path, database, artifacts = _workspace(tmp_path)
    reports_root = root / "reports"
    runtime = build_runtime(
        root=root,
        config_path=config_path,
        db_path=database,
        artifact_dir=artifacts,
        provider_registry=_registry(),
        executor=FixtureMarketCodex(),
    )
    try:
        with runtime.repository.transaction():
            request = runtime.repository.submit_request(
                team_ref=MARKET_TEAM,
                subject=ResearchSubject(scope=ResearchScope.market),
                origin="web",
                submission_identity="crash-during-staged-publication",
                requested_at=BOUNDARY,
                accepted_at=BOUNDARY,
            )
        original_write = CycleReporter._write_new
        writes = 0

        def crash_after_cycle_json(path, content):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise KeyboardInterrupt("simulated crash after staged cycle.json")
            original_write(path, content)

        with monkeypatch.context() as patched:
            patched.setattr(CycleReporter, "_write_new", staticmethod(crash_after_cycle_json))
            crashed = _service(runtime, reports_root)
            with pytest.raises(KeyboardInterrupt, match="staged cycle.json"):
                crashed.tick()

        report_dir = reports_root / BOUNDARY.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat() / f"cycle-{request.request_id.removeprefix('request-')}"
        assert not report_dir.exists()
        assert list(report_dir.parent.glob(f".{report_dir.name}.staging-*"))
        assert runtime.repository.get_request(request.request_id).status == "running"
    finally:
        runtime.close()

    restarted = build_runtime(
        root=root,
        config_path=config_path,
        db_path=database,
        artifact_dir=artifacts,
        provider_registry=_registry(forbid_calls=True),
        executor=FixtureMarketCodex(forbid_calls=True),
    )
    try:
        resumed = ResearchService(
            restarted.repository,
            lambda current, boundary, progress, cancelled: _execute_service_request(
                restarted, current, boundary, progress, cancelled, reports_root=reports_root
            ),
            owner_id="staged-recovery-worker",
            clock=lambda: BOUNDARY + timedelta(seconds=31),
        )

        completed = resumed.tick()

        assert completed is not None and completed.status == "passed"
        assert restarted.repository.get_request(request.request_id).status == "passed"
        assert (report_dir / "complete.json").is_file()
    finally:
        restarted.close()


def test_long_lived_service_uses_saved_model_for_new_request(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [{
        "slug": "new-model", "visibility": "list", "default_reasoning_level": "high",
        "supported_reasoning_levels": [{"effort": "high"}]}]}))
    from advisor.research.execution_settings import ExecutionSettingsService
    class ModelRecordingCodex(FixtureMarketCodex):
        def __init__(self):
            super().__init__()
            self.models = []
        def execute(self, capsule, policy, **kwargs):
            self.models.append((policy.model, policy.reasoning_effort))
            return super().execute(capsule, policy, **kwargs)
    root, config_path, database, artifacts = _workspace(tmp_path)
    executor = ModelRecordingCodex()
    runtime = build_runtime(root=root, config_path=config_path, db_path=database,
        artifact_dir=artifacts, provider_registry=_registry(), executor=executor)
    try:
        ExecutionSettingsService(root=root, config_path=config_path).update(
            expected_policy_ref=str(runtime.policy.policy), model="new-model", reasoning_effort="high")
        with runtime.repository.transaction():
            runtime.repository.submit_request(team_ref=MARKET_TEAM,
                subject=ResearchSubject(scope=ResearchScope.market), origin="web",
                submission_identity="new-model-selection", requested_at=BOUNDARY, accepted_at=BOUNDARY)
        completed = _service(runtime, root / "reports").tick()
        assert completed.status == "passed"
        assert executor.models and set(executor.models) == {("new-model", "high")}
        assert runtime.policy.model != "new-model"  # The process started with the old default.
    finally:
        runtime.close()
