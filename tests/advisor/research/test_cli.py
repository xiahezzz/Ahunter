from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import advisor.research.cli as research_cli
from advisor.research.cli import _execute_service_request, build_runtime, preflight_runtime
from advisor.research.contracts import ResearchBoundary, ResearchSubject, RunStatus, VersionRef, canonical_json
from advisor.research.data_products.engine import ProductRequest, ProviderObservation, ProviderRegistry
from advisor.research.repository import ResearchRepository
from advisor.research.state_machine import TeamRunResult


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
            source_locator="fixture://product",
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
            source_locator="fixture://mx-events",
        )


def _registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    for product in (
        "company_identity@1", "market_daily_bars@1", "market_quote@1",
        "fundamental_snapshot@1", "financial_statements@1", "earnings_forecast@1",
        "industry_context@1", "insider_activity@1", "company_news@1", "macro_news@1",
        "mx_events@1", "mx_events@2", "hot_stocks@1", "capital_flows@1", "concepts@1",
        "dragon_tiger@1", "lockup_calendar@1",
    ):
        if product == "market_daily_bars@1":
            provider = LocalFixtureProvider()
        elif product == "mx_events@2":
            provider = LocalMxFixtureProvider()
        else:
            provider = FixtureProvider()
        registry.register(product, provider)
    return registry


class NoopExecutor:
    def preflight(self, policy):
        return "fixture-codex"


def test_runtime_preflight_validates_catalog_storage_and_local_executor(tmp_path: Path):
    runtime = build_runtime(
        root=Path.cwd(),
        db_path=tmp_path / "research.sqlite",
        artifact_dir=tmp_path / "artifacts",
        provider_registry=_registry(),
        executor=NoopExecutor(),
    )
    try:
        result = preflight_runtime(runtime)
        assert result.status == "passed"
        assert result.cli_version == "fixture-codex"
        assert result.checks["catalog"] == "passed"
        assert result.checks["public_sources"] == "passed"
        assert result.checks["local_codex"] == "passed"
    finally:
        runtime.close()


def test_runtime_preflight_blocks_when_selected_source_is_not_registered(tmp_path: Path):
    runtime = build_runtime(
        root=Path.cwd(),
        db_path=tmp_path / "research.sqlite",
        artifact_dir=tmp_path / "artifacts",
        provider_registry=ProviderRegistry(),
        executor=NoopExecutor(),
    )
    try:
        result = preflight_runtime(runtime)
        assert result.status == "blocked"
        assert "Provider Adapter" in result.message
        assert result.checks["catalog"] == "passed"
    finally:
        runtime.close()


def test_default_runtime_registers_the_scoped_mx_provider(tmp_path: Path):
    runtime = build_runtime(
        root=Path.cwd(),
        db_path=tmp_path / "research.sqlite",
        artifact_dir=tmp_path / "artifacts",
        executor=NoopExecutor(),
    )
    try:
        providers = runtime.product_engine.registry.providers_for(
            "mx_events@2",
            provider_ids=("local-mx",),
        )
        assert len(providers) == 1
        assert providers[0].events_database == (Path.cwd() / "data/state/events.sqlite").resolve()
        assert providers[0].allowed_rids_path == (Path.cwd() / "config/allowed-rids.yaml").resolve()
    finally:
        runtime.close()


def test_service_request_stops_at_preflight_before_building_a_snapshot(tmp_path: Path):
    class BrokenPreflight:
        def preflight(self, _policy):
            raise RuntimeError("fixture structured output rejection")

    class UnexpectedCycle:
        def run_cycle(self, *_args, **_kwargs):
            raise AssertionError("Cycle must not start after a failed preflight")

    team = VersionRef.parse("core@1")
    subject = ResearchSubject(code="600519")
    runtime = SimpleNamespace(
        catalog=SimpleNamespace(
            validate_subject_for_team=lambda *_args: None,
            team=lambda _ref: SimpleNamespace(agents=(VersionRef.parse("market@1"),)),
        ),
        executor=BrokenPreflight(),
        policy=object(),
        cycle_engine=UnexpectedCycle(),
    )
    request = SimpleNamespace(
        request_id=f"request-{'a' * 32}",
        team=team,
        subject=subject,
        scope=subject.scope,
    )

    result = _execute_service_request(
        runtime,
        request,
        ResearchBoundary(as_of=datetime(2026, 8, 6, tzinfo=timezone.utc)),
        lambda *_args: None,
        lambda: False,
        reports_root=tmp_path,
    )

    assert result.status == "blocked"
    assert result.reason_code == "preflight_failed"
    assert result.cycle_id is None


def test_service_request_preserves_the_cycle_terminal_reason(tmp_path: Path):
    class PassingPreflight:
        def preflight(self, _policy):
            return "fixture"

    team = VersionRef.parse("core@1")
    subject = ResearchSubject(code="600519")
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, tzinfo=timezone.utc))
    cycle = SimpleNamespace(
        cycle_id="cycle-fixture",
        subject=subject,
        boundary=boundary,
        status=RunStatus.blocked,
        fingerprint="a" * 64,
        snapshot=None,
        agent_errors={},
        team_results={
            str(team): TeamRunResult(
                team,
                RunStatus.blocked,
                reason_code="snapshot_unavailable",
            )
        },
    )
    runtime = SimpleNamespace(
        catalog=SimpleNamespace(
            validate_subject_for_team=lambda *_args: None,
            team=lambda _ref: SimpleNamespace(agents=(VersionRef.parse("market@1"),)),
        ),
        executor=PassingPreflight(),
        policy=object(),
        cycle_engine=SimpleNamespace(run_cycle=lambda *_args, **_kwargs: cycle),
    )
    request = SimpleNamespace(
        request_id=f"request-{'b' * 32}",
        team=team,
        subject=subject,
        scope=subject.scope,
    )

    result = _execute_service_request(
        runtime,
        request,
        boundary,
        lambda *_args: None,
        lambda: False,
        reports_root=tmp_path,
    )

    assert result.status == "blocked"
    assert result.reason_code == "snapshot_unavailable"
    report_root = tmp_path / "2026-08-06" / "cycle-fixture"
    assert (report_root / "complete.json").is_file()
    assert (report_root / "teams" / "core@1" / "status.json").is_file()


def test_manual_single_subject_run_requires_an_explicit_published_team(monkeypatch, capsys):
    def unexpected_runtime(**_kwargs):
        raise AssertionError("manual input must be rejected before runtime construction")

    monkeypatch.setattr(research_cli, "build_runtime", unexpected_runtime)

    status = research_cli.main(
        [
            "run",
            "--codes", "600519",
            "--as-of", "2026-08-06T08:30:00+08:00",
        ]
    )

    assert status == 1
    assert "--team" in capsys.readouterr().out


def test_manual_single_subject_run_with_an_explicit_team_only_submits_a_durable_request(monkeypatch, tmp_path: Path, capsys):
    def unexpected_execution(**_kwargs):
        raise AssertionError("a CLI submitter must not construct an execution runtime")

    monkeypatch.setattr(research_cli, "build_runtime", unexpected_execution)
    monkeypatch.setattr(research_cli, "run_research_cycle", unexpected_execution)
    database = tmp_path / "research.sqlite"

    assert research_cli.main([
        "--db", str(database),
        "run", "--codes", "600519", "--team", "a_share_core@1",
        "--as-of", "2026-08-06T08:30:00+08:00",
    ]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "queued"
    assert payload["scope"] == "security"
    repository = ResearchRepository.open(database)
    try:
        submitted = repository.get_request(payload["request_id"])
        assert submitted.team == research_cli.VersionRef.parse("a_share_core@1")
        assert submitted.subject.code == "600519"
        assert submitted.status == "queued"
    finally:
        repository.close()
