from __future__ import annotations

from datetime import date, datetime, timezone
import math
import re
import threading
import time
from typing import Any

import requests

from advisor.research.data_products.engine import (
    ProductRequest,
    ProviderObservation,
    ProviderRegistry,
)
from advisor.research.providers.fundamentals import (
    normalize_earnings_forecast,
    normalize_financial_statements,
    normalize_industry_context,
    normalize_insider_activity,
)
from advisor.research.providers.information import (
    MarketInformationProvider,
    PublicMarketInformationSources,
    normalize_information_rows,
)
from advisor.research.providers.industry_taxonomy import EastmoneyIndustryTaxonomyProvider
from advisor.research.providers.local import LocalMarketProvider
from advisor.research.providers.local_mx import LocalMxProvider
from advisor.research.providers.market import derive_indicators, normalize_quote
from advisor.research.providers.normalization import parse_public_time, rows_from_payload
from advisor.research.providers.whole_market_intraday import (
    HybridTradingSessionAuthority,
    SinaBenchmarkCurrentSessionAuthority,
    SinaWholeMarketIntradayProvider,
    SqliteExpectedUniverse,
    SqliteTradingSessionAuthority,
)


_CODE_RE = re.compile(r"^[0-9]{6}$")
_PREFIX = {"0": "sz", "3": "sz", "4": "bj", "6": "sh", "8": "bj"}


def _code(request: ProductRequest) -> str:
    code = request.subject.code
    if not _CODE_RE.fullmatch(code):
        raise ValueError("unsupported subject code")
    return code


def _warning(request: ProductRequest, product: str, message: str, *, locator: str | None = None) -> ProviderObservation:
    fetched_at = datetime.now(timezone.utc)
    return ProviderObservation(
        provider="public-a-share",
        payload={"status": "unavailable", "product": product, "items": [], "message": message},
        observed_at=request.boundary.as_of,
        source_locator=locator,
        quality_status="warning",
        quality_message=message,
        coverage=0.0,
        fetched_at=fetched_at,
        schema_version=f"{product}@1",
    )


class PublicAStockProvider:
    provider_id = "public-a-share"

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = 12.0,
        mx_snapshot: Any | None = None,
        request_interval: float = 0.2,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.mx_snapshot = mx_snapshot
        self.request_interval = max(0.0, float(request_interval))
        self._request_lock = threading.Lock()
        self._last_request_at = 0.0

    def preflight(self) -> str:
        """Check that the public quote source can be reached before a live run."""
        endpoint = "https://qt.gtimg.cn/q=sh600519"
        response = self._get(
            endpoint,
            timeout=min(self.timeout, 5.0),
            headers={"User-Agent": "A-Hunter/1.0"},
        )
        response.raise_for_status()
        if not response.content:
            raise RuntimeError("public quote source returned an empty response")
        return endpoint

    def _get(self, endpoint: str, **kwargs: Any):
        """Use one shared limiter for every public endpoint in this adapter."""
        self._wait_for_slot()
        return self.session.get(endpoint, **kwargs)

    def _wait_for_slot(self) -> None:
        with self._request_lock:
            elapsed = time.monotonic() - self._last_request_at
            if self.request_interval > elapsed:
                time.sleep(self.request_interval - elapsed)
            self._last_request_at = time.monotonic()

    def fetch(self, request: ProductRequest, *, dependencies: dict[str, Any] | None = None) -> ProviderObservation:
        product = request.product.id
        if product == "market_quote":
            return self._quote(request)
        if product == "market_indicators":
            return self._indicators(request, dependencies or {})
        if product == "company_identity":
            return self._identity(request)
        if product == "fundamental_snapshot":
            return self._fundamental_snapshot(request)
        if product in {"financial_statements", "earnings_forecast", "industry_context", "insider_activity"}:
            return self._public_html_or_warning(request, product)
        if product in {"company_news", "macro_news"}:
            return self._news(request, product)
        if product == "mx_events":
            return self._mx_events(request)
        if product in {
            "hot_stocks",
            "capital_flows",
            "concepts",
            "dragon_tiger",
            "lockup_calendar",
        }:
            return _warning(request, product, "no approved public provider is configured")
        raise ValueError(f"unsupported public product: {request.product}")

    def _quote(self, request: ProductRequest) -> ProviderObservation:
        code = _code(request)
        symbol = f"{_PREFIX[code[0]]}{code}"
        endpoint = "https://qt.gtimg.cn/q=" + symbol
        fetched_at = datetime.now(timezone.utc)
        try:
            response = self._get(endpoint, timeout=self.timeout, headers={"User-Agent": "A-Hunter/1.0"})
            response.raise_for_status()
            text = response.content.decode("gbk", errors="replace")
            values = text.split('="', 1)[-1].rsplit('"', 1)[0].split("~")
            if len(values) < 49:
                raise ValueError("quote row is incomplete")
            quote_time = parse_public_time(
                " ".join(item for item in (values[30], values[31]) if item)
            ) if len(values) > 31 else None
            if quote_time is not None and quote_time > request.boundary.as_of:
                return _warning(request, "market_quote", "quote is after as_of", locator=endpoint)
            payload = normalize_quote(
                {
                    "name": values[1],
                    "price": values[3],
                    "last_close": values[4],
                    "open": values[5],
                    "change_pct": values[32],
                    "high": values[33],
                    "low": values[34],
                    "turnover_pct": values[38],
                    "pe_ttm": values[39],
                    "market_cap_yi": values[44],
                    "float_market_cap_yi": values[45],
                    "pb": values[46],
                    "limit_up": values[47],
                    "limit_down": values[48],
                },
                code=code,
                source="tencent-quote",
                fetched_at=fetched_at,
                source_time=quote_time,
            )
            price = payload.get("price")
            last_close = payload.get("last_close")
            if price is None and last_close is None:
                raise ValueError("quote has no usable price")
            high = payload.get("high")
            low = payload.get("low")
            if high is not None and low is not None and high < low:
                raise ValueError("quote high is below low")
            if price is not None and high is not None and price > high:
                raise ValueError("quote price is above high")
            if price is not None and low is not None and price < low:
                raise ValueError("quote price is below low")
        except (requests.RequestException, UnicodeError, ValueError, IndexError) as error:
            return _warning(request, "market_quote", type(error).__name__, locator=endpoint)
        return ProviderObservation(
            provider=self.provider_id,
            payload=payload,
            observed_at=quote_time or request.boundary.as_of,
            source_locator=endpoint,
            fetched_at=fetched_at,
            schema_version="market-products@1",
        )

    def _identity(self, request: ProductRequest) -> ProviderObservation:
        quote = self._quote(request)
        payload = quote.payload if isinstance(quote.payload, dict) else {"code": request.subject.code}
        return ProviderObservation(
            provider=quote.provider,
            payload={"status": quote.quality_status, "code": request.subject.code, "name": payload.get("name")},
            observed_at=quote.observed_at,
            source_locator=quote.source_locator,
            quality_status=quote.quality_status,
            quality_message=quote.quality_message,
            coverage=quote.coverage,
            fetched_at=quote.fetched_at,
            schema_version="market-products@1",
        )

    def _indicators(self, request: ProductRequest, dependencies: dict[str, Any]) -> ProviderObservation:
        bars_product = dependencies.get("market_daily_bars@1", {})
        rows = bars_product.get("rows", []) if isinstance(bars_product, dict) else []
        rows = [row for row in rows if isinstance(row, dict)]
        if not rows:
            return _warning(request, "market_indicators", "daily bars unavailable")
        return ProviderObservation(
            provider=self.provider_id,
            payload=derive_indicators(rows),
            observed_at=request.boundary.as_of,
            schema_version="market-products@1",
        )

    def _fundamental_snapshot(self, request: ProductRequest) -> ProviderObservation:
        quote = self._quote(request)
        if not isinstance(quote.payload, dict):
            return _warning(request, "fundamental_snapshot", "quote unavailable")
        payload = {
            "status": quote.quality_status,
            "code": request.subject.code,
            "valuation": {
                key: quote.payload.get(key)
                for key in ("price", "pe_ttm", "pb", "market_cap_yi", "float_market_cap_yi")
            },
            "market": {
                key: quote.payload.get(key)
                for key in ("turnover_pct", "change_pct", "high", "low")
            },
        }
        return ProviderObservation(
            provider=quote.provider,
            payload=payload,
            observed_at=request.boundary.as_of,
            source_locator=quote.source_locator,
            quality_status=quote.quality_status,
            quality_message=quote.quality_message,
            coverage=quote.coverage,
            fetched_at=quote.fetched_at,
            schema_version="fundamental-products@1",
        )

    def _news(self, request: ProductRequest, product: str) -> ProviderObservation:
        code = _code(request)
        endpoint = "https://feed.mix.sina.com.cn/api/roll/get"
        params = {"pageid": "153", "lid": "2510", "k": code, "num": "30", "page": "1"}
        fetched_at = datetime.now(timezone.utc)
        try:
            response = self._get(endpoint, params=params, timeout=self.timeout, headers={"User-Agent": "A-Hunter/1.0"})
            response.raise_for_status()
            payload = response.json()
            data = payload.get("result", {}).get("data", []) if isinstance(payload, dict) else []
            normalized = normalize_information_rows(
                data,
                code=code,
                product=product,
                as_of=request.boundary.as_of,
                source_locator=endpoint,
                fetched_at=fetched_at,
            )
            return ProviderObservation(
                provider=self.provider_id,
                payload=normalized.payload,
                observed_at=request.boundary.as_of,
                source_locator=endpoint,
                quality_status=normalized.quality_status,
                quality_message=normalized.quality_message,
                fetched_at=fetched_at,
                schema_version="information-products@1",
            )
        except (requests.RequestException, ValueError, TypeError, AttributeError) as error:
            return _warning(request, product, type(error).__name__, locator=endpoint)

    def _public_html_or_warning(self, request: ProductRequest, product: str) -> ProviderObservation:
        code = _code(request)
        endpoint = f"https://money.finance.sina.com.cn/corp/go.php/vFD_FinanceSummary/stockid/{code}.phtml"
        fetched_at = datetime.now(timezone.utc)
        try:
            response = self._get(endpoint, timeout=self.timeout, headers={"User-Agent": "A-Hunter/1.0"})
            response.raise_for_status()
            text = response.text
            if len(text) < 100:
                raise ValueError("public page is empty")
            if "<tr" not in text.lower():
                raise ValueError("public page has no structured table")
            normalizers = {
                "financial_statements": normalize_financial_statements,
                "earnings_forecast": normalize_earnings_forecast,
                "industry_context": normalize_industry_context,
                "insider_activity": normalize_insider_activity,
            }
            normalized = normalizers[product](
                text,
                code=code,
                as_of=request.boundary.as_of,
                source_locator=endpoint,
                fetched_at=fetched_at,
            )
            return ProviderObservation(
                provider=self.provider_id,
                payload=normalized.payload,
                observed_at=request.boundary.as_of,
                source_locator=endpoint,
                quality_status=normalized.quality_status,
                quality_message=normalized.quality_message,
                fetched_at=fetched_at,
                schema_version="fundamental-products@1",
            )
        except (requests.RequestException, ValueError, TypeError, KeyError) as error:
            return _warning(request, product, type(error).__name__, locator=endpoint)

    def _mx_events(self, request: ProductRequest) -> ProviderObservation:
        snapshot = self.mx_snapshot
        if snapshot is None:
            return _warning(request, "mx_events", "no quality-checked local MX snapshot was supplied")
        quality = getattr(snapshot, "quality", None)
        if getattr(quality, "blocking_failure", False):
            return ProviderObservation(
                provider=self.provider_id,
                payload={"status": "blocked", "items": []},
                observed_at=request.boundary.as_of,
                quality_status="blocked",
                quality_message="local MX snapshot quality is blocking",
                coverage=0.0,
                fetched_at=datetime.now(timezone.utc),
                schema_version="information-products@1",
            )
        items = []
        for event in getattr(snapshot, "events", ()):
            summary = str(getattr(event, "summary", ""))[:800]
            if request.subject.code not in summary and not summary:
                continue
            received_at = getattr(event, "received_at", None)
            if received_at is None or received_at > request.boundary.as_of:
                continue
            items.append(
                {
                    "evidence_id": str(getattr(event, "evidence_id", "")),
                    "rid": getattr(event, "rid", None),
                    "summary": summary,
                    "received_at": received_at.isoformat(),
                    "source_type": str(getattr(event, "source_type", "mx")),
                    "source_id": str(getattr(event, "source_id", "")),
                }
            )
        items.sort(key=lambda item: (item["received_at"], item["evidence_id"]))
        snapshot_as_of = getattr(snapshot, "as_of", request.boundary.as_of)
        observed_at = snapshot_as_of if snapshot_as_of <= request.boundary.as_of else request.boundary.as_of
        return ProviderObservation(
            provider=self.provider_id,
            payload={"status": "empty" if not items else "passed", "items": items},
            observed_at=observed_at,
            quality_status="passed",
            coverage=1.0,
            fetched_at=datetime.now(timezone.utc),
            schema_version="information-products@1",
        )

def _number(value: object) -> float | None:
    if value in (None, "", "--", "-", "None"):
        return None
    try:
        parsed = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _finite(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def build_default_provider_registry(
    *,
    mx_snapshot: Any | None = None,
    database_path: Any | None = None,
    mx_events_database: Any | None = None,
    allowed_rids_path: Any | None = None,
    repository_root: Any | None = None,
    research_repository: Any | None = None,
    query_staging_dir: Any | None = None,
) -> ProviderRegistry:
    provider = PublicAStockProvider(mx_snapshot=mx_snapshot)
    registry = ProviderRegistry()
    if database_path is not None:
        registry.register("market_daily_bars@1", LocalMarketProvider(database_path))
        registry.register(
            "whole_market_daily_history@1",
            LocalMarketProvider(database_path, staging_dir=query_staging_dir),
        )
        universe = SqliteExpectedUniverse(database_path)
        session_authority = HybridTradingSessionAuthority(
            SqliteTradingSessionAuthority(database_path),
            SinaBenchmarkCurrentSessionAuthority(),
        )
        registry.register(
            "whole_market_intraday_snapshot@1",
            SinaWholeMarketIntradayProvider(universe, session_authority=session_authority),
        )
        if research_repository is not None:
            registry.register(
                "industry_sector_taxonomy@1",
                EastmoneyIndustryTaxonomyProvider(research_repository, universe),
            )
    market_information_sources = PublicMarketInformationSources()
    registry.register(
        "market_information@1",
        MarketInformationProvider(
            official_fetcher=market_information_sources.official,
            media_fetcher=market_information_sources.media,
        ),
    )
    if mx_events_database is not None and allowed_rids_path is not None:
        registry.register(
            "mx_events@2",
            LocalMxProvider(
                mx_events_database,
                allowed_rids_path,
                repository_root=repository_root,
            ),
        )
    product_ids = (
        "company_identity",
        "market_quote",
        "market_indicators",
        "fundamental_snapshot",
        "financial_statements",
        "earnings_forecast",
        "industry_context",
        "insider_activity",
        "company_news",
        "macro_news",
        "mx_events",
        "hot_stocks",
        "capital_flows",
        "concepts",
        "dragon_tiger",
        "lockup_calendar",
    )
    for product_id in product_ids:
        registry.register(f"{product_id}@1", provider)
    return registry
