from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

from advisor.research.artifacts import ArtifactStore
from advisor.research.catalog import ManifestCatalog
from advisor.research.contracts import ResearchBoundary, ResearchScope, ResearchSubject, canonical_json
from advisor.research.data_products.engine import (
    DataProductEngine,
    ProductRequest,
    ProductUnavailable,
    ProviderObservation,
    ProviderRegistry,
)
from advisor.research.repository import ResearchRepository


class FakeProvider:
    def __init__(self, provider_id: str, payload: object | None = None, error: Exception | None = None, observed_at=None, coverage: float = 1.0):
        self.provider_id = provider_id
        self.payload = payload
        self.error = error
        self.observed_at = observed_at
        self.coverage = coverage
        self.calls = 0

    def fetch(self, request: ProductRequest, *, dependencies=None):
        self.calls += 1
        if self.error:
            raise self.error
        return ProviderObservation(
            provider=self.provider_id,
            payload=self.payload,
            observed_at=self.observed_at or request.boundary.as_of,
            source_locator=f"fixture://{self.provider_id}",
            coverage=self.coverage,
        )


def catalog_for(products: dict, agents: dict, teams: dict) -> ManifestCatalog:
    return ManifestCatalog(products=products, agents=agents, teams=teams, pipelines={}, execution_policies={}).validate()


def test_snapshot_builds_union_once_and_falls_back(tmp_path: Path):
    from advisor.research.contracts import AgentManifest, DataProductManifest, ResearchTeam

    product = DataProductManifest(product="bars@1", title="Bars", providers=("bad", "good"))
    agent = AgentManifest(agent="market@1", title="Market", instructions="inspect", required_products=("bars@1",))
    team = ResearchTeam(team="core@1", title="Core", agents=("market@1",))
    catalog = catalog_for({"bars@1": product}, {"market@1": agent}, {"core@1": team})
    registry = ProviderRegistry()
    bad = FakeProvider("bad", error=ProductUnavailable("offline"))
    good = FakeProvider("good", payload={"rows": [1]})
    registry.register("bars@1", bad)
    registry.register("bars@1", good)
    engine = DataProductEngine(catalog, registry, ArtifactStore(tmp_path / "artifacts"))
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))

    snapshot = engine.build_snapshot(
        cycle_id="cycle-1",
        subject=ResearchSubject(code="600519"),
        boundary=boundary,
        team_refs=["core@1"],
    )

    assert snapshot.product("bars@1").payload == {"rows": [1]}
    assert bad.calls == 1
    assert good.calls == 1
    assert len(snapshot.product("bars@1").attempts) == 2
    assert snapshot.product("bars@1").attempts[1]["payload_hash"]
    assert snapshot.product("bars@1").attempts[1]["schema_version"] == "bars@1"


def test_product_manifest_controls_provider_order_and_membership(tmp_path: Path):
    from advisor.research.contracts import AgentManifest, DataProductManifest, ResearchTeam

    product = DataProductManifest(product="bars@1", title="Bars", providers=("bad", "good"))
    agent = AgentManifest(agent="market@1", title="Market", instructions="inspect", required_products=("bars@1",))
    team = ResearchTeam(team="core@1", title="Core", agents=("market@1",))
    catalog = catalog_for({"bars@1": product}, {"market@1": agent}, {"core@1": team})
    registry = ProviderRegistry()
    unlisted = FakeProvider("unlisted", payload={"rows": ["must not run"]})
    bad = FakeProvider("bad", error=ProductUnavailable("offline"))
    good = FakeProvider("good", payload={"rows": [1]})
    registry.register("bars@1", unlisted)
    registry.register("bars@1", bad)
    registry.register("bars@1", good)

    snapshot = DataProductEngine(catalog, registry, ArtifactStore(tmp_path / "artifacts")).build_snapshot(
        cycle_id="cycle-provider-policy",
        subject=ResearchSubject(code="600519"),
        boundary=ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc)),
        team_refs=["core@1"],
    )

    assert snapshot.product("bars@1").payload == {"rows": [1]}
    assert unlisted.calls == 0
    assert bad.calls == 1
    assert good.calls == 1


def test_missing_provider_blocks_product(tmp_path: Path):
    from advisor.research.contracts import AgentManifest, DataProductManifest, ResearchTeam

    product = DataProductManifest(product="bars@1", title="Bars", providers=("missing",))
    agent = AgentManifest(agent="market@1", title="Market", instructions="inspect", required_products=("bars@1",))
    team = ResearchTeam(team="core@1", title="Core", agents=("market@1",))
    catalog = catalog_for({"bars@1": product}, {"market@1": agent}, {"core@1": team})
    engine = DataProductEngine(catalog, ProviderRegistry(), ArtifactStore(tmp_path / "artifacts"))
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))

    snapshot = engine.build_snapshot(
        cycle_id="cycle-1",
        subject=ResearchSubject(code="600519"),
        boundary=boundary,
        team_refs=["core@1"],
    )
    assert "bars@1" in snapshot.unavailable


def test_product_result_artifact_ref_uses_wrapped_artifact_size(tmp_path: Path):
    from advisor.research.contracts import AgentManifest, DataProductManifest, ResearchTeam

    product = DataProductManifest(product="bars@1", title="Bars", providers=("good",))
    agent = AgentManifest(agent="market@1", title="Market", instructions="inspect", required_products=("bars@1",))
    team = ResearchTeam(team="core@1", title="Core", agents=("market@1",))
    catalog = catalog_for({"bars@1": product}, {"market@1": agent}, {"core@1": team})
    store = ArtifactStore(tmp_path / "artifacts")
    registry = ProviderRegistry()
    registry.register("bars@1", FakeProvider("good", payload={"rows": [1, 2]}))
    engine = DataProductEngine(catalog, registry, store)
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))

    snapshot = engine.build_snapshot(
        cycle_id="cycle-1",
        subject=ResearchSubject(code="600519"),
        boundary=boundary,
        team_refs=["core@1"],
    )

    ref = engine.store_ref(snapshot.product("bars@1"))
    assert ref.byte_size == len(store.read_bytes(ref.content_hash))


def test_product_quality_rejects_missing_required_fields_and_stale_observation(tmp_path: Path):
    from advisor.research.contracts import AgentManifest, DataProductManifest, ResearchTeam
    from datetime import timedelta

    product = DataProductManifest(
        product="bars@1", title="Bars", providers=("bad",), required_fields=("rows",), freshness_minutes=60
    )
    agent = AgentManifest(agent="market@1", title="Market", instructions="inspect", required_products=("bars@1",))
    team = ResearchTeam(team="core@1", title="Core", agents=("market@1",))
    catalog = catalog_for({"bars@1": product}, {"market@1": agent}, {"core@1": team})
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))

    stale = FakeProvider("bad", payload={"wrong": []}, observed_at=boundary.as_of - timedelta(hours=2))
    registry = ProviderRegistry()
    registry.register("bars@1", stale)
    snapshot = DataProductEngine(catalog, registry, ArtifactStore(tmp_path / "artifacts")).build_snapshot(
        cycle_id="cycle-1", subject=ResearchSubject(code="600519"), boundary=boundary, team_refs=["core@1"]
    )
    assert "required fields" in snapshot.unavailable["bars@1"]


def test_product_quality_accepts_legal_empty_result_and_rejects_future_rows(tmp_path: Path):
    from advisor.research.contracts import AgentManifest, DataProductManifest, ResearchTeam

    product = DataProductManifest(product="bars@1", title="Bars", providers=("fixture",), required_fields=("rows",))
    agent = AgentManifest(agent="market@1", title="Market", instructions="inspect", required_products=("bars@1",))
    team = ResearchTeam(team="core@1", title="Core", agents=("market@1",))
    catalog = catalog_for({"bars@1": product}, {"market@1": agent}, {"core@1": team})
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))
    registry = ProviderRegistry()
    registry.register("bars@1", FakeProvider("fixture", payload={"rows": []}))
    snapshot = DataProductEngine(catalog, registry, ArtifactStore(tmp_path / "empty")).build_snapshot(
        cycle_id="cycle-empty", subject=ResearchSubject(code="600519"), boundary=boundary, team_refs=["core@1"]
    )
    assert snapshot.product("bars@1").payload == {"rows": []}

    future_registry = ProviderRegistry()
    future_registry.register(
        "bars@1",
        FakeProvider("fixture", payload={"rows": [{"trade_date": "2026-08-07", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]}),
    )
    future_snapshot = DataProductEngine(catalog, future_registry, ArtifactStore(tmp_path / "future")).build_snapshot(
        cycle_id="cycle-future", subject=ResearchSubject(code="600519"), boundary=boundary, team_refs=["core@1"]
    )
    assert "after as_of" in future_snapshot.unavailable["bars@1"]

    shanghai_monday_registry = ProviderRegistry()
    shanghai_monday_registry.register(
        "bars@1",
        FakeProvider(
            "fixture",
            payload={
                "rows": [
                    {
                        "trade_date": "2026-08-10",
                        "open": 1,
                        "high": 1,
                        "low": 1,
                        "close": 1,
                        "volume": 1,
                    }
                ]
            },
        ),
    )
    shanghai_monday = DataProductEngine(
        catalog, shanghai_monday_registry, ArtifactStore(tmp_path / "shanghai-monday")
    ).build_snapshot(
        cycle_id="cycle-shanghai-monday",
        subject=ResearchSubject(code="600519"),
        boundary=ResearchBoundary(
            as_of=datetime(2026, 8, 9, 16, 30, tzinfo=timezone.utc)
        ),
        team_refs=["core@1"],
    )

    assert shanghai_monday.product("bars@1").payload["rows"][0]["trade_date"] == "2026-08-10"


def test_whole_market_daily_history_orders_rows_by_trade_date_and_security_code(tmp_path: Path):
    from advisor.research.contracts import AgentManifest, DataProductManifest, ResearchTeam

    product_ref = "whole_market_daily_history@1"
    product = DataProductManifest(
        product=product_ref,
        title="Whole-market history",
        providers=("fixture",),
        required_fields=("rows",),
    )
    agent = AgentManifest(
        agent="market_history@1",
        scope=ResearchScope.market,
        title="Market history",
        instructions="inspect",
        required_products=(product_ref,),
    )
    team = ResearchTeam(
        team="market_history@1",
        scope=ResearchScope.market,
        title="Market history",
        agents=("market_history@1",),
    )
    catalog = catalog_for({product_ref: product}, {"market_history@1": agent}, {"market_history@1": team})
    registry = ProviderRegistry()
    registry.register(
        product_ref,
        FakeProvider(
            "fixture",
            payload={
                "rows": [
                    {"code": "000001", "trade_date": "2026-08-05", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
                    {"code": "600000", "trade_date": "2026-08-05", "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2},
                ]
            },
        ),
    )

    store = ArtifactStore(tmp_path / "whole-market")
    snapshot = DataProductEngine(catalog, registry, store).build_snapshot(
        cycle_id="cycle-whole-market-history",
        subject=ResearchSubject(scope=ResearchScope.market),
        boundary=ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc)),
        team_refs=["market_history@1"],
    )

    compact = snapshot.product(product_ref).payload
    assert compact["summary"]["row_count"] == 2
    import gzip

    with gzip.open(store._path_for(compact["__a_hunter_query_artifact__"]), "rt", encoding="utf-8") as handle:
        rows = [json.loads(line)["row"] for line in handle]
    assert compact["__a_hunter_query_format__"] == "ndjson-v1"
    assert rows[1]["code"] == "600000"


def test_restart_stream_migrates_an_oversized_legacy_whole_market_snapshot_once(
    tmp_path: Path,
    monkeypatch,
):
    from advisor.research.contracts import AgentManifest, DataProductManifest, ResearchTeam
    import advisor.research.data_products.engine as engine_module

    product_ref = "whole_market_daily_history@1"
    product = DataProductManifest(
        product=product_ref,
        title="Whole-market history",
        providers=("fixture",),
        required_fields=("rows", "absences", "securities", "sessions", "snapshot_proof"),
    )
    agent = AgentManifest(
        agent="market_history@1",
        scope=ResearchScope.market,
        title="Market history",
        instructions="inspect",
        required_products=(product_ref,),
    )
    team = ResearchTeam(
        team="market_history@1",
        scope=ResearchScope.market,
        title="Market history",
        agents=("market_history@1",),
    )
    catalog = catalog_for({product_ref: product}, {"market_history@1": agent}, {"market_history@1": team})
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))
    subject = ResearchSubject(scope=ResearchScope.market)
    legacy_payload = {
        "status": "passed",
        "rows": [
            {"code": "000001", "trade_date": "2026-08-05", "close": 10},
            {"code": "600000", "trade_date": "2026-08-05", "close": 20},
        ],
        "absences": [{"code": "000002", "trade_date": "2026-08-05", "reason": "suspended"}],
        "securities": ["000001", "000002", "600000"],
        "sessions": ["2026-08-05"],
        "snapshot_proof": {"fixture": True},
    }
    legacy_envelope = {
        "product": product_ref,
        "subject": subject.model_dump(mode="json"),
        "as_of": boundary.as_of.isoformat(),
        "provider": "fixture",
        "observed_at": boundary.as_of.isoformat(),
        "fetched_at": boundary.as_of.isoformat(),
        "schema_version": product_ref,
        "source_locator": "fixture://legacy",
        "quality_status": "passed",
        "quality_message": None,
        "coverage": 1.0,
        "payload": legacy_payload,
        "payload_hash": hashlib.sha256(canonical_json(legacy_payload)).hexdigest(),
    }
    database = tmp_path / "advisor.sqlite"
    artifact_root = tmp_path / "artifacts"
    store = ArtifactStore(artifact_root)
    repository = ResearchRepository.open(database)
    legacy_ref = store.put_json(legacy_envelope)
    cycle_id = "cycle-legacy-large"
    snapshot_id = "snapshot-legacy-large"
    snapshot_material = {
        "as_of": boundary.as_of.isoformat(),
        "products": {product_ref: legacy_ref.content_hash},
        "unavailable": {},
        "subject": subject.model_dump(mode="json"),
    }
    snapshot_hash = hashlib.sha256(canonical_json(snapshot_material)).hexdigest()
    with repository.transaction():
        repository.record_artifact(
            legacy_ref,
            relative_path=str(store._path_for(legacy_ref.content_hash).relative_to(store.root)),
        )
        repository.record_scope_snapshot(
            snapshot_id=snapshot_id,
            cycle_id=cycle_id,
            subject=subject,
            as_of=boundary.as_of.isoformat(),
            product_refs=[product_ref],
            product_hashes={product_ref: legacy_ref.content_hash},
            snapshot_hash=snapshot_hash,
            sealed=True,
        )
        repository.record_scope_snapshot_product(
            snapshot_id=snapshot_id,
            product_ref=product_ref,
            artifact_hash=legacy_ref.content_hash,
            quality_status="passed",
            provider_attempts=[],
        )
    monkeypatch.setattr(engine_module, "LEGACY_STREAM_THRESHOLD_BYTES", 0)

    first = DataProductEngine(catalog, ProviderRegistry(), store, repository).build_snapshot(
        cycle_id=cycle_id,
        subject=subject,
        boundary=boundary,
        team_refs=["market_history@1"],
    )
    backing_hash = first.product(product_ref).payload["__a_hunter_query_artifact__"]
    migration_count = repository.connection.execute(
        "SELECT count(*) FROM research_query_backing_migrations"
    ).fetchone()[0]
    repository.close()
    store.close()

    restarted_store = ArtifactStore(artifact_root)
    restarted_repository = ResearchRepository.open(database)
    replay = DataProductEngine(
        catalog, ProviderRegistry(), restarted_store, restarted_repository
    ).build_snapshot(
        cycle_id=cycle_id,
        subject=subject,
        boundary=boundary,
        team_refs=["market_history@1"],
    )
    try:
        assert migration_count == 1
        assert replay.product(product_ref).payload["__a_hunter_query_artifact__"] == backing_hash
        assert replay.product(product_ref).payload["summary"]["row_count"] == 2
        media_type = restarted_repository.connection.execute(
            "SELECT media_type FROM research_artifacts WHERE content_hash = ?",
            (backing_hash,),
        ).fetchone()[0]
        assert media_type == "application/vnd.a-hunter.query-product+gzip"
    finally:
        restarted_repository.close()
        restarted_store.close()


def test_product_quality_blocks_conflicting_successful_providers(tmp_path: Path):
    from advisor.research.contracts import AgentManifest, DataProductManifest, ResearchTeam

    product = DataProductManifest(product="quote@1", title="Quote", providers=("one", "two"), conflict_tolerance=0.05)
    agent = AgentManifest(agent="market@1", title="Market", instructions="inspect", required_products=("quote@1",))
    team = ResearchTeam(team="core@1", title="Core", agents=("market@1",))
    catalog = catalog_for({"quote@1": product}, {"market@1": agent}, {"core@1": team})
    registry = ProviderRegistry()
    registry.register("quote@1", FakeProvider("one", payload={"price": 100.0}))
    registry.register("quote@1", FakeProvider("two", payload={"price": 120.0}))
    snapshot = DataProductEngine(catalog, registry, ArtifactStore(tmp_path / "conflict")).build_snapshot(
        cycle_id="cycle-conflict", subject=ResearchSubject(code="600519"), boundary=ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc)), team_refs=["core@1"]
    )
    assert "conflict" in snapshot.unavailable["quote@1"]


def test_one_product_failure_is_visible_but_does_not_block_unrelated_products(tmp_path: Path):
    from advisor.research.contracts import AgentManifest, DataProductManifest, ResearchTeam

    products = {
        "bars@1": DataProductManifest(product="bars@1", title="Bars", providers=("good",), required_fields=("rows",)),
        "quote@1": DataProductManifest(product="quote@1", title="Quote", providers=("bad",), required_fields=("price",)),
    }
    agents = {
        "market@1": AgentManifest(agent="market@1", title="Market", instructions="inspect", required_products=("bars@1",)),
        "quote@1": AgentManifest(agent="quote@1", title="Quote", instructions="inspect", required_products=("quote@1",)),
    }
    teams = {
        "core@1": ResearchTeam(team="core@1", title="Core", agents=("market@1",)),
        "quote@1": ResearchTeam(team="quote@1", title="Quote", agents=("quote@1",)),
    }
    catalog = catalog_for(products, agents, teams)
    registry = ProviderRegistry()
    registry.register("bars@1", FakeProvider("good", payload={"rows": []}))
    registry.register("quote@1", FakeProvider("bad", error=ProductUnavailable("source offline")))
    snapshot = DataProductEngine(catalog, registry, ArtifactStore(tmp_path / "partial")).build_snapshot(
        cycle_id="cycle-partial",
        subject=__import__("advisor.research.contracts", fromlist=["ResearchSubject"]).ResearchSubject(code="600519"),
        boundary=__import__("advisor.research.contracts", fromlist=["ResearchBoundary"]).ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc)),
        team_refs=["core@1", "quote@1"],
    )
    assert "bars@1" in snapshot.products
    assert "quote@1" not in snapshot.products
    assert "quote@1" in snapshot.unavailable


def test_market_snapshot_reopens_the_same_sealed_artifacts_after_a_service_restart(tmp_path: Path):
    from advisor.research.contracts import AgentManifest, DataProductManifest, ResearchTeam
    from advisor.research.repository import ResearchRepository

    product = DataProductManifest(product="market_snapshot@1", title="市场快照", providers=("fixture",))
    agent = AgentManifest(
        agent="market_scope@1",
        scope=ResearchScope.market,
        title="市场范围研究",
        instructions="inspect",
        required_products=("market_snapshot@1",),
    )
    team = ResearchTeam(
        team="market_team@1",
        scope=ResearchScope.market,
        title="市场团队",
        agents=("market_scope@1",),
    )
    catalog = catalog_for({"market_snapshot@1": product}, {"market_scope@1": agent}, {"market_team@1": team})
    database = tmp_path / "advisor.sqlite"
    repository = ResearchRepository.open(database)
    store = ArtifactStore(tmp_path / "artifacts")
    boundary = ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc))
    provider = FakeProvider("fixture", payload={"rows": ["sealed"]})
    registry = ProviderRegistry()
    registry.register("market_snapshot@1", provider)

    first = DataProductEngine(catalog, registry, store, repository).build_snapshot(
        cycle_id="market-request-cycle",
        subject=ResearchSubject(scope=ResearchScope.market),
        boundary=boundary,
        team_refs=["market_team@1"],
    )
    assert provider.calls == 1
    persisted = repository.connection.execute(
        "SELECT scope, subject_code FROM research_scope_snapshots WHERE snapshot_id = ?", (first.snapshot_id,)
    ).fetchone()
    assert tuple(persisted) == ("market", None)
    repository.close()

    restarted_repository = ResearchRepository.open(database)
    retry_provider = FakeProvider("fixture", payload={"rows": ["must not fetch"]})
    retry_registry = ProviderRegistry()
    retry_registry.register("market_snapshot@1", retry_provider)
    restored = DataProductEngine(catalog, retry_registry, store, restarted_repository).build_snapshot(
        cycle_id="market-request-cycle",
        subject=ResearchSubject(scope=ResearchScope.market),
        boundary=boundary,
        team_refs=["market_team@1"],
    )

    assert retry_provider.calls == 0
    assert restored == first
