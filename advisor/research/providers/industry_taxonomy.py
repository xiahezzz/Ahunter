"""Versioned Eastmoney first-level industry taxonomy snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any, Callable, Iterable, Protocol

import requests

from advisor.research.data_products.engine import ProductRequest, ProductUnavailable, ProviderObservation
from advisor.research.market_time import a_share_date
from advisor.research.providers.diagnostics import exception_type_chain
from advisor.research.providers.eastmoney_transport import EASTMONEY_CLIST_ENDPOINT, EastmoneyNodeSession
from advisor.research.providers.whole_market_intraday import ExpectedUniverse
from advisor.research.repository import ResearchRepository


class TaxonomyFetcher(Protocol):
    def __call__(self) -> Iterable[dict[str, Any]]: ...


class IndustryTaxonomyProvider:
    """Refresh once per trading day, otherwise reuse an immutable version."""

    provider_id = "eastmoney-industry-taxonomy"

    def __init__(
        self,
        repository: ResearchRepository,
        universe: ExpectedUniverse,
        fetcher: TaxonomyFetcher,
        *,
        max_age_trading_days: int = 5,
        minimum_coverage: float = 0.95,
        max_membership_churn: float = 0.80,
    ) -> None:
        if not 0 <= max_age_trading_days <= 30 or not 0 < minimum_coverage <= 1 or not 0 < max_membership_churn <= 1:
            raise ValueError("industry taxonomy provider configuration is invalid")
        self.repository = repository
        self.universe = universe
        self.fetcher = fetcher
        self.max_age_trading_days = max_age_trading_days
        self.minimum_coverage = minimum_coverage
        self.max_membership_churn = max_membership_churn

    def fetch(self, request: ProductRequest, *, dependencies: dict[str, Any] | None = None) -> ProviderObservation:
        if request.product.id != "industry_sector_taxonomy":
            raise ValueError(f"{self.provider_id} does not provide {request.product}")
        if not request.subject.is_market:
            raise ProductUnavailable("industry taxonomy requires a Market Subject", retryable=False)
        cached = self.repository.industry_taxonomy_for(request.boundary.as_of, max_age_trading_days=0)
        if cached is not None:
            return _cached_observation(cached, request.boundary.as_of, provider=self.provider_id, stale=False)
        try:
            expected = {item.code for item in self.universe.active(request.boundary.as_of)}
            fetched_at = datetime.now(timezone.utc)
            normalized = normalize_industry_taxonomy_rows(
                self.fetcher(),
                expected_codes=expected,
                as_of=request.boundary.as_of,
                fetched_at=fetched_at,
                minimum_coverage=self.minimum_coverage,
            )
            self._reject_abnormal_churn(normalized, request.boundary.as_of)
            with self.repository.transaction():
                self.repository.record_industry_taxonomy(
                    taxonomy_hash=normalized["taxonomy_hash"],
                    as_of_date=a_share_date(request.boundary.as_of),
                    observed_at=request.boundary.as_of,
                    fetched_at=fetched_at,
                    source="eastmoney-first-level-industry",
                    coverage=float(normalized["coverage"]),
                    payload=normalized,
                    members=tuple(
                        (item["industry_id"], item["industry_name"], code)
                        for item in normalized["industries"]
                        for code in item["members"]
                    ),
                )
            stored = self.repository.industry_taxonomy_for(request.boundary.as_of, max_age_trading_days=0)
            if stored is None:
                raise ProductUnavailable("industry taxonomy persistence failed", retryable=False)
            return _cached_observation(stored, request.boundary.as_of, provider=self.provider_id, stale=False)
        except Exception as error:
            fallback = self.repository.industry_taxonomy_for(
                request.boundary.as_of,
                max_age_trading_days=self.max_age_trading_days,
            )
            if fallback is None:
                failure = exception_type_chain(error)
                return ProviderObservation(
                    provider=self.provider_id,
                    payload={"status": "blocked", "industries": [], "reason_code": "taxonomy_stale"},
                    observed_at=request.boundary.as_of,
                    fetched_at=datetime.now(timezone.utc),
                    quality_status="blocked",
                    quality_message=(
                        "industry taxonomy is unavailable or older than five trading days; "
                        f"refresh failed: {failure}"
                    )[:320],
                    coverage=0.0,
                    schema_version="industry-sector-taxonomy@1",
                )
            return _cached_observation(
                fallback,
                request.boundary.as_of,
                provider=self.provider_id,
                stale=True,
                reason=f"taxonomy refresh failed: {exception_type_chain(error)}",
            )

    def _reject_abnormal_churn(self, payload: dict[str, Any], as_of: datetime) -> None:
        previous = self.repository.industry_taxonomy_for(as_of, max_age_trading_days=self.max_age_trading_days)
        if previous is None or previous["as_of_date"] == a_share_date(as_of).isoformat():
            return
        before = {code: industry_id for industry_id, _name, code in previous["members"]}
        after = {code: item["industry_id"] for item in payload["industries"] for code in item["members"]}
        common = set(before) & set(after)
        if not common:
            raise ProductUnavailable("industry taxonomy changed all known memberships", retryable=False)
        changed = sum(before[code] != after[code] for code in common) / len(common)
        if changed > self.max_membership_churn:
            raise ProductUnavailable("industry taxonomy membership churn is abnormal", retryable=False)


class EastmoneyIndustryTaxonomyProvider(IndustryTaxonomyProvider):
    """Repository-owned default adapter; tests pass a fixture fetcher instead."""

    def __init__(
        self,
        repository: ResearchRepository,
        universe: ExpectedUniverse,
        fetcher: TaxonomyFetcher | None = None,
        *,
        session: requests.Session | EastmoneyNodeSession | None = None,
        timeout: float = 10.0,
        **kwargs: Any,
    ) -> None:
        self._session = session or EastmoneyNodeSession()
        self._timeout = timeout
        super().__init__(repository, universe, fetcher or self._fetch_first_level, **kwargs)

    def _fetch_first_level(self) -> Iterable[dict[str, Any]]:
        # The real endpoint has two explicitly separate calls: the list of
        # Eastmoney first-level industry boards, then each board's components.
        # No quote endpoint label is accepted as membership truth.
        endpoint = EASTMONEY_CLIST_ENDPOINT
        headers = {"User-Agent": "A-Hunter/1.0"}
        board_params = {
            "pn": 1, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "fid": "f12", "fs": "m:90+s:4", "fields": "f12,f14",
        }
        first_board_response = self._session.get(
            endpoint, params=board_params, timeout=self._timeout, headers=headers
        )
        entries, board_total = _clist_page(first_board_response, maximum_total=500)
        board_pages = _proved_page_count(entries, board_total, page_size=100)
        if board_pages > 1:
            extra_board_params = tuple({**board_params, "pn": page} for page in range(2, board_pages + 1))
            for response in _session_responses(
                self._session, endpoint, extra_board_params, timeout=self._timeout, headers=headers
            ):
                page_rows, page_total = _clist_page(response, maximum_total=500)
                if page_total != board_total:
                    raise ProductUnavailable("industry taxonomy board total changed", retryable=False)
                entries.extend(page_rows)
        if board_total is not None and len(entries) != board_total:
            raise ProductUnavailable("industry taxonomy board pages are incomplete", retryable=False)
        valid_entries: list[tuple[str, str]] = []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            industry_id = str(entry.get("f12", ""))
            name = str(entry.get("f14", ""))
            if not industry_id or not name:
                continue
            valid_entries.append((industry_id, name))
        member_params = tuple(
            {"pn": 1, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f12", "fs": f"b:{industry_id}+f:!50", "fields": "f12"}
            for industry_id, _name in valid_entries
        )
        member_responses = _session_responses(
            self._session, endpoint, member_params, timeout=self._timeout, headers=headers
        )
        if len(member_responses) != len(valid_entries):
            raise ProductUnavailable("industry taxonomy membership response count is invalid", retryable=False)
        members_by_industry: list[list[dict[str, Any]]] = []
        member_totals: list[int | None] = []
        additional_members: list[tuple[int, dict[str, Any]]] = []
        for index, (params, response) in enumerate(zip(member_params, member_responses, strict=True)):
            rows, total = _clist_page(response, maximum_total=6_000)
            members_by_industry.append(rows)
            member_totals.append(total)
            pages = _proved_page_count(rows, total, page_size=100)
            additional_members.extend(
                (index, {**params, "pn": page}) for page in range(2, pages + 1)
            )
        if additional_members:
            extra_responses = _session_responses(
                self._session,
                endpoint,
                tuple(params for _index, params in additional_members),
                timeout=self._timeout,
                headers=headers,
            )
            for (index, _params), response in zip(additional_members, extra_responses, strict=True):
                page_rows, page_total = _clist_page(response, maximum_total=6_000)
                if page_total != member_totals[index]:
                    raise ProductUnavailable("industry taxonomy membership total changed", retryable=False)
                members_by_industry[index].extend(page_rows)
        result: list[dict[str, Any]] = []
        for index, (industry_id, name) in enumerate(valid_entries):
            member_rows = members_by_industry[index]
            total = member_totals[index]
            if total is not None and len(member_rows) != total:
                raise ProductUnavailable("industry taxonomy membership pages are incomplete", retryable=False)
            result.append({
                "industry_id": industry_id,
                "name": name,
                "level": "first",
                "members": [
                    str(item.get("f12"))
                    for item in member_rows
                    if isinstance(item, dict) and _is_a_share_code(item.get("f12"))
                ],
            })
        return result


def _session_responses(
    session: requests.Session | EastmoneyNodeSession,
    endpoint: str,
    params: tuple[dict[str, Any], ...],
    *,
    timeout: float,
    headers: dict[str, str],
) -> tuple[Any, ...]:
    if not params:
        return ()
    if hasattr(session, "get_many"):
        responses: list[Any] = []
        for start in range(0, len(params), 200):
            responses.extend(
                session.get_many(endpoint, params[start:start + 200], timeout=timeout, headers=headers)
            )
        return tuple(responses)
    return tuple(
        session.get(endpoint, params=item, timeout=timeout, headers=headers)
        for item in params
    )


def _clist_page(response: Any, *, maximum_total: int) -> tuple[list[dict[str, Any]], int | None]:
    response.raise_for_status()
    raw = response.json()
    data = raw.get("data") if isinstance(raw, dict) else None
    rows = data.get("diff") if isinstance(data, dict) else None
    total_raw = data.get("total") if isinstance(data, dict) else None
    if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
        raise ProductUnavailable("industry taxonomy page is invalid", retryable=False)
    if total_raw is None:
        total = None
    elif isinstance(total_raw, (int, float, str)) and str(total_raw).isdigit():
        total = int(total_raw)
        if not 0 <= total <= maximum_total:
            raise ProductUnavailable("industry taxonomy page total is invalid", retryable=False)
    else:
        raise ProductUnavailable("industry taxonomy page total is invalid", retryable=False)
    return list(rows), total


def _proved_page_count(rows: list[dict[str, Any]], total: int | None, *, page_size: int) -> int:
    if total is None:
        if len(rows) >= page_size:
            raise ProductUnavailable("industry taxonomy pagination total is unavailable", retryable=False)
        return 1
    pages = max(1, math.ceil(total / page_size))
    if len(rows) != min(page_size, total):
        raise ProductUnavailable("industry taxonomy first page is incomplete", retryable=False)
    return pages


def _is_a_share_code(value: object) -> bool:
    code = str(value or "")
    return len(code) == 6 and code.isdigit() and (
        code.startswith(("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688"))
    )


def normalize_industry_taxonomy_rows(
    rows: Iterable[dict[str, Any]],
    *,
    expected_codes: set[str],
    as_of: datetime,
    fetched_at: datetime,
    minimum_coverage: float = 0.95,
) -> dict[str, Any]:
    if as_of.tzinfo is None or as_of.utcoffset() is None or fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ProductUnavailable("industry taxonomy time is invalid", retryable=False)
    if not expected_codes:
        raise ProductUnavailable("industry taxonomy expected universe is empty", retryable=False)
    industries: list[dict[str, Any]] = []
    assigned: set[str] = set()
    out_of_scope: set[str] = set()
    identifiers: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ProductUnavailable("industry taxonomy row is invalid", retryable=False)
        level = str(raw.get("level", raw.get("classification_level", "first"))).strip().lower()
        category = str(raw.get("category", "industry")).strip().lower()
        if level not in {"first", "1", "一级"} or category not in {"industry", "行业", ""}:
            raise ProductUnavailable("industry taxonomy contains non-first-level membership", retryable=False)
        industry_id = str(raw.get("industry_id", raw.get("code", ""))).strip()
        name = str(raw.get("industry_name", raw.get("name", ""))).strip()
        members_value = raw.get("members", raw.get("codes", ()))
        if not industry_id or not name or len(industry_id) > 80 or len(name) > 120 or industry_id in identifiers:
            raise ProductUnavailable("industry taxonomy industry is invalid", retryable=False)
        if not isinstance(members_value, (list, tuple)) or not members_value:
            raise ProductUnavailable("industry taxonomy industry is empty", retryable=False)
        raw_members = {str(item).zfill(6) for item in members_value}
        if any(not _is_a_share_code(code) for code in raw_members):
            raise ProductUnavailable("industry taxonomy contains an unknown security", retryable=False)
        identifiers.add(industry_id)
        out_of_scope.update(raw_members - expected_codes)
        members = tuple(sorted(raw_members & expected_codes))
        if not members:
            continue
        if assigned & set(members):
            raise ProductUnavailable("industry taxonomy contains a duplicate member", retryable=False)
        assigned.update(members)
        industries.append({"industry_id": industry_id, "industry_name": name, "members": list(members)})
    total_source_members = len(assigned | out_of_scope)
    if total_source_members and len(out_of_scope) / total_source_members > 1 - minimum_coverage:
        raise ProductUnavailable("industry taxonomy contains too many out-of-scope securities", retryable=False)
    if not industries:
        raise ProductUnavailable("industry taxonomy is empty", retryable=False)
    industries.sort(key=lambda item: item["industry_id"])
    coverage = len(assigned) / len(expected_codes)
    if coverage < minimum_coverage:
        raise ProductUnavailable("industry taxonomy universe coverage is insufficient", retryable=False)
    material = {
        "as_of_date": a_share_date(as_of).isoformat(),
        "source": "eastmoney-first-level-industry",
        "industries": industries,
        "expected_universe_count": len(expected_codes),
        "assigned_count": len(assigned),
        "out_of_scope_member_count": len(out_of_scope),
        "coverage": coverage,
    }
    material["taxonomy_hash"] = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return material


def _cached_observation(
    stored: dict[str, object],
    boundary: datetime,
    *,
    provider: str,
    stale: bool,
    reason: str | None = None,
) -> ProviderObservation:
    payload = dict(stored["payload"])  # immutable persisted JSON; make a new public envelope projection
    payload["taxonomy_hash"] = stored["taxonomy_hash"]
    payload["age_trading_days"] = stored["age_trading_days"]
    payload["status"] = "warning" if stale else "passed"
    payload["stale_fallback"] = stale
    observed_at = datetime.fromisoformat(str(stored["observed_at"]))
    fetched_at = datetime.fromisoformat(str(stored["fetched_at"]))
    return ProviderObservation(
        provider=provider,
        payload=payload,
        observed_at=min(observed_at, boundary),
        fetched_at=fetched_at,
        source_locator="eastmoney://industry-taxonomy",
        quality_status="warning" if stale else "passed",
        quality_message=reason if stale else None,
        coverage=float(stored["coverage"]),
        schema_version="industry-sector-taxonomy@1",
    )


__all__ = [
    "EastmoneyIndustryTaxonomyProvider",
    "IndustryTaxonomyProvider",
    "normalize_industry_taxonomy_rows",
]
