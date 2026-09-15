from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from advisor.research.agents.runner import AgentFindingError, AgentRunner, invocation_key, visible_input_artifacts
from advisor.research.artifacts import ArtifactStore
from advisor.research.catalog import ManifestCatalog
from advisor.research.contracts import (
    AgentManifest,
    DataProductManifest,
    ExecutionPolicy,
    ResearchBoundary,
    ResearchSubject,
    ResearchTeam,
)
from advisor.research.data_products.engine import DataProductEngine, ProviderRegistry
from advisor.research.providers.local_mx import LocalMxProvider
from advisor.research.state_machine import ResearchCycleEngine


_BOUNDARY = datetime(2026, 8, 8, 8, 30, tzinfo=timezone.utc)


def _events_database(path: Path) -> None:
    connection = sqlite3.connect(path)
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
    start = int((_BOUNDARY - timedelta(days=1)).timestamp() * 1000)
    rows = [
        ("event-111", 111, start, start - 1_000, "RID 111 的规范化资讯", "1" * 64),
        ("event-222", 222, start + 1, start - 1_000, "RID 222 的规范化资讯", "2" * 64),
    ]
    connection.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)", rows)
    connection.executemany(
        "INSERT INTO mx_event_search(rowid, event_id, decoded_text) VALUES (?, ?, ?)",
        [(1, "event-111", "RID 111 的规范化资讯"), (2, "event-222", "RID 222 的规范化资讯")],
    )
    connection.commit()
    connection.close()


def _catalog() -> ManifestCatalog:
    product = DataProductManifest(
        product="mx_events@2",
        title="RID scoped MX",
        providers=("local-mx",),
        required_fields=("status", "feeds"),
        feed_scope={"kind": "mx_rid_feeds"},
    )
    one = AgentManifest(
        agent="one@1",
        title="One",
        instructions="只读取声明的资料。",
        data_access=({"product": "mx_events@2", "feed_scope": {"rids": [111]}},),
    )
    both = AgentManifest(
        agent="both@1",
        title="Both",
        instructions="只读取声明的资料。",
        data_access=({"product": "mx_events@2", "feed_scope": {"rids": [111, 222]}},),
    )
    return ManifestCatalog(
        products={"mx_events@2": product},
        agents={"one@1": one, "both@1": both},
        teams={
            "one_team@1": ResearchTeam(team="one_team@1", title="One", agents=("one@1",)),
            "both_team@1": ResearchTeam(team="both_team@1", title="Both", agents=("both@1",)),
        },
        pipelines={},
        execution_policies={},
    ).validate()


def _engine(tmp_path: Path, *, rids: str = "111, 222") -> tuple[DataProductEngine, ManifestCatalog, ArtifactStore, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "events.sqlite"
    _events_database(database)
    allowed = tmp_path / "allowed-rids.yaml"
    allowed.write_text(f"allowed_rids: [{rids}]\n", encoding="utf-8")
    catalog = _catalog()
    store = ArtifactStore(tmp_path / "artifacts")
    registry = ProviderRegistry()
    registry.register("mx_events@2", LocalMxProvider(database, allowed, repository_root=tmp_path))
    return DataProductEngine(catalog, registry, store), catalog, store, allowed


def _snapshot(engine: DataProductEngine):
    return engine.build_snapshot(
        cycle_id="mx-rid-fixture",
        subject=ResearchSubject(code="600519"),
        boundary=ResearchBoundary(as_of=_BOUNDARY),
        team_refs=["one_team@1", "both_team@1"],
    )


def _policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        policy="codex@1",
        model="fixture",
        reasoning_effort="low",
        timeout_seconds=30,
        max_agent_concurrency=1,
        max_stage_concurrency=1,
    )


def test_scoped_mx_feeds_are_sealed_once_and_capsules_only_receive_the_declared_subset(tmp_path: Path):
    engine, catalog, store, _allowed = _engine(tmp_path)
    snapshot = _snapshot(engine)
    index = snapshot.product("mx_events@2").payload
    feeds = index["feeds"]
    assert [feed["rid"] for feed in feeds] == [111, 222]
    assert feeds[0]["artifact_hash"] != feeds[1]["artifact_hash"]
    assert all("items" not in feed for feed in feeds)

    runner = AgentRunner(catalog, object(), artifact_store=store)
    one_inputs, _one_evidence, _ = runner._inputs(catalog.agent("one@1"), snapshot)
    both_inputs, _both_evidence, _ = runner._inputs(catalog.agent("both@1"), snapshot)
    one_payload = one_inputs["mx_events@2"]["payload"]
    both_payload = both_inputs["mx_events@2"]["payload"]
    assert [feed["rid"] for feed in one_payload["feeds"]] == [111]
    assert [feed["rid"] for feed in both_payload["feeds"]] == [111, 222]
    assert "event-222" not in repr(one_payload)
    assert "raw_payload" not in repr(both_payload)
    assert "source_url" not in repr(both_payload)
    assert "local_path" not in repr(both_payload)

    one = catalog.agent("one@1")
    both = catalog.agent("both@1")
    assert invocation_key(
        one.agent,
        snapshot,
        _policy(),
        input_products=one.product_refs,
        input_artifacts=visible_input_artifacts(one, snapshot),
    ) != invocation_key(
        both.agent,
        snapshot,
        _policy(),
        input_products=both.product_refs,
        input_artifacts=visible_input_artifacts(both, snapshot),
    )


def test_revoked_rid_blocks_only_agents_that_declared_that_exact_feed(tmp_path: Path):
    engine, catalog, store, _allowed = _engine(tmp_path, rids="111")
    snapshot = _snapshot(engine)
    runner = AgentRunner(catalog, object(), artifact_store=store)
    runner._inputs(catalog.agent("one@1"), snapshot)
    with pytest.raises(AgentFindingError, match="RID Feed 222 is unavailable"):
        runner._inputs(catalog.agent("both@1"), snapshot)


def test_unavailable_rid_feed_blocks_the_security_team_before_any_agent_runs(tmp_path: Path):
    engine, catalog, store, _allowed = _engine(tmp_path, rids="111")
    snapshot = _snapshot(engine)
    catalog.teams["mixed@1"] = ResearchTeam(
        team="mixed@1",
        title="Mixed",
        agents=("one@1", "both@1"),
    )

    class StaticProducts:
        def build_snapshot(self, **_kwargs):
            return snapshot

    class UnexpectedCodex:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("no Agent may start when a required RID feed is unavailable")

    class UnexpectedDecision:
        def run(self, *_args, **_kwargs):
            raise AssertionError("no Decision Pipeline may start for a preblocked Team")

    cycle = ResearchCycleEngine(
        catalog,
        StaticProducts(),
        AgentRunner(catalog, UnexpectedCodex(), artifact_store=store),
        UnexpectedDecision(),
    ).run_cycle(
        "cycle-mx-input-gate",
        snapshot.subject,
        snapshot.boundary,
        ["mixed@1"],
        _policy(),
    )

    assert cycle.team_results["mixed@1"].status.value == "blocked"
    assert cycle.team_results["mixed@1"].reason_code == "snapshot_unavailable"
    assert "both@1" in cycle.agent_errors


def test_media_and_unattributed_decode_failures_fail_closed_per_their_scope(tmp_path: Path):
    engine, catalog, store, _allowed = _engine(tmp_path)
    database = tmp_path / "events.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("INSERT INTO media_jobs VALUES (?, ?)", ("event-111", "pending"))
    connection.commit()
    connection.close()
    snapshot = _snapshot(engine)
    runner = AgentRunner(catalog, object(), artifact_store=store)
    with pytest.raises(AgentFindingError, match="RID Feed 111 is unavailable"):
        runner._inputs(catalog.agent("one@1"), snapshot)

    # A decode failure without an attributable RID blocks all MX v2 feeds in
    # the same half-open window, rather than leaking retained event content.
    engine, catalog, store, _allowed = _engine(tmp_path / "global")
    database = tmp_path / "global" / "events.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO decode_failures VALUES (?, ?, ?, ?)",
        ("f" * 64, "DecodeError", int((_BOUNDARY - timedelta(days=1)).timestamp() * 1000), 1),
    )
    connection.commit()
    connection.close()
    snapshot = _snapshot(engine)
    runner = AgentRunner(catalog, object(), artifact_store=store)
    with pytest.raises(AgentFindingError, match="RID Feed 111 is unavailable"):
        runner._inputs(catalog.agent("one@1"), snapshot)
