from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from advisor.mx.rid_authorization import RidAuthorizationStore
from advisor.research.agent_access_publication import AgentAccessPublicationService
from advisor.research.agents.runner import AgentFindingError, AgentRunner
from advisor.research.artifacts import ArtifactStore
from advisor.research.catalog import ManifestCatalog, load_catalog_from_directory
from advisor.research.contracts import ResearchBoundary, ResearchSubject
from advisor.research.data_products.engine import (
    DataProductEngine,
    ProductRequest,
    ProviderObservation,
    ProviderRegistry,
)
from advisor.research.providers.local_mx import LocalMxProvider
from advisor.research.team_publication import TeamPublicationService


_BOUNDARY = datetime(2026, 8, 8, 8, 30, tzinfo=timezone.utc)


class _IdentityProvider:
    provider_id = "fixture"

    def fetch(self, request: ProductRequest, *, dependencies=None) -> ProviderObservation:
        assert str(request.product) == "identity@1"
        return ProviderObservation(
            provider=self.provider_id,
            payload={"status": "passed"},
            observed_at=request.boundary.as_of,
            quality_status="passed",
        )


class _CountingLocalMxProvider(LocalMxProvider):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.requests: list[tuple[int, ...]] = []

    def fetch(self, request: ProductRequest, *, dependencies=None) -> ProviderObservation:
        self.requests.append(request.feed_rids)
        return super().fetch(request, dependencies=dependencies)


class _NoCallsExecutor:
    def execute(self, *_args, **_kwargs):
        raise AssertionError("the end-to-end access fixture must not invoke Codex")


def _write_workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    catalog = tmp_path / "catalog"
    for directory in ("products", "agents", "teams", "pipelines", "execution"):
        (catalog / directory).mkdir(parents=True)
    (catalog / "products" / "identity.yaml").write_text(
        """
product: identity@1
title: 固定身份资料
providers: [fixture]
required_fields: [status]
""".lstrip(),
        encoding="utf-8",
    )
    (catalog / "products" / "mx_events.yaml").write_text(
        """
product: mx_events@2
title: 按 RID 隔离的 MX 资讯
providers: [local-mx]
required_fields: [status, feeds]
feed_scope: {kind: mx_rid_feeds, required: [rids]}
""".lstrip(),
        encoding="utf-8",
    )
    for agent_id, title in (("alpha", "甲研究"), ("beta", "乙研究")):
        (catalog / "agents" / f"{agent_id}.yaml").write_text(
            f"""
agent: {agent_id}@1
title: {title}
instructions: 只能读取固定的已声明资料。
required_products: [identity@1]
""".lstrip(),
            encoding="utf-8",
        )
    (catalog / "teams" / "shared.yaml").write_text(
        """
team: shared@1
title: 共享团队
agents: [alpha@1, beta@1]
""".lstrip(),
        encoding="utf-8",
    )
    allowed = tmp_path / "allowed-rids.yaml"
    allowed.write_text("allowed_rids: [111, 222]\n", encoding="utf-8")
    database = tmp_path / "events.sqlite"
    _write_events(database)
    return catalog, allowed, database


def _write_events(database: Path) -> None:
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE events (
          event_id TEXT PRIMARY KEY, rid INTEGER, received_at INTEGER,
          source_created_at INTEGER, decoded_text TEXT, content_hash TEXT
        );
        CREATE TABLE media (
          event_id TEXT, url_hash TEXT, content_hash TEXT, content_type TEXT,
          local_path TEXT, downloaded_at INTEGER
        );
        CREATE TABLE media_jobs (event_id TEXT, status TEXT);
        CREATE TABLE decode_failures (
          payload_hash TEXT, error_class TEXT, bucket_start INTEGER, count INTEGER
        );
        CREATE VIRTUAL TABLE mx_event_search USING fts5(event_id UNINDEXED, decoded_text);
        """
    )
    received = int((_BOUNDARY - timedelta(days=1)).timestamp() * 1000)
    rows = [
        ("fixture-111", 111, received, received - 1, "RID 111 已接收资讯", "1" * 64),
        ("fixture-222", 222, received + 1, received, "RID 222 已接收资讯", "2" * 64),
    ]
    connection.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)", rows)
    connection.executemany(
        "INSERT INTO mx_event_search(rowid, event_id, decoded_text) VALUES (?, ?, ?)",
        [(1, "fixture-111", "RID 111 已接收资讯"), (2, "fixture-222", "RID 222 已接收资讯")],
    )
    connection.commit()
    connection.close()


def _engine(
    tmp_path: Path,
    catalog: ManifestCatalog,
    database: Path,
    allowed: Path,
) -> tuple[DataProductEngine, _CountingLocalMxProvider, ArtifactStore]:
    store = ArtifactStore(tmp_path / "artifacts")
    provider = _CountingLocalMxProvider(database, allowed, repository_root=tmp_path)
    registry = ProviderRegistry()
    registry.register("identity@1", _IdentityProvider())
    registry.register("mx_events@2", provider)
    return DataProductEngine(catalog, registry, store), provider, store


def _snapshot(engine: DataProductEngine, cycle_id: str, teams: list[str]):
    return engine.build_snapshot(
        cycle_id=cycle_id,
        subject=ResearchSubject(code="600519"),
        boundary=ResearchBoundary(as_of=_BOUNDARY),
        team_refs=teams,
    )


def test_agent_access_versions_keep_team_revisions_explicit_and_scope_mx_feeds_end_to_end(tmp_path: Path):
    catalog_dir, allowed, database = _write_workspace(tmp_path)
    access = AgentAccessPublicationService(catalog_dir, allowed_rids_path=allowed)
    rid_version = RidAuthorizationStore(allowed).read().version

    alpha = access.publish(
        "alpha@1",
        [
            {"product": "identity@1"},
            {"product": "mx_events@2", "feed_scope": {"rids": [111]}},
        ],
        expected_rid_version=rid_version,
    )
    beta = access.publish(
        "beta@1",
        [
            {"product": "identity@1"},
            {"product": "mx_events@2", "feed_scope": {"rids": [111, 222]}},
        ],
        expected_rid_version=rid_version,
    )
    teams = TeamPublicationService(catalog_dir)
    shared = teams.publish("shared", "共享团队", ["alpha", "beta"], scope="security")
    independent = teams.publish("independent", "独立团队", ["alpha"], scope="security")

    assert (alpha.agent_ref, beta.agent_ref) == ("alpha@2", "beta@2")
    assert shared.team_ref == "shared@2"
    assert independent.team_ref == "independent@1"
    assert load_catalog_from_directory(catalog_dir).team("shared@1").agents == (
        load_catalog_from_directory(catalog_dir).agent("alpha@1").agent,
        load_catalog_from_directory(catalog_dir).agent("beta@1").agent,
    )

    catalog = load_catalog_from_directory(catalog_dir)
    engine, provider, store = _engine(tmp_path / "initial", catalog, database, allowed)
    snapshot = _snapshot(engine, "mx-access-initial", ["shared@2", "independent@1"])
    index = snapshot.product("mx_events@2").payload
    assert provider.requests == [(111, 222)]
    assert [feed["rid"] for feed in index["feeds"]] == [111, 222]
    assert len({feed["artifact_hash"] for feed in index["feeds"]}) == 2

    runner = AgentRunner(catalog, _NoCallsExecutor(), artifact_store=store)
    alpha_inputs, _alpha_evidence, _ = runner._inputs(catalog.agent("alpha@2"), snapshot)
    beta_inputs, _beta_evidence, _ = runner._inputs(catalog.agent("beta@2"), snapshot)
    assert [feed["rid"] for feed in alpha_inputs["mx_events@2"]["payload"]["feeds"]] == [111]
    assert [feed["rid"] for feed in beta_inputs["mx_events@2"]["payload"]["feeds"]] == [111, 222]
    assert "fixture-222" not in repr(alpha_inputs)
    assert "raw_payload" not in repr(beta_inputs)
    assert "source_url" not in repr(beta_inputs)
    assert "local_path" not in repr(beta_inputs)

    # Revocation is a new temporary authorization state.  It does not rewrite
    # either Team and blocks only the Agent that still pins RID 222.
    allowed.write_text("allowed_rids: [111]\n", encoding="utf-8")
    revoked_catalog = load_catalog_from_directory(catalog_dir)
    revoked_engine, revoked_provider, revoked_store = _engine(tmp_path / "revoked", revoked_catalog, database, allowed)
    revoked_snapshot = _snapshot(revoked_engine, "mx-access-revoked", ["shared@2", "independent@1"])
    assert revoked_provider.requests == [(111, 222)]
    revoked_runner = AgentRunner(revoked_catalog, _NoCallsExecutor(), artifact_store=revoked_store)
    revoked_runner._inputs(revoked_catalog.agent("alpha@2"), revoked_snapshot)
    with pytest.raises(AgentFindingError, match="RID Feed 222 is unavailable"):
        revoked_runner._inputs(revoked_catalog.agent("beta@2"), revoked_snapshot)

    current_version = RidAuthorizationStore(allowed).read().version
    restored_beta = access.publish(
        "beta@2",
        [
            {"product": "identity@1"},
            {"product": "mx_events@2", "feed_scope": {"rids": [111]}},
        ],
        expected_rid_version=current_version,
    )
    restored_team = teams.publish("shared", "共享团队", ["alpha", "beta"], scope="security")
    assert restored_beta.agent_ref == "beta@3"
    assert restored_team.team_ref == "shared@3"
    assert load_catalog_from_directory(catalog_dir).team("shared@2").agents[-1].version == 2

    restored_catalog = load_catalog_from_directory(catalog_dir)
    restored_engine, restored_provider, restored_store = _engine(tmp_path / "restored", restored_catalog, database, allowed)
    restored_snapshot = _snapshot(restored_engine, "mx-access-restored", ["shared@3", "independent@1"])
    assert restored_provider.requests == [(111,)]
    restored_runner = AgentRunner(restored_catalog, _NoCallsExecutor(), artifact_store=restored_store)
    restored_runner._inputs(restored_catalog.agent("alpha@2"), restored_snapshot)
    restored_beta_inputs, _restored_evidence, _ = restored_runner._inputs(
        restored_catalog.agent("beta@3"), restored_snapshot
    )
    assert [feed["rid"] for feed in restored_beta_inputs["mx_events@2"]["payload"]["feeds"]] == [111]
