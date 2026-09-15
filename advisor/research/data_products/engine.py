from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Protocol, TextIO
import uuid

from advisor.research.artifacts import ArtifactRef, ArtifactStore
from advisor.research.catalog import ManifestCatalog
from advisor.research.contracts import (
    DataProductManifest,
    ResearchBoundary,
    ResearchSubject,
    VersionRef,
    canonical_json,
)
from advisor.research.market_time import a_share_date
from advisor.research.repository import ResearchRepository


class ProductUnavailable(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


QUERY_BACKING_FORMAT = "ndjson-v1"
QUERY_BACKING_MEDIA_TYPE = "application/vnd.a-hunter.query-product+gzip"
LEGACY_STREAM_THRESHOLD_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class ProductRequest:
    product: VersionRef
    subject: ResearchSubject
    boundary: ResearchBoundary
    # mx_events@2 is materialized once for the union of exact Agent grants.
    # Other Products always receive an empty scope.
    feed_rids: tuple[int, ...] = ()


@dataclass(frozen=True)
class QueryBacking:
    path: Path
    format: str = QUERY_BACKING_FORMAT
    media_type: str = QUERY_BACKING_MEDIA_TYPE


@dataclass(frozen=True)
class ProviderObservation:
    provider: str
    payload: Any
    observed_at: datetime
    source_locator: str | None = None
    quality_status: str = "passed"
    quality_message: str | None = None
    coverage: float = 1.0
    fetched_at: datetime | None = None
    schema_version: str | None = None
    query_backing: QueryBacking | None = None


class ProviderAdapter(Protocol):
    provider_id: str

    def fetch(
        self,
        request: ProductRequest,
        *,
        dependencies: dict[str, Any] | None = None,
    ) -> ProviderObservation:
        raise NotImplementedError


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, list[ProviderAdapter]] = {}

    def register(self, product: str | VersionRef, provider: ProviderAdapter) -> None:
        key = str(VersionRef.parse(product))
        provider_id = getattr(provider, "provider_id", None)
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError("Provider Adapter requires provider_id")
        self._providers.setdefault(key, []).append(provider)

    def providers_for(
        self,
        product: str | VersionRef,
        *,
        provider_ids: tuple[str, ...] | list[str] | None = None,
    ) -> tuple[ProviderAdapter, ...]:
        providers = tuple(self._providers.get(str(VersionRef.parse(product)), ()))
        if provider_ids is None:
            return providers
        by_id = {provider.provider_id: provider for provider in providers}
        return tuple(by_id[provider_id] for provider_id in provider_ids if provider_id in by_id)


@dataclass(frozen=True)
class ProductResult:
    product: VersionRef
    payload: Any
    artifact_hash: str
    provider: str
    quality_status: str
    quality_message: str | None
    attempts: tuple[dict[str, object], ...]
    fetched_at: datetime | None = None
    schema_version: str | None = None
    payload_hash: str | None = None


@dataclass(frozen=True)
class SnapshotResult:
    snapshot_id: str
    subject: ResearchSubject
    boundary: ResearchBoundary
    products: dict[str, ProductResult]
    snapshot_hash: str
    unavailable: dict[str, str] = field(default_factory=dict)

    def product(self, ref: str | VersionRef) -> ProductResult:
        key = str(VersionRef.parse(ref))
        try:
            return self.products[key]
        except KeyError as error:
            raise KeyError(f"Product not in Snapshot: {key}") from error


class DataProductEngine:
    def __init__(
        self,
        catalog: ManifestCatalog,
        registry: ProviderRegistry,
        store: ArtifactStore,
        repository: ResearchRepository | None = None,
    ) -> None:
        self.catalog = catalog
        self.registry = registry
        self.store = store
        self.repository = repository

    def required_products(self, team_refs: list[str | VersionRef]) -> tuple[VersionRef, ...]:
        ordered: list[VersionRef] = []
        visited: set[str] = set()

        def visit(product_ref: VersionRef) -> None:
            key = str(product_ref)
            if key in visited:
                return
            visited.add(key)
            manifest = self.catalog.product(product_ref)
            for dependency in manifest.dependencies:
                visit(dependency)
            ordered.append(product_ref)

        for team_ref in team_refs:
            team = self.catalog.team(team_ref)
            for agent_ref in team.agents:
                agent = self.catalog.agent(agent_ref)
                for product_ref in agent.product_refs:
                    visit(product_ref)
        return tuple(ordered)

    def requested_feed_rids(self, team_refs: list[str | VersionRef]) -> dict[str, tuple[int, ...]]:
        """Return the exact per-Product feed union for this sealed Snapshot."""
        result: dict[str, set[int]] = {}
        for team_ref in team_refs:
            team = self.catalog.team(team_ref)
            for agent_ref in team.agents:
                for access in self.catalog.agent(agent_ref).product_accesses:
                    if access.feed_scope is None:
                        continue
                    result.setdefault(str(access.product), set()).update(access.feed_scope.rids)
        return {key: tuple(sorted(value)) for key, value in result.items()}

    def build_snapshot(
        self,
        *,
        cycle_id: str,
        subject: ResearchSubject,
        boundary: ResearchBoundary,
        team_refs: list[str | VersionRef],
    ) -> SnapshotResult:
        cached = self._load_sealed_snapshot(cycle_id, subject, boundary)
        if cached is not None:
            return cached
        products: dict[str, ProductResult] = {}
        unavailable: dict[str, str] = {}
        requested_feeds = self.requested_feed_rids(team_refs)
        for product_ref in self.required_products(team_refs):
            manifest = self.catalog.product(product_ref)
            dependencies = {
                key: value.payload for key, value in products.items() if key in {str(item) for item in manifest.dependencies}
            }
            dependency_results = {
                key: value for key, value in products.items() if key in {str(item) for item in manifest.dependencies}
            }
            missing_dependencies = [str(item) for item in manifest.dependencies if str(item) not in products]
            if missing_dependencies:
                unavailable[str(product_ref)] = f"dependency unavailable: {', '.join(missing_dependencies)}"
                continue
            try:
                products[str(product_ref)] = self._build_product(
                    product_ref,
                    manifest,
                    subject,
                    boundary,
                    dependencies,
                    dependency_results=dependency_results,
                    feed_rids=requested_feeds.get(str(product_ref), ()),
                )
            except ProductUnavailable as error:
                unavailable[str(product_ref)] = str(error)[:500]

        snapshot_material = {
            "as_of": boundary.as_of.isoformat(),
            "products": {key: value.artifact_hash for key, value in sorted(products.items())},
            "unavailable": unavailable,
            "subject": subject.model_dump(mode="json"),
        }
        snapshot_hash = hashlib.sha256(canonical_json(snapshot_material)).hexdigest()
        snapshot_id = f"snapshot-{hashlib.sha256(f'{cycle_id}|{snapshot_hash}'.encode('utf-8')).hexdigest()[:24]}"
        result = SnapshotResult(snapshot_id, subject, boundary, products, snapshot_hash, unavailable)
        if self.repository is not None:
            with self.repository.transaction():
                if subject.is_market:
                    self.repository.record_scope_snapshot(
                        snapshot_id=snapshot_id,
                        cycle_id=cycle_id,
                        subject=subject,
                        as_of=boundary.as_of.isoformat(),
                        product_refs=list(products),
                        product_hashes={key: value.artifact_hash for key, value in products.items()},
                        unavailable=unavailable,
                        snapshot_hash=snapshot_hash,
                        sealed=True,
                    )
                else:
                    self.repository.record_snapshot(
                        snapshot_id=snapshot_id,
                        cycle_id=cycle_id,
                        subject_code=subject.code,
                        as_of=boundary.as_of.isoformat(),
                        product_refs=list(products),
                        product_hashes={key: value.artifact_hash for key, value in products.items()},
                        unavailable=unavailable,
                        snapshot_hash=snapshot_hash,
                        sealed=True,
                    )
                for key, value in products.items():
                    artifact_path = self.store._path_for(value.artifact_hash).relative_to(self.store.root)
                    self.repository.record_artifact(
                        self.store_ref(value),
                        relative_path=str(artifact_path),
                        retention_class=self.catalog.product(key).retention_class,
                    )
                    query_hash = _query_artifact_hash(value.payload)
                    if query_hash is not None:
                        query_path = self.store._path_for(query_hash)
                        if not query_path.is_file() or query_path.is_symlink():
                            raise ProductUnavailable(
                                "query-backed Product artifact is unavailable",
                                retryable=False,
                            )
                        self.repository.record_artifact(
                            ArtifactRef(query_hash, query_path.stat().st_size, QUERY_BACKING_MEDIA_TYPE),
                            relative_path=str(query_path.relative_to(self.store.root)),
                            retention_class=self.catalog.product(key).retention_class,
                        )
                    if subject.is_market:
                        self.repository.record_scope_snapshot_product(
                            snapshot_id=snapshot_id,
                            product_ref=key,
                            artifact_hash=value.artifact_hash,
                            quality_status=value.quality_status,
                            provider_attempts=list(value.attempts),
                        )
                    else:
                        self.repository.record_snapshot_product(
                            snapshot_id=snapshot_id,
                            product_ref=key,
                            artifact_hash=value.artifact_hash,
                            quality_status=value.quality_status,
                            provider_attempts=list(value.attempts),
                        )
        return result

    def _load_sealed_snapshot(
        self,
        cycle_id: str,
        subject: ResearchSubject,
        boundary: ResearchBoundary,
    ) -> SnapshotResult | None:
        if self.repository is None:
            return None
        if subject.is_market:
            return self._load_scope_snapshot(cycle_id, subject, boundary)
        row = self.repository.connection.execute(
            "SELECT snapshot_id, subject_code, as_of, status, product_hashes_json, unavailable_json, snapshot_hash FROM research_snapshots WHERE cycle_id = ? ORDER BY created_at DESC LIMIT 1",
            (cycle_id,),
        ).fetchone()
        if row is None:
            return None
        if row[1] != subject.code or row[2] != boundary.as_of.isoformat():
            raise ProductUnavailable("sealed Snapshot scope does not match requested boundary", retryable=False)
        if row[3] != "sealed":
            raise ProductUnavailable("previous Snapshot was not sealed", retryable=False)
        products: dict[str, ProductResult] = {}
        product_rows = self.repository.connection.execute(
            "SELECT product_ref, artifact_hash, quality_status, provider_attempts_json FROM research_snapshot_products WHERE snapshot_id = ? ORDER BY product_ref",
            (row[0],),
        ).fetchall()
        try:
            for product_ref, artifact_hash, quality_status, attempts_json in product_rows:
                envelope = self._load_product_envelope(product_ref, artifact_hash)
                products[product_ref] = ProductResult(
                    VersionRef.parse(product_ref),
                    envelope["payload"],
                    artifact_hash,
                    envelope["provider"],
                    quality_status,
                    envelope.get("quality_message"),
                    tuple(json.loads(attempts_json)),
                    _parse_stored_datetime(envelope.get("fetched_at")),
                    envelope.get("schema_version"),
                    envelope.get("payload_hash"),
                )
            unavailable = json.loads(row[5])
            expected_hash = hashlib.sha256(canonical_json({
                "as_of": boundary.as_of.isoformat(),
                "products": {key: value.artifact_hash for key, value in sorted(products.items())},
                "unavailable": unavailable,
                "subject": subject.model_dump(mode="json"),
            })).hexdigest()
            if expected_hash != row[6] or json.loads(row[4]) != {key: value.artifact_hash for key, value in sorted(products.items())} or not isinstance(unavailable, dict):
                raise ValueError("sealed Snapshot hash mismatch")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ProductUnavailable("sealed Snapshot artifact is unavailable or corrupt", retryable=False) from error
        return SnapshotResult(row[0], subject, boundary, products, row[6], unavailable)

    def _load_scope_snapshot(
        self,
        cycle_id: str,
        subject: ResearchSubject,
        boundary: ResearchBoundary,
    ) -> SnapshotResult | None:
        if self.repository is None:
            return None
        stored = self.repository.scope_snapshot_for_cycle(cycle_id)
        if stored is None:
            return None
        if stored["subject"] != subject or stored["boundary"] != boundary:
            raise ProductUnavailable("sealed Snapshot scope does not match requested boundary", retryable=False)
        raw_unavailable = stored["unavailable"]
        if not isinstance(raw_unavailable, dict):
            raise ProductUnavailable("sealed scope Snapshot unavailable map is invalid", retryable=False)
        products: dict[str, ProductResult] = {}
        try:
            product_rows = stored["products"]
            if not isinstance(product_rows, tuple):
                raise ValueError("scope snapshot products are invalid")
            for product_ref, artifact_hash, quality_status, attempts_json in product_rows:
                envelope = self._load_product_envelope(str(product_ref), artifact_hash)
                products[str(product_ref)] = ProductResult(
                    VersionRef.parse(product_ref),
                    envelope["payload"],
                    artifact_hash,
                    envelope["provider"],
                    quality_status,
                    envelope.get("quality_message"),
                    tuple(json.loads(attempts_json)),
                    _parse_stored_datetime(envelope.get("fetched_at")),
                    envelope.get("schema_version"),
                    envelope.get("payload_hash"),
                )
            expected_hash = hashlib.sha256(canonical_json({
                "as_of": boundary.as_of.isoformat(),
                "products": {key: value.artifact_hash for key, value in sorted(products.items())},
                "unavailable": raw_unavailable,
                "subject": subject.model_dump(mode="json"),
            })).hexdigest()
            if expected_hash != stored["snapshot_hash"]:
                raise ValueError("scope snapshot hash mismatch")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ProductUnavailable("sealed scope Snapshot artifact is unavailable or corrupt", retryable=False) from error
        snapshot_hash = stored["snapshot_hash"]
        snapshot_id = stored["snapshot_id"]
        if not isinstance(snapshot_hash, str) or not isinstance(snapshot_id, str):
            raise ProductUnavailable("sealed scope Snapshot is invalid", retryable=False)
        return SnapshotResult(
            snapshot_id,
            subject,
            boundary,
            products,
            snapshot_hash,
            {str(key): str(value) for key, value in raw_unavailable.items()},
        )

    def _load_product_envelope(self, product_ref: str, artifact_hash: str) -> dict[str, Any]:
        if product_ref != "whole_market_daily_history@1":
            envelope = self.store.read_json(artifact_hash)
            if not isinstance(envelope, dict):
                raise ValueError("stored Product envelope is invalid")
            return envelope
        migration = self.repository.query_backing_migration(artifact_hash) if self.repository is not None else None
        if migration is not None:
            backing_hash = migration["backing_artifact_hash"]
            envelope = migration["envelope"]
            payload = envelope.get("payload")
            if (
                not isinstance(payload, dict)
                or payload.get("__a_hunter_query_artifact__") != backing_hash
                or payload.get("__a_hunter_query_format__") != QUERY_BACKING_FORMAT
                or not self.store.verify(artifact_hash)
                or not self.store.verify(backing_hash)
            ):
                raise ValueError("stored query backing migration is invalid")
            return envelope
        source_path = self.store._path_for(artifact_hash)
        if source_path.stat().st_size <= LEGACY_STREAM_THRESHOLD_BYTES:
            envelope = self.store.read_json(artifact_hash)
            if not isinstance(envelope, dict):
                raise ValueError("stored Product envelope is invalid")
            return envelope
        if not self.store.verify(artifact_hash):
            raise ValueError("legacy Product Artifact is unavailable or corrupt")
        envelope, backing_ref = _migrate_legacy_whole_market_envelope(source_path, self.store)
        if self.repository is not None:
            with self.repository.transaction():
                self.repository.record_artifact(
                    backing_ref,
                    relative_path=str(self.store._path_for(backing_ref.content_hash).relative_to(self.store.root)),
                    retention_class=self.catalog.product(product_ref).retention_class,
                )
                self.repository.record_query_backing_migration(
                    source_artifact_hash=artifact_hash,
                    backing_artifact_hash=backing_ref.content_hash,
                    migrated_envelope=envelope,
                )
        return envelope

    def _build_product(
        self,
        product_ref: VersionRef,
        manifest: DataProductManifest,
        subject: ResearchSubject,
        boundary: ResearchBoundary,
        dependencies: dict[str, Any],
        *,
        dependency_results: dict[str, ProductResult] | None = None,
        feed_rids: tuple[int, ...] = (),
    ) -> ProductResult:
        if manifest.derived:
            return self._build_derived_product(
                product_ref,
                manifest,
                subject,
                boundary,
                dependencies,
                dependency_results=dependency_results or {},
            )
        request = ProductRequest(product_ref, subject, boundary, tuple(sorted(feed_rids)))
        providers = self.registry.providers_for(product_ref, provider_ids=manifest.providers)
        if not providers:
            raise ProductUnavailable(f"no Provider Adapter registered for {product_ref}", retryable=False)
        attempts: list[dict[str, object]] = []
        observations: list[ProviderObservation] = []
        last_error: ProductUnavailable | None = None
        for provider in providers:
            started = datetime.now(boundary.as_of.tzinfo).isoformat()
            observation: ProviderObservation | None = None
            try:
                observation = provider.fetch(request, dependencies=dependencies)
                if observation.quality_status == "blocked":
                    raise ProductUnavailable(observation.quality_message or "provider returned blocked quality", retryable=False)
                if observation.quality_status == "warning" and observation.coverage <= 0:
                    raise ProductUnavailable(
                        observation.quality_message or "provider returned no usable coverage",
                        retryable=False,
                    )
                self._validate_observation(observation, boundary, manifest, product_ref)
            except ProductUnavailable as error:
                if observation is not None:
                    _discard_query_backing(observation)
                last_error = error
                attempts.append({"provider": provider.provider_id, "status": "failed", "message": str(error), "started_at": started})
                continue
            except Exception as error:
                if observation is not None:
                    _discard_query_backing(observation)
                last_error = ProductUnavailable(type(error).__name__, retryable=False)
                attempts.append({"provider": provider.provider_id, "status": "failed", "message": type(error).__name__, "started_at": started})
                continue
            attempts.append(
                {
                    "provider": provider.provider_id,
                    "status": "passed",
                    "started_at": started,
                    "source_locator": observation.source_locator,
                    "observed_at": observation.observed_at.isoformat(),
                    "fetched_at": (observation.fetched_at or observation.observed_at).isoformat(),
                    "schema_version": observation.schema_version or str(product_ref),
                    "payload_hash": hashlib.sha256(canonical_json(observation.payload)).hexdigest(),
                }
            )
            observations.append(observation)
            if manifest.conflict_tolerance is None:
                return self._persist_product(
                    product_ref,
                    subject,
                    boundary,
                    observation,
                    attempts,
                    requested_feed_rids=request.feed_rids,
                )
        if observations:
            if len(observations) > 1 and _conflicts(
                [observation.payload for observation in observations], manifest.conflict_tolerance or 0.0
            ):
                for observation in observations:
                    _discard_query_backing(observation)
                for attempt in attempts:
                    if attempt["status"] == "passed":
                        attempt["status"] = "conflicted"
                raise ProductUnavailable(f"conflict detected for {product_ref}", retryable=False)
            return self._persist_product(
                product_ref,
                subject,
                boundary,
                observations[0],
                attempts,
                requested_feed_rids=request.feed_rids,
            )
        if last_error is not None:
            raise last_error
        raise ProductUnavailable(f"all Provider Adapters failed for {product_ref}", retryable=False)

    def _build_derived_product(
        self,
        product_ref: VersionRef,
        manifest: DataProductManifest,
        subject: ResearchSubject,
        boundary: ResearchBoundary,
        dependencies: dict[str, Any],
        *,
        dependency_results: dict[str, ProductResult],
    ) -> ProductResult:
        """Build deterministic derived products without routing them to a Provider Adapter."""
        if product_ref.id not in {"market_indicators", "trading_calendar"}:
            raise ProductUnavailable(f"no derived implementation registered for {product_ref}", retryable=False)
        bars = dependencies.get("market_daily_bars@1", {})
        raw_rows = bars.get("raw_rows", bars.get("rows", [])) if isinstance(bars, dict) else []
        research_series = bars.get("research_price_series", {}) if isinstance(bars, dict) else {}
        research_rows = research_series.get("bars", []) if isinstance(research_series, dict) else []
        # Legacy fixed fixtures may only carry rows.  Live Market Daily
        # products always carry the explicit forward-adjusted series.
        if isinstance(research_rows, list) and research_rows:
            raw_by_date = {
                row.get("trade_date"): row
                for row in raw_rows
                if isinstance(row, dict) and isinstance(row.get("trade_date"), str)
            }
            rows = [
                {
                    **row,
                    "volume": raw_by_date.get(row.get("trade_date"), {}).get("volume"),
                    "amount": raw_by_date.get(row.get("trade_date"), {}).get("amount"),
                }
                for row in research_rows
                if isinstance(row, dict)
            ]
        else:
            rows = raw_rows
        input_artifacts = {
            key: value.artifact_hash for key, value in sorted(dependency_results.items())
        }
        if product_ref.id == "trading_calendar":
            observed = bars.get("observed_sessions", []) if isinstance(bars, dict) else []
            source_sessions = observed if isinstance(observed, list) and observed else rows
            sessions = [
                {"trade_date": row["trade_date"], "is_open": True}
                for row in source_sessions
                if isinstance(row, dict) and isinstance(row.get("trade_date"), str)
            ]
            payload = {
                "status": "passed" if sessions else "warning",
                "sessions": sessions,
                "latest_session": sessions[-1]["trade_date"] if sessions else None,
                "input_artifacts": input_artifacts,
                "algorithm_version": "trading-calendar@1",
            }
            observation = ProviderObservation(
                provider="a-hunter-derived",
                payload=payload,
                observed_at=boundary.as_of,
                source_locator=f"derived://{product_ref}",
                quality_status="passed" if sessions else "warning",
                quality_message=None if sessions else "market_daily_bars is empty",
                coverage=1.0,
            )
            return self._persist_product(product_ref, subject, boundary, observation, [{
                "provider": observation.provider,
                "status": "passed",
                "started_at": boundary.as_of.isoformat(),
                "source_locator": observation.source_locator,
            }])
        closes = [float(row["close"]) for row in rows if isinstance(row, dict) and _finite_number(row.get("close"))]
        volumes = [float(row["volume"]) for row in rows if isinstance(row, dict) and _finite_number(row.get("volume"))]

        def average(window: int, values: list[float]) -> float | None:
            if len(values) < window:
                return None
            return sum(values[-window:]) / window

        payload = {
            "status": "passed" if closes else "warning",
            "latest_close": closes[-1] if closes else None,
            "change_pct": ((closes[-1] / closes[-2]) - 1) * 100 if len(closes) > 1 and closes[-2] else None,
            "sma_5": average(5, closes),
            "sma_20": average(20, closes),
            "sma_60": average(60, closes),
            "volume_5": average(5, volumes),
            "volume_20": average(20, volumes),
            "observations": len(closes),
            "price_basis": "forward_adjusted" if research_rows else "unadjusted_fixture",
            "factor_set_hash": research_series.get("factor_set_hash") if isinstance(research_series, dict) else None,
            "input_artifacts": input_artifacts,
            "algorithm_version": "market-indicators@1",
        }
        observation = ProviderObservation(
            provider="a-hunter-derived",
            payload=payload,
            observed_at=boundary.as_of,
            source_locator=f"derived://{product_ref}",
            quality_status="passed" if closes else "warning",
            quality_message=None if closes else "market_daily_bars is empty",
            coverage=1.0,
        )
        return self._persist_product(product_ref, subject, boundary, observation, [{
            "provider": observation.provider,
            "status": "passed",
            "started_at": boundary.as_of.isoformat(),
            "source_locator": observation.source_locator,
        }])

    def _persist_product(
        self,
        product_ref: VersionRef,
        subject: ResearchSubject,
        boundary: ResearchBoundary,
        observation: ProviderObservation,
        attempts: list[dict[str, object]],
        *,
        requested_feed_rids: tuple[int, ...] = (),
    ) -> ProductResult:
        if product_ref.id == "mx_events" and product_ref.version == 2:
            return self._persist_scoped_mx_product(
                product_ref,
                subject,
                boundary,
                observation,
                attempts,
                requested_feed_rids=requested_feed_rids,
            )
        audit_attempts = [dict(attempt) for attempt in attempts]
        persisted_payload = observation.payload
        if product_ref.id == "whole_market_daily_history" and isinstance(observation.payload, dict):
            backing = observation.query_backing or _write_whole_market_query_backing(
                observation.payload,
                directory=self.store.root,
            )
            if backing.format != QUERY_BACKING_FORMAT or backing.media_type != QUERY_BACKING_MEDIA_TYPE:
                _discard_query_backing_path(backing.path)
                raise ProductUnavailable("query-backed Product format is invalid", retryable=False)
            try:
                query_artifact = self.store.adopt_file(
                    backing.path,
                    media_type=QUERY_BACKING_MEDIA_TYPE,
                )
            except (OSError, ValueError) as error:
                _discard_query_backing_path(backing.path)
                raise ProductUnavailable("query-backed Product artifact could not be sealed", retryable=False) from error
            persisted_payload = {
                "status": observation.payload.get("status"),
                "__a_hunter_query_artifact__": query_artifact.content_hash,
                "__a_hunter_query_format__": QUERY_BACKING_FORMAT,
                "summary": _whole_market_history_summary(observation.payload),
            }
            payload_hash = hashlib.sha256(canonical_json(persisted_payload)).hexdigest()
        else:
            if observation.query_backing is not None:
                _discard_query_backing(observation)
                raise ProductUnavailable("only whole-market history may use query backing", retryable=False)
            payload_hash = _attempt_payload_hash(attempts) or hashlib.sha256(
                canonical_json(observation.payload)
            ).hexdigest()
        for attempt in audit_attempts:
            if attempt.get("status") in {"passed", "conflicted"}:
                attempt.setdefault("observed_at", observation.observed_at.isoformat())
                attempt.setdefault("fetched_at", (observation.fetched_at or observation.observed_at).isoformat())
                attempt.setdefault("schema_version", observation.schema_version or str(product_ref))
                attempt["payload_hash"] = payload_hash
        payload = {
            "product": str(product_ref),
            "subject": subject.model_dump(mode="json"),
            "as_of": boundary.as_of.isoformat(),
            "provider": observation.provider,
            "observed_at": observation.observed_at.isoformat(),
            "fetched_at": (observation.fetched_at or observation.observed_at or datetime.now(timezone.utc)).isoformat(),
            "schema_version": observation.schema_version or str(product_ref),
            "source_locator": observation.source_locator,
            "quality_status": observation.quality_status,
            "quality_message": observation.quality_message,
            "coverage": observation.coverage,
            "payload": persisted_payload,
        }
        payload["payload_hash"] = payload_hash
        artifact = self.store.put_json(payload, media_type="application/json")
        return ProductResult(
            product_ref,
            persisted_payload,
            artifact.content_hash,
            observation.provider,
            observation.quality_status,
            observation.quality_message,
            tuple(audit_attempts),
            datetime.fromisoformat(payload["fetched_at"]),
            payload["schema_version"],
            payload["payload_hash"],
        )

    def _persist_scoped_mx_product(
        self,
        product_ref: VersionRef,
        subject: ResearchSubject,
        boundary: ResearchBoundary,
        observation: ProviderObservation,
        attempts: list[dict[str, object]],
        *,
        requested_feed_rids: tuple[int, ...],
    ) -> ProductResult:
        """Seal every RID Feed independently, then seal a small product index.

        The index deliberately has no event content.  It can be shared by the
        Snapshot planner without letting a Capsule inspect another Agent's
        scoped Feed.
        """
        feeds = _validated_mx_feeds(
            observation.payload,
            requested_feed_rids=requested_feed_rids,
            boundary=boundary,
        )
        index_feeds: list[dict[str, object]] = []
        for feed in feeds:
            feed_payload = {
                "product": str(product_ref),
                "rid": feed["rid"],
                "window": feed["window"],
                "event_count": feed["event_count"],
                "quality": feed["quality"],
                "content_hash": feed["content_hash"],
                "items": feed["items"],
            }
            artifact = self.store.put_json(
                feed_payload,
                media_type="application/vnd.a-hunter.mx-rid-feed+json",
            )
            index_feeds.append({
                key: feed_payload[key]
                for key in ("rid", "window", "event_count", "quality", "content_hash")
            } | {"artifact_hash": artifact.content_hash})
        index_payload: dict[str, object] = {
            "status": "passed",
            "feeds": index_feeds,
        }
        payload_hash = hashlib.sha256(canonical_json(index_payload)).hexdigest()
        audit_attempts = [dict(attempt) for attempt in attempts]
        for attempt in audit_attempts:
            if attempt.get("status") in {"passed", "conflicted"}:
                attempt.setdefault("observed_at", observation.observed_at.isoformat())
                attempt.setdefault("fetched_at", (observation.fetched_at or observation.observed_at).isoformat())
                attempt.setdefault("schema_version", observation.schema_version or str(product_ref))
                attempt.setdefault("payload_hash", payload_hash)
        envelope = {
            "product": str(product_ref),
            "subject": subject.model_dump(mode="json"),
            "as_of": boundary.as_of.isoformat(),
            "provider": observation.provider,
            "observed_at": observation.observed_at.isoformat(),
            "fetched_at": (observation.fetched_at or observation.observed_at).isoformat(),
            "schema_version": observation.schema_version or str(product_ref),
            "source_locator": observation.source_locator,
            "quality_status": observation.quality_status,
            "quality_message": observation.quality_message,
            "coverage": observation.coverage,
            "payload": index_payload,
            "payload_hash": payload_hash,
        }
        artifact = self.store.put_json(envelope, media_type="application/json")
        return ProductResult(
            product_ref,
            index_payload,
            artifact.content_hash,
            observation.provider,
            observation.quality_status,
            observation.quality_message,
            tuple(audit_attempts),
            datetime.fromisoformat(envelope["fetched_at"]),
            envelope["schema_version"],
            payload_hash,
        )

    @staticmethod
    def _validate_observation(observation: ProviderObservation, boundary: ResearchBoundary, manifest: DataProductManifest | None = None, product_ref: VersionRef | None = None) -> None:
        if not isinstance(observation.provider, str) or not observation.provider.strip():
            raise ProductUnavailable("observation provider is missing", retryable=False)
        if observation.observed_at.tzinfo is None or observation.observed_at.utcoffset() is None:
            raise ProductUnavailable("observation time must be timezone-aware", retryable=False)
        if observation.fetched_at is not None and (
            observation.fetched_at.tzinfo is None or observation.fetched_at.utcoffset() is None
        ):
            raise ProductUnavailable("fetched_at must be timezone-aware", retryable=False)
        if observation.schema_version is not None and (
            not isinstance(observation.schema_version, str) or not observation.schema_version.strip()
        ):
            raise ProductUnavailable("schema_version is invalid", retryable=False)
        if observation.query_backing is not None and (
            product_ref is None or product_ref.id != "whole_market_daily_history"
        ):
            raise ProductUnavailable("query backing is not allowed for this Product", retryable=False)
        if observation.observed_at > boundary.as_of:
            raise ProductUnavailable("observation is after as_of", retryable=False)
        if observation.quality_status not in {"passed", "warning"}:
            raise ProductUnavailable("invalid observation quality", retryable=False)
        if not isinstance(observation.coverage, (int, float)) or isinstance(observation.coverage, bool) or not 0 <= float(observation.coverage) <= 1:
            raise ProductUnavailable("invalid observation coverage", retryable=False)
        if manifest is not None:
            try:
                _validate_schema(observation.payload, manifest.result_schema, path="product")
            except ValueError as error:
                raise ProductUnavailable(str(error), retryable=False) from error
            missing = [field for field in manifest.required_fields if not isinstance(observation.payload, dict) or field not in observation.payload]
            if missing:
                raise ProductUnavailable(f"required fields missing: {', '.join(missing)}", retryable=False)
            if observation.coverage < manifest.minimum_coverage:
                raise ProductUnavailable("minimum coverage not met", retryable=False)
            if manifest.freshness_minutes is not None and (
                boundary.as_of - observation.observed_at
            ).total_seconds() > manifest.freshness_minutes * 60:
                raise ProductUnavailable("observation is stale", retryable=False)
            _validate_payload_dates(observation.payload, boundary, product_ref)

    def store_ref(self, result: ProductResult):
        from advisor.research.artifacts import ArtifactRef

        # ProductResult.artifact_hash addresses the complete envelope, not only
        # the provider payload. Verify and measure the immutable stored bytes.
        value = self.store.read_bytes(result.artifact_hash)
        return ArtifactRef(result.artifact_hash, len(value), "application/json")


_MX_SAFE_FORBIDDEN_KEYS = frozenset({
    "raw_payload",
    "raw_payload_hash",
    "source_url",
    "local_path",
    "cookie",
    "cookies",
    "token",
    "authorization",
    "debugger_url",
    "websocket_debugger_url",
    "chrome_debugging_id",
})


def _validated_mx_feeds(
    payload: Any,
    *,
    requested_feed_rids: tuple[int, ...],
    boundary: ResearchBoundary,
) -> tuple[dict[str, Any], ...]:
    """Validate an untrusted LocalMxProvider result before it enters storage."""
    if not requested_feed_rids:
        raise ProductUnavailable("mx_events@2 is missing an explicit RID scope", retryable=False)
    if not isinstance(payload, dict) or not isinstance(payload.get("feeds"), list):
        raise ProductUnavailable("mx_events@2 feed result is invalid", retryable=False)
    feeds = payload["feeds"]
    if len(feeds) != len(requested_feed_rids):
        raise ProductUnavailable("mx_events@2 returned an incomplete RID feed set", retryable=False)
    expected = set(requested_feed_rids)
    found: set[int] = set()
    normalized: list[dict[str, Any]] = []
    for raw_feed in feeds:
        if not isinstance(raw_feed, dict):
            raise ProductUnavailable("mx_events@2 feed is invalid", retryable=False)
        rid = raw_feed.get("rid")
        if type(rid) is not int or rid not in expected or rid in found:
            raise ProductUnavailable("mx_events@2 RID feed is invalid", retryable=False)
        found.add(rid)
        window = raw_feed.get("window")
        quality = raw_feed.get("quality")
        items = raw_feed.get("items")
        event_count = raw_feed.get("event_count")
        content_hash = raw_feed.get("content_hash")
        if (
            not isinstance(window, dict)
            or set(window) != {"start_exclusive", "end_inclusive"}
            or not all(isinstance(value, str) for value in window.values())
            or not isinstance(quality, dict)
            or set(quality) != {"status", "reason"}
            or quality.get("status") not in {"passed", "blocked", "unavailable"}
            or not isinstance(quality.get("reason"), str)
            or len(quality["reason"]) > 240
            or not isinstance(items, list)
            or len(items) > 5_000
            or type(event_count) is not int
            or event_count != len(items)
            or not isinstance(content_hash, str)
            or len(content_hash) != 64
            or any(char not in "0123456789abcdef" for char in content_hash)
        ):
            raise ProductUnavailable("mx_events@2 feed fields are invalid", retryable=False)
        try:
            start = datetime.fromisoformat(window["start_exclusive"])
            end = datetime.fromisoformat(window["end_inclusive"])
        except ValueError as error:
            raise ProductUnavailable("mx_events@2 feed window is invalid", retryable=False) from error
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or start.utcoffset() is None
            or end.utcoffset() is None
            or end != boundary.as_of
            or start >= end
        ):
            raise ProductUnavailable("mx_events@2 feed window is invalid", retryable=False)
        _validate_safe_mx_value(items)
        actual_hash = hashlib.sha256(
            canonical_json({"rid": rid, "window": window, "items": items})
        ).hexdigest()
        if actual_hash != content_hash:
            raise ProductUnavailable("mx_events@2 feed content hash is invalid", retryable=False)
        if quality["status"] == "passed" and quality["reason"]:
            raise ProductUnavailable("mx_events@2 passed feed has a quality reason", retryable=False)
        if quality["status"] != "passed" and items:
            raise ProductUnavailable("mx_events@2 blocked feed exposes events", retryable=False)
        normalized.append({
            "rid": rid,
            "window": {"start_exclusive": window["start_exclusive"], "end_inclusive": window["end_inclusive"]},
            "event_count": event_count,
            "quality": {"status": quality["status"], "reason": quality["reason"]},
            "content_hash": content_hash,
            "items": items,
        })
    if found != expected:
        raise ProductUnavailable("mx_events@2 returned an incomplete RID feed set", retryable=False)
    return tuple(sorted(normalized, key=lambda item: item["rid"]))


def _validate_safe_mx_value(value: Any, *, depth: int = 0) -> None:
    if depth > 20:
        raise ProductUnavailable("mx_events@2 feed nesting is invalid", retryable=False)
    if isinstance(value, dict):
        if len(value) > 100:
            raise ProductUnavailable("mx_events@2 feed item is too large", retryable=False)
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 120 or key.lower() in _MX_SAFE_FORBIDDEN_KEYS:
                raise ProductUnavailable("mx_events@2 feed contains an unsafe field", retryable=False)
            _validate_safe_mx_value(child, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > 5_000:
            raise ProductUnavailable("mx_events@2 feed item is too large", retryable=False)
        for child in value:
            _validate_safe_mx_value(child, depth=depth + 1)
    elif isinstance(value, str):
        if len(value) > 12_000:
            raise ProductUnavailable("mx_events@2 feed text is too large", retryable=False)
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise ProductUnavailable("mx_events@2 feed value is invalid", retryable=False)


def _attempt_payload_hash(attempts: list[dict[str, object]]) -> str | None:
    for attempt in reversed(attempts):
        value = attempt.get("payload_hash")
        if attempt.get("status") in {"passed", "conflicted"} and isinstance(value, str):
            if len(value) == 64 and all(char in "0123456789abcdef" for char in value):
                return value
    return None


def _write_whole_market_query_backing(
    payload: dict[str, Any],
    *,
    directory: Path,
) -> QueryBacking:
    """Write fixture/legacy rows incrementally in the production query format."""

    rows = payload.get("rows")
    absences = payload.get("absences", [])
    if not isinstance(rows, list) or not isinstance(absences, list):
        raise ProductUnavailable("whole-market query rows are invalid", retryable=False)
    fd, raw_path = tempfile.mkstemp(prefix=".whole-market-query-", suffix=".jsonl.gz", dir=directory)
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "wb") as raw_handle:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=6,
                fileobj=raw_handle,
                mtime=0,
            ) as compressed:
                for kind, values in (
                    ("bar", rows),
                    ("absence", absences),
                    ("security", payload.get("securities", [])),
                    ("session", payload.get("sessions", [])),
                ):
                    if not isinstance(values, list):
                        raise ProductUnavailable("whole-market query records are invalid", retryable=False)
                    for row in values:
                        if kind == "security" and isinstance(row, str):
                            row = {"code": row}
                        elif kind == "session" and isinstance(row, str):
                            row = {"trade_date": row}
                        if not isinstance(row, dict):
                            raise ProductUnavailable("whole-market query row is invalid", retryable=False)
                        compressed.write(canonical_json({"kind": kind, "row": row}) + b"\n")
            raw_handle.flush()
            os.fsync(raw_handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return QueryBacking(path)


class _JsonCursor:
    """Small incremental JSON cursor used only for oversized legacy Artifacts."""

    def __init__(self, handle: TextIO) -> None:
        self.handle = handle
        self.buffer = ""
        self.position = 0
        self.eof = False
        self.decoder = json.JSONDecoder()

    def _fill(self) -> bool:
        if self.position:
            self.buffer = self.buffer[self.position:]
            self.position = 0
        chunk = self.handle.read(64 * 1024)
        if not chunk:
            self.eof = True
            return False
        self.buffer += chunk
        return True

    def _skip_space(self) -> None:
        while True:
            while self.position < len(self.buffer) and self.buffer[self.position].isspace():
                self.position += 1
            if self.position < len(self.buffer) or not self._fill():
                return

    def peek(self) -> str:
        self._skip_space()
        if self.position >= len(self.buffer):
            raise ValueError("legacy Product JSON ended unexpectedly")
        return self.buffer[self.position]

    def expect(self, token: str) -> None:
        if self.peek() != token:
            raise ValueError("legacy Product JSON structure is invalid")
        self.position += 1

    def value(self, *, max_chars: int = 2_000_000) -> Any:
        self._skip_space()
        if self.position:
            self.buffer = self.buffer[self.position:]
            self.position = 0
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer, 0)
            except json.JSONDecodeError as error:
                if len(self.buffer) > max_chars or self.eof:
                    raise ValueError("legacy Product JSON value is invalid or oversized") from error
                self._fill()
                continue
            if end > max_chars:
                raise ValueError("legacy Product JSON value is oversized")
            self.position = end
            return value

    def finish(self) -> None:
        self._skip_space()
        if self.position < len(self.buffer) or self._fill():
            self._skip_space()
            if self.position < len(self.buffer):
                raise ValueError("legacy Product JSON has trailing content")


class _EngineQueryWriter:
    def __init__(self, directory: Path) -> None:
        fd, raw_path = tempfile.mkstemp(
            prefix=".legacy-whole-market-",
            suffix=".jsonl.gz",
            dir=directory,
        )
        self.path = Path(raw_path)
        self.raw = os.fdopen(fd, "wb")
        self.compressed = gzip.GzipFile(
            filename="", mode="wb", compresslevel=6, fileobj=self.raw, mtime=0
        )
        self.finished = False

    def write(self, kind: str, row: dict[str, Any]) -> None:
        self.compressed.write(canonical_json({"kind": kind, "row": row}) + b"\n")

    def finish(self) -> Path:
        if not self.finished:
            self.compressed.close()
            self.raw.flush()
            os.fsync(self.raw.fileno())
            self.raw.close()
            self.finished = True
        return self.path

    def abort(self) -> None:
        if not self.finished:
            try:
                self.compressed.close()
            except OSError:
                pass
            try:
                self.raw.close()
            except OSError:
                pass
            self.finished = True
        self.path.unlink(missing_ok=True)


def _migrate_legacy_whole_market_envelope(
    source_path: Path,
    store: ArtifactStore,
) -> tuple[dict[str, Any], ArtifactRef]:
    writer = _EngineQueryWriter(store.staging_dir)
    envelope: dict[str, Any] = {}
    payload_status: Any = None
    snapshot_proof: dict[str, Any] = {}
    counts = {"bar": 0, "absence": 0, "security": 0, "session": 0}
    payload_fields: set[str] = set()

    def stream_array(cursor: _JsonCursor, kind: str) -> None:
        cursor.expect("[")
        if cursor.peek() == "]":
            cursor.expect("]")
            return
        while True:
            row = cursor.value()
            if kind == "security" and isinstance(row, str):
                row = {"code": row}
            elif kind == "session" and isinstance(row, str):
                row = {"trade_date": row}
            if not isinstance(row, dict):
                raise ValueError("legacy whole-market row is invalid")
            writer.write(kind, row)
            counts[kind] += 1
            delimiter = cursor.peek()
            if delimiter == "]":
                cursor.expect("]")
                return
            cursor.expect(",")

    def parse_payload(cursor: _JsonCursor) -> None:
        nonlocal payload_status, snapshot_proof
        cursor.expect("{")
        if cursor.peek() == "}":
            cursor.expect("}")
            return
        kinds = {
            "rows": "bar",
            "absences": "absence",
            "securities": "security",
            "sessions": "session",
        }
        while True:
            key = cursor.value(max_chars=1024)
            if not isinstance(key, str):
                raise ValueError("legacy whole-market payload key is invalid")
            if key in payload_fields:
                raise ValueError("legacy whole-market payload contains a duplicate key")
            payload_fields.add(key)
            cursor.expect(":")
            if key in kinds:
                stream_array(cursor, kinds[key])
            elif key == "snapshot_proof":
                value = cursor.value(max_chars=4_000_000)
                if not isinstance(value, dict):
                    raise ValueError("legacy whole-market proof is invalid")
                snapshot_proof = value
            elif key == "status":
                payload_status = cursor.value(max_chars=1024)
            else:
                raise ValueError("legacy whole-market payload contains an unknown field")
            delimiter = cursor.peek()
            if delimiter == "}":
                cursor.expect("}")
                return
            cursor.expect(",")

    try:
        with source_path.open("r", encoding="utf-8") as handle:
            cursor = _JsonCursor(handle)
            cursor.expect("{")
            envelope_fields: set[str] = set()
            while cursor.peek() != "}":
                key = cursor.value(max_chars=1024)
                if not isinstance(key, str):
                    raise ValueError("legacy Product envelope key is invalid")
                if key in envelope_fields:
                    raise ValueError("legacy Product envelope contains a duplicate key")
                envelope_fields.add(key)
                cursor.expect(":")
                if key == "payload":
                    parse_payload(cursor)
                else:
                    envelope[key] = cursor.value(max_chars=4_000_000)
                if cursor.peek() == "}":
                    break
                cursor.expect(",")
            cursor.expect("}")
            cursor.finish()
        required_payload_fields = {
            "status", "rows", "absences", "securities", "sessions", "snapshot_proof",
        }
        if payload_fields != required_payload_fields:
            raise ValueError("legacy whole-market payload fields are incomplete")
        if envelope.get("product") != "whole_market_daily_history@1":
            raise ValueError("legacy Product identity is invalid")
        backing = store.adopt_file(
            writer.finish(),
            media_type=QUERY_BACKING_MEDIA_TYPE,
        )
    except Exception:
        writer.abort()
        raise
    summary = {
        "status": payload_status,
        "row_count": counts["bar"],
        "absence_count": counts["absence"],
        "security_count": counts["security"],
        "session_count": counts["session"],
        "fields": ["code", "trade_date", "open", "high", "low", "close", "volume", "amount"],
        "query_operations": [
            "filter", "absences", "securities", "sessions", "aggregate",
            "group_by", "breadth", "rank", "window_compare",
        ],
        "snapshot_proof": snapshot_proof,
    }
    envelope["payload"] = {
        "status": payload_status,
        "__a_hunter_query_artifact__": backing.content_hash,
        "__a_hunter_query_format__": QUERY_BACKING_FORMAT,
        "summary": summary,
    }
    return envelope, backing


def _discard_query_backing(observation: ProviderObservation) -> None:
    if observation.query_backing is not None:
        _discard_query_backing_path(observation.query_backing.path)


def _discard_query_backing_path(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _whole_market_history_summary(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload.get("rows")
    securities = payload.get("securities")
    sessions = payload.get("sessions")
    return {
        "status": payload.get("status"),
        "row_count": payload["row_count"] if type(payload.get("row_count")) is int else len(rows) if isinstance(rows, list) else 0,
        "absence_count": payload["absence_count"] if type(payload.get("absence_count")) is int else len(payload.get("absences", [])) if isinstance(payload.get("absences"), list) else 0,
        "security_count": len(securities) if isinstance(securities, list) else 0,
        "session_count": len(sessions) if isinstance(sessions, list) else 0,
        "field_coverage": payload.get("field_coverage", {}),
        "fields": ["code", "trade_date", "open", "high", "low", "close", "volume", "amount"],
        "query_operations": [
            "filter", "absences", "securities", "sessions", "aggregate",
            "group_by", "breadth", "rank", "window_compare",
        ],
        "snapshot_proof": payload.get("snapshot_proof", {}),
    }


def _query_artifact_hash(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("__a_hunter_query_artifact__")
    if not isinstance(value, str) or len(value) != 64:
        return None
    if any(char not in "0123456789abcdef" for char in value):
        return None
    return value


def _validate_payload_dates(payload: Any, boundary: ResearchBoundary, product_ref: VersionRef | None) -> None:
    if not isinstance(payload, dict):
        return
    rows = payload.get("rows")
    if isinstance(rows, list) and any(isinstance(row, dict) and "trade_date" in row for row in rows):
        whole_market_history = product_ref is not None and product_ref.id == "whole_market_daily_history"
        previous_key: str | tuple[str, str] | None = None
        seen: set[str | tuple[str, str]] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ProductUnavailable("market bar row is not an object", retryable=False)
            trade_date = row.get("trade_date")
            if not isinstance(trade_date, str):
                raise ProductUnavailable("market bar trade_date is missing", retryable=False)
            try:
                parsed_date = datetime.fromisoformat(trade_date).date() if "T" in trade_date else datetime.strptime(trade_date, "%Y-%m-%d").date()
            except ValueError as error:
                raise ProductUnavailable("market bar trade_date is invalid", retryable=False) from error
            if parsed_date > a_share_date(boundary.as_of):
                raise ProductUnavailable("market bar is after as_of", retryable=False)
            if whole_market_history:
                code = row.get("code")
                if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
                    raise ProductUnavailable("whole-market bar code is invalid", retryable=False)
                key: str | tuple[str, str] = (trade_date, code)
            else:
                key = trade_date
            if key in seen or (previous_key is not None and key < previous_key):
                raise ProductUnavailable("market bars are not unique and ordered", retryable=False)
            seen.add(key)
            previous_key = key
            numbers = {key: row.get(key) for key in ("open", "high", "low", "close", "volume", "amount") if key in row}
            if any(not _finite_number(value) or float(value) < 0 for key, value in numbers.items() if key in {"volume", "amount"}):
                raise ProductUnavailable("market bar contains a non-finite value", retryable=False)
            if any(not _finite_number(value) for key, value in numbers.items() if key not in {"volume", "amount"}):
                raise ProductUnavailable("market bar contains a non-finite value", retryable=False)
            if {"open", "high", "low", "close"}.issubset(row):
                if float(row["high"]) < max(float(row["open"]), float(row["close"])) or float(row["low"]) > min(float(row["open"]), float(row["close"])):
                    raise ProductUnavailable("market bar OHLC logic is invalid", retryable=False)
    for collection_name in ("items", "rows"):
        collection = payload.get(collection_name)
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, dict):
                continue
            for field in (
                "published_at",
                "announcement_at",
                "announcement_date",
                "disclosed_at",
                "disclosure_at",
                "report_disclosed_at",
                "forecast_published_at",
                "plan_date",
                "event_time",
            ):
                if field not in item or item[field] in (None, ""):
                    continue
                parsed = _parse_timestamp(item[field])
                if parsed is None:
                    raise ProductUnavailable(f"{field} is invalid", retryable=False)
                if parsed > boundary.as_of:
                    if field == "plan_date" and product_ref is not None and product_ref.id == "lockup_calendar":
                        disclosed = _parse_timestamp(item.get("announcement_at"))
                        if disclosed is not None and disclosed <= boundary.as_of:
                            continue
                    raise ProductUnavailable(f"{field} is after as_of", retryable=False)


def _parse_stored_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
        try:
            return datetime.fromtimestamp(float(value), tz=boundary_timezone())
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.isdigit():
        try:
            return datetime.fromtimestamp(float(text), tz=boundary_timezone())
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=boundary_timezone())
        except ValueError:
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=boundary_timezone())
    return parsed


def boundary_timezone():
    from datetime import timezone

    return timezone.utc


def _finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _validate_schema(value: Any, schema: dict[str, Any], *, path: str) -> None:
    if not schema:
        return
    expected = schema.get("type")
    matches = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if expected in matches and not matches[expected]:
        raise ValueError(f"{path} must be {expected}")
    if isinstance(value, dict):
        missing = [key for key in schema.get("required", ()) if key not in value]
        if missing:
            raise ValueError(f"{path} is missing required fields: {', '.join(missing)}")
        for key, child in schema.get("properties", {}).items():
            if key in value and isinstance(child, dict):
                _validate_schema(value[key], child, path=f"{path}.{key}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _validate_schema(item, schema["items"], path=f"{path}[{index}]")


def _conflicts(payloads: list[Any], tolerance: float) -> bool:
    if len(payloads) < 2:
        return False
    first = payloads[0]
    for other in payloads[1:]:
        if _payload_conflict(first, other, tolerance):
            return True
    return False


def _payload_conflict(left: Any, right: Any, tolerance: float) -> bool:
    if isinstance(left, dict) and isinstance(right, dict):
        if set(left) != set(right):
            return True
        return any(_payload_conflict(left[key], right[key], tolerance) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return True
        return any(_payload_conflict(a, b, tolerance) for a, b in zip(left, right))
    if _finite_number(left) and _finite_number(right):
        denominator = max(abs(float(left)), abs(float(right)), 1.0)
        return abs(float(left) - float(right)) / denominator > tolerance
    return left != right
