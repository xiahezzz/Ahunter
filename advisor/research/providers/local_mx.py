"""Scoped, read-only MX Accepted Event feeds for Research snapshots."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Any

from advisor.mx.information import (
    MxInformationError,
    MxInformationStore,
)
from advisor.research.contracts import canonical_json
from advisor.research.data_products.engine import ProductRequest, ProviderObservation


_TOKEN_SECRET = hashlib.sha256(b"a-hunter-local-mx-provider-v2").digest()


class LocalMxProvider:
    """Materialize only the exact RID union requested for ``mx_events@2``.

    The provider intentionally delegates all Accepted Event reads to
    ``MxInformationStore``.  It neither opens media nor exposes database
    paths, raw event payloads, source URLs, or the current full RID set.
    """

    provider_id = "local-mx"

    def __init__(
        self,
        events_database: Path | str,
        allowed_rids_path: Path | str,
        *,
        repository_root: Path | str | None = None,
    ) -> None:
        self.events_database = Path(events_database).expanduser().resolve()
        self.allowed_rids_path = Path(allowed_rids_path).expanduser().resolve()
        self.repository_root = Path(repository_root or self.events_database.parent).expanduser().resolve()

    def fetch(
        self,
        request: ProductRequest,
        *,
        dependencies: dict[str, Any] | None = None,
    ) -> ProviderObservation:
        if request.product.id != "mx_events" or request.product.version != 2:
            raise ValueError(f"local-mx does not provide {request.product}")
        if not request.feed_rids:
            raise ValueError("mx_events@2 requires exact RID feeds")
        boundary = request.boundary.as_of
        start = boundary - timedelta(days=30)
        window = {
            "start_exclusive": start.isoformat(),
            "end_inclusive": boundary.isoformat(),
        }
        store = MxInformationStore(
            self.events_database,
            self.allowed_rids_path,
            repository_root=self.repository_root,
            token_secret=_TOKEN_SECRET,
            clock=lambda: boundary,
        )
        feeds: list[dict[str, object]] = []
        for rid in request.feed_rids:
            try:
                result = store.read_accepted_rid_feed(
                    rid=rid,
                    start_at=int(start.timestamp() * 1000),
                    end_at=int(boundary.timestamp() * 1000),
                    limit=5_000,
                )
                quality = result.get("quality")
                items = result.get("items")
                if (
                    not isinstance(quality, dict)
                    or not isinstance(quality.get("status"), str)
                    or not isinstance(quality.get("reason"), str)
                    or not isinstance(items, list)
                ):
                    raise ValueError("invalid accepted event feed")
            except (MxInformationError, OSError, ValueError):
                quality = {"status": "unavailable", "reason": "local MX information is unavailable"}
                items = []
            material = {"rid": rid, "window": window, "items": items}
            feeds.append(
                {
                    "rid": rid,
                    "window": window,
                    "event_count": len(items),
                    "quality": quality,
                    "content_hash": hashlib.sha256(canonical_json(material)).hexdigest(),
                    "items": items,
                }
            )
        return ProviderObservation(
            provider=self.provider_id,
            payload={"status": "passed", "feeds": feeds},
            observed_at=boundary,
            source_locator="local-mx:accepted-events",
            quality_status="passed",
            coverage=1.0,
            fetched_at=boundary,
            schema_version="mx-events-rid-feed@2",
        )
