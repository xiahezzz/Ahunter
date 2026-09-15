"""Bounded, deduplicated information-flow normalizers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
import hashlib
import json
import re
import threading
import time
from typing import Any, Callable, Iterable
from urllib.parse import urljoin, urlparse

import requests

from advisor.research.providers.diagnostics import exception_type_chain
from advisor.research.data_products.engine import ProductRequest, ProviderObservation

from advisor.research.providers.normalization import (
    NormalizationResult,
    bounded_text,
    first_value,
    parse_public_time,
    rows_from_payload,
    stable_key,
)


SCHEMA_VERSION = "information-products@1"
MAX_ITEMS = 30


def normalize_information_rows(
    source: list[dict[str, Any]] | dict[str, Any],
    *,
    code: str,
    product: str,
    as_of: datetime,
    source_locator: str,
    fetched_at: datetime,
) -> NormalizationResult:
    rows = rows_from_payload(source)
    if not rows:
        return NormalizationResult(
            {"status": "empty", "product": product, "code": code, "items": [], "schema_version": SCHEMA_VERSION, "fetched_at": fetched_at.isoformat()}
        )
    normalized: list[dict[str, Any]] = []
    invalid = 0
    for row in rows:
        published = parse_public_time(first_value(row, "published_at", "ctime", "time", "发布时间", "日期"))
        if published is None or published > as_of:
            invalid += 1
            continue
        title = bounded_text(first_value(row, "title", "name", "标题") or "", 500)
        summary = bounded_text(first_value(row, "summary", "intro", "摘要") or "", 1500)
        if not title and not summary:
            invalid += 1
            continue
        url = bounded_text(first_value(row, "url", "link", "链接") or "", 500)
        publisher = bounded_text(first_value(row, "source", "publisher", "来源") or product, 120)
        security_codes = _codes(first_value(row, "security_codes", "codes", "证券代码") or code)
        normalized.append(
            {
                "item_id": _item_id(title, summary, published.isoformat()),
                "title": title,
                "summary": summary,
                "url": url,
                "published_at": published.isoformat(),
                "security_codes": security_codes,
                "category": bounded_text(first_value(row, "category", "分类") or product, 80),
                "source": publisher,
                "source_locators": sorted({source_locator, url} - {""}),
            }
        )
    normalized.sort(key=lambda item: (item["published_at"], item["item_id"]))
    deduplicated: list[dict[str, Any]] = []
    by_key: dict[str, int] = {}
    for item in normalized:
        key = stable_key(item["title"], item["summary"])
        if key in by_key:
            existing = deduplicated[by_key[key]]
            existing["source_locators"] = sorted(set(existing["source_locators"]) | set(item["source_locators"]))
            existing["source_variants"] = sorted(set(existing.get("source_variants", ())) | {item["source"]})
            existing["source"] = min(existing["source"], item["source"])
            existing["url"] = min(existing["url"], item["url"])
            existing["security_codes"] = sorted(
                set(existing.get("security_codes", ())) | set(item.get("security_codes", ()))
            )
            continue
        item["source_variants"] = [item["source"]]
        by_key[key] = len(deduplicated)
        deduplicated.append(item)
    deduplicated = deduplicated[-MAX_ITEMS:]
    deduplicated.sort(key=lambda item: (item["published_at"], item["item_id"]))
    return NormalizationResult(
        {
            "status": "passed" if deduplicated else ("warning" if invalid else "empty"),
            "product": product,
            "code": code,
            "items": deduplicated,
            "invalid_rows": invalid,
            "schema_version": SCHEMA_VERSION,
            "fetched_at": fetched_at.isoformat(),
        },
        "passed" if deduplicated or not invalid else "warning",
        None if not invalid else f"{invalid} information rows lacked a valid visible publication time or content",
    )


def _codes(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        values = value
    else:
        values = [value]
    result: list[str] = []
    for item in values:
        text = str(item)
        if len(text) == 6 and text.isdigit() and text not in result:
            result.append(text)
    return result


def _item_id(title: str, summary: str, published_at: str) -> str:
    value = json.dumps([title, summary, published_at], ensure_ascii=False, separators=(",", ":"))
    return "info-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


MARKET_INFORMATION_SCHEMA_VERSION = "market-information@1"


@dataclass(frozen=True)
class MarketInformationSource:
    """A bounded, public listing source for code-free Market Information."""

    endpoint: str
    publisher: str
    category: str


_LISTING_BLOCK = re.compile(r"<(?:li|article|tr)\b[^>]*>(?P<body>.*?)</(?:li|article|tr)>", re.IGNORECASE | re.DOTALL)
_LISTING_LINK = re.compile(
    r"<a\b[^>]*\bhref\s*=\s*[\"'](?P<href>[^\"']+)[\"'][^>]*>(?P<title>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_TAG = re.compile(r"<[^>]*>")
_PUBLIC_TIMESTAMP = re.compile(
    r"(?<!\d)(?P<year>20\d{2})\s*(?:[-/.]|年)\s*(?P<month>0?[1-9]|1[0-2])\s*(?:[-/.]|月)\s*(?P<day>0?[1-9]|[12]\d|3[01])(?:日)?"
    r"(?:\s*(?:T\s*)?(?P<hour>[01]?\d|2[0-3])\s*[:：]\s*(?P<minute>[0-5]\d)(?::\s*(?P<second>[0-5]\d))?\s*(?P<timezone>Z|UTC|GMT(?:[+-]0?8)?|CST|北京时间|中国标准时间)?)?",
)
_PAGE_GENERATED = re.compile(
    r"页面生成时间\s*(?P<year>20\d{2})[-/.年](?P<month>0?[1-9]|1[0-2])[-/.月]"
    r"(?P<day>0?[1-9]|[12]\d|3[01])"
)
_MONTH_DAY = re.compile(r"(?<!\d)(?P<month>0?[1-9]|1[0-2])[-/.](?P<day>0?[1-9]|[12]\d|3[01])(?!\d)")
_VISIBLE_CLOCK = re.compile(
    r"(?<!\d)(?P<hour>[01]?\d|2[0-3])\s*[:：]\s*(?P<minute>[0-5]\d)(?::\s*(?P<second>[0-5]\d))?(?!\d)"
)
_URL_DATE = re.compile(
    r"(?:/|_)(?P<year>20\d{2})(?:/|-)?(?P<month>0[1-9]|1[0-2])(?:/|-)?(?P<day>0[1-9]|[12]\d|3[01])(?:/|_|\.)"
)


class PublicMarketInformationSources:
    """Official-first and public-media Market Information listing adapters.

    These adapters intentionally retain only a date, headline, publisher and
    same-origin source URL.  They do not scrape an article body or accept a
    security code, and a failed request is bounded by both attempts and timeout.
    """

    DEFAULT_OFFICIAL_SOURCES = (
        MarketInformationSource(
            endpoint="https://www.csrc.gov.cn/csrc/xwfb/index.shtml?channelid=d3f6aca5967843fab5ece6b57b7e81e6",
            publisher="中国证监会",
            category="capital_market_regulation",
        ),
        MarketInformationSource(
            endpoint="https://www.gov.cn/zhengce/zuixin.htm",
            publisher="中国政府网",
            category="macro_policy",
        ),
    )
    DEFAULT_MEDIA_SOURCES = (
        MarketInformationSource(
            endpoint="https://www.cs.com.cn/",
            publisher="中国证券报",
            category="market_media",
        ),
    )

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = 10.0,
        max_attempts: int = 2,
        minimum_interval_seconds: float = 0.2,
        official_sources: Iterable[MarketInformationSource] | None = None,
        media_sources: Iterable[MarketInformationSource] | None = None,
    ) -> None:
        if timeout <= 0 or not 1 <= max_attempts <= 3 or minimum_interval_seconds < 0:
            raise ValueError("market information HTTP configuration is invalid")
        self._session = session or requests.Session()
        self._timeout = float(timeout)
        self._max_attempts = max_attempts
        self._minimum_interval_seconds = float(minimum_interval_seconds)
        self._official_sources = tuple(official_sources or self.DEFAULT_OFFICIAL_SOURCES)
        self._media_sources = tuple(media_sources or self.DEFAULT_MEDIA_SOURCES)
        self._request_lock = threading.Lock()
        self._last_request_at = 0.0

    def official(self) -> list[dict[str, str]]:
        return self._fetch_sources(self._official_sources)

    def media(self) -> list[dict[str, str]]:
        return self._fetch_sources(self._media_sources)

    def _fetch_sources(self, sources: tuple[MarketInformationSource, ...]) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        errors: list[Exception] = []
        for source in sources:
            try:
                rows.extend(_listing_rows(self._get_text(source.endpoint), source))
            except (requests.RequestException, ValueError) as error:
                errors.append(error)
        if not rows and errors:
            raise RuntimeError(f"all {len(sources)} market information sources failed") from errors[-1]
        return rows[:MAX_ITEMS]

    def _get_text(self, endpoint: str) -> str:
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            with self._request_lock:
                elapsed = time.monotonic() - self._last_request_at
                if elapsed < self._minimum_interval_seconds:
                    time.sleep(self._minimum_interval_seconds - elapsed)
                self._last_request_at = time.monotonic()
            try:
                response = self._session.get(
                    endpoint,
                    timeout=self._timeout,
                    headers={"User-Agent": "A-Hunter/1.0"},
                )
                response.raise_for_status()
                return _response_text(response)
            except requests.RequestException as error:
                last_error = error
                if attempt + 1 < self._max_attempts:
                    time.sleep(min(0.5, 0.1 * (2 ** attempt)))
        assert last_error is not None
        raise last_error


def _listing_rows(markup: str, source: MarketInformationSource) -> list[dict[str, str]]:
    """Extract only visible rows with a source publication time.

    Month/day-only official listings are assigned the conservative end of that
    source day.  Boundary filtering can therefore admit prior days while a
    same-day row remains invisible until the day is complete.  A visible clock
    may instead be combined with the publication date carried by its URL.
    """
    source_url = urlparse(source.endpoint)
    # CSRC exposes this proof in a meta ``content`` attribute, so inspect the
    # bounded markup before stripping tags.
    page_generation = _PAGE_GENERATED.search(unescape(markup))
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for block in _LISTING_BLOCK.finditer(markup):
        body = block.group("body")
        plain_body = _plain_text(body)
        date_match = _PUBLIC_TIMESTAMP.search(plain_body)
        link = _LISTING_LINK.search(body)
        if link is None:
            continue
        href = unescape(link.group("href")).strip()
        locator = _same_origin_locator(source_url, source.endpoint, href)
        title = bounded_text(_plain_text(link.group("title")), 500)
        if locator is None or not title:
            continue
        published_at: str | None = None
        if date_match is not None and date_match.group("hour") is not None:
            published_at = _publication_timestamp(
                date_match.group("year"),
                date_match.group("month"),
                date_match.group("day"),
                date_match.group("hour"),
                date_match.group("minute"),
                date_match.group("second"),
                _timezone_suffix(date_match.group("timezone")),
            )
        elif date_match is None:
            url_date = _URL_DATE.search(href)
            clock = _VISIBLE_CLOCK.search(plain_body)
            month_day = _MONTH_DAY.search(plain_body)
            if url_date is not None and clock is not None:
                published_at = _publication_timestamp(
                    url_date.group("year"), url_date.group("month"), url_date.group("day"),
                    clock.group("hour"), clock.group("minute"), clock.group("second"), "+08:00",
                )
            elif month_day is not None and page_generation is not None:
                published_at = _publication_timestamp(
                    page_generation.group("year"), month_day.group("month"), month_day.group("day"),
                    "23", "59", "59", "+08:00",
                )
        if published_at is None:
            continue
        key = (title, locator)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "title": title,
                "summary": bounded_text(_plain_text(body), 1500),
                "published_at": published_at,
                "original_url": locator,
                "publisher": source.publisher,
                "category": source.category,
            }
        )
    return rows


def _publication_timestamp(
    year: str,
    month: str,
    day: str,
    hour: str,
    minute: str,
    second: str | None,
    suffix: str,
) -> str:
    return (
        f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        f"T{int(hour):02d}:{int(minute):02d}:{int(second or 0):02d}{suffix}"
    )


def _response_text(response: Any) -> str:
    content = getattr(response, "content", None)
    if not isinstance(content, bytes):
        text = getattr(response, "text", None)
        if not isinstance(text, str):
            raise ValueError("market information response text is unavailable")
        return text
    header_value = ""
    headers = getattr(response, "headers", None)
    if isinstance(headers, dict):
        header_value = str(headers.get("Content-Type", headers.get("content-type", "")))
    candidates: list[str] = []
    header_charset = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9._-]+)", header_value, re.IGNORECASE)
    meta_charset = re.search(br"charset\s*=\s*[\"']?([A-Za-z0-9._-]+)", content[:4096], re.IGNORECASE)
    if header_charset is not None:
        candidates.append(header_charset.group(1))
    if meta_charset is not None:
        candidates.append(meta_charset.group(1).decode("ascii"))
    candidates.extend(("utf-8", "gb18030"))
    for encoding in dict.fromkeys(candidates):
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


def _same_origin_locator(source_url: Any, endpoint: str, href: str) -> str | None:
    locator = urlparse(urljoin(endpoint, href))
    if locator.scheme != "https" or locator.netloc.lower() != source_url.netloc.lower():
        return None
    return locator.geturl()


def _plain_text(markup: str) -> str:
    return " ".join(unescape(_HTML_TAG.sub(" ", markup)).split())


def _timezone_suffix(value: str | None) -> str:
    if value == "Z" or value == "UTC":
        return "+00:00"
    # Chinese public listing pages that write a local clock without an offset
    # are explicitly interpreted using the same Asia/Shanghai convention as
    # ``parse_public_time``; never treat an undated row as midnight.
    return "+08:00"


def normalize_market_information_rows(
    official_rows: list[dict[str, Any]] | dict[str, Any] | None,
    media_rows: list[dict[str, Any]] | dict[str, Any] | None,
    *,
    as_of: datetime,
    fetched_at: datetime,
) -> NormalizationResult:
    """Normalize official-first, boundary-safe Market Information events.

    A media repost of an official release becomes a reference on the official
    canonical event.  Repost count is deliberately never exposed as evidence
    strength.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None or fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("market information times must be timezone-aware")
    candidates: list[dict[str, Any]] = []
    invalid = 0
    for tier, source in (("official", official_rows), ("media", media_rows)):
        for raw in rows_from_payload(source or []):
            item = _market_information_row(raw, tier=tier, as_of=as_of, fetched_at=fetched_at)
            if item is None:
                invalid += 1
                continue
            candidates.append(item)
    # Group by original publication URL where known, otherwise stable content.
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in candidates:
        key = str(item["canonical_key"])
        grouped.setdefault(key, []).append(item)
    events: list[dict[str, Any]] = []
    for key, values in sorted(grouped.items()):
        values.sort(key=lambda item: (0 if item["source_tier"] == "official" else 1, item["published_at"], item["source_locator"]))
        canonical = dict(values[0])
        references = sorted({str(item["source_locator"]) for item in values if item["source_locator"] != canonical["source_locator"]})
        canonical["references"] = references
        canonical.pop("canonical_key", None)
        canonical["content_hash"] = hashlib.sha256(
            json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        events.append(canonical)
    events.sort(key=lambda item: (item["published_at"], item["event_id"]))
    payload = {
        "status": "passed" if events else ("warning" if invalid else "empty"),
        "items": events[-MAX_ITEMS:],
        "invalid_rows": invalid,
        "schema_version": MARKET_INFORMATION_SCHEMA_VERSION,
        "fetched_at": fetched_at.isoformat(),
    }
    return NormalizationResult(
        payload,
        "passed" if events else "warning",
        None if events else "no boundary-safe market information is available",
    )


class MarketInformationProvider:
    """Scope-safe provider that never accepts or sends a stock code."""

    provider_id = "market-information"

    def __init__(
        self,
        official_fetcher: Callable[[], Any] | None = None,
        media_fetcher: Callable[[], Any] | None = None,
    ) -> None:
        self.official_fetcher = official_fetcher or (lambda: [])
        self.media_fetcher = media_fetcher or (lambda: [])

    def fetch(self, request: ProductRequest, *, dependencies: dict[str, Any] | None = None) -> ProviderObservation:
        if request.product.id != "market_information":
            raise ValueError(f"{self.provider_id} does not provide {request.product}")
        if not request.subject.is_market or request.subject.code is not None:
            raise ValueError("market information requires a code-free Market Subject")
        fetched_at = datetime.now(timezone.utc)
        official: Any = []
        media: Any = []
        errors: list[str] = []
        try:
            official = self.official_fetcher()
        except Exception as error:
            errors.append(f"official:{exception_type_chain(error)}")
        try:
            media = self.media_fetcher()
        except Exception as error:
            errors.append(f"media:{exception_type_chain(error)}")
        normalized = normalize_market_information_rows(
            official,
            media,
            as_of=request.boundary.as_of,
            fetched_at=fetched_at,
        )
        message = normalized.quality_message
        if errors:
            # A usable result can still be degraded: retain which source tier
            # failed without persisting transport text or weakening the
            # successfully normalized evidence.
            message = "; ".join(errors)[:240]
        return ProviderObservation(
            provider=self.provider_id,
            payload=normalized.payload,
            observed_at=request.boundary.as_of,
            fetched_at=fetched_at,
            source_locator="market-information://official-first",
            quality_status=normalized.quality_status,
            quality_message=message,
            coverage=1.0 if normalized.quality_status == "passed" else 0.0,
            schema_version=MARKET_INFORMATION_SCHEMA_VERSION,
        )


def _market_information_row(
    raw: dict[str, Any],
    *,
    tier: str,
    as_of: datetime,
    fetched_at: datetime,
) -> dict[str, Any] | None:
    published = parse_public_time(first_value(raw, "published_at", "time", "date", "发布时间"))
    if published is None or published > as_of:
        return None
    title = bounded_text(first_value(raw, "title", "name", "标题") or "", 500)
    summary = bounded_text(first_value(raw, "summary", "intro", "摘要") or "", 1500)
    locator = bounded_text(first_value(raw, "original_url", "url", "link", "链接") or "", 500)
    publisher = bounded_text(first_value(raw, "publisher", "source", "来源") or "", 160)
    category = bounded_text(first_value(raw, "category", "类别") or "market", 80)
    rumor = bool(first_value(raw, "rumor", "unconfirmed", "传闻"))
    if not title or not locator or not publisher or rumor:
        return None
    original = bounded_text(first_value(raw, "original_url", "primary_url") or "", 500)
    canonical_key = original or stable_key(title, summary)
    event_id = "market-info-" + hashlib.sha256(
        json.dumps([canonical_key, published.isoformat()], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return {
        "event_id": event_id,
        "category": category,
        "title": title,
        "summary": summary,
        "publisher": publisher,
        "source_tier": tier,
        "published_at": published.isoformat(),
        "observed_at": min(published, as_of).isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "source_locator": locator,
        "canonical_key": canonical_key,
    }
