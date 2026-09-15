"""Whole-market intraday snapshots with whole-package fallback only.

The module deliberately does not reuse the per-security quote adapter.  A
Market Request is one observation of a bounded A-share universe, so a source
either returns a complete package that meets the documented coverage contract
or it contributes nothing.  This keeps provenance replayable and prevents a
seemingly healthy snapshot assembled from incompatible observation windows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time as wall_time
from typing import Any, Callable, Iterable, Protocol
from zoneinfo import ZoneInfo

import requests

from advisor.research.data_products.engine import ProductRequest, ProductUnavailable, ProviderObservation
from advisor.research.market_time import a_share_date
from advisor.sina_rate_limit import PROCESS_SINA_LIMITER


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CODE = re.compile(r"^(?:00[0-3]|30[0-1]|60[0135]|688)[0-9]{3}$")
_SINA_KLINE_ENDPOINT = "https://quotes.sina.cn/cn/api/openapi.php/CN_MarketDataService.getKLineData"


class _RequestStartLimiter:
    """Thread-safe fixed-spacing request-start limiter."""

    def __init__(self, minimum_interval_seconds: float = 0.5) -> None:
        if (
            isinstance(minimum_interval_seconds, bool)
            or not isinstance(minimum_interval_seconds, (int, float))
            or not math.isfinite(float(minimum_interval_seconds))
            or minimum_interval_seconds < 0
        ):
            raise ValueError("Sina request interval is invalid")
        self._minimum_interval_seconds = float(minimum_interval_seconds)
        self._lock = threading.Lock()
        self._last_request = 0.0

    def acquire(self) -> None:
        with self._lock:
            elapsed = wall_time.monotonic() - self._last_request
            if elapsed < self._minimum_interval_seconds:
                wall_time.sleep(self._minimum_interval_seconds - elapsed)
            self._last_request = wall_time.monotonic()


_SINA_REQUEST_LIMITER = PROCESS_SINA_LIMITER


@dataclass(frozen=True)
class ExpectedSecurity:
    code: str
    name: str | None = None
    list_date: date | None = None
    delist_date: date | None = None


@dataclass(frozen=True)
class BulkPage:
    """One source page, normalized before aggregation.

    ``observed_at`` is the source page observation time.  It is intentionally
    page-specific so a delayed page cannot silently masquerade as the same
    intraday observation as the first page.
    """

    page: int
    # ``0`` is an explicit source-owned "unknown total" sentinel used by
    # list-only bulk endpoints.  The collector then reads consecutive pages
    # until it receives a short page; it never pretends page one is complete.
    total_pages: int
    rows: tuple[dict[str, Any], ...]
    observed_at: datetime
    fetched_at: datetime
    source_locator: str


class PageFetcher(Protocol):
    def __call__(self, page: int, page_size: int) -> BulkPage | dict[str, Any]: ...


class ExpectedUniverse(Protocol):
    def active(self, as_of: datetime) -> tuple[ExpectedSecurity, ...]: ...


class TradingSessionAuthority(Protocol):
    """Explicit trading-session facts used to distinguish holidays from gaps."""

    def session_state(self, as_of: datetime) -> str:
        """Return ``open``, ``closed`` or ``unknown`` for the local date."""

    def latest_completed_session(self, as_of: datetime) -> date:
        """Return a locally proved latest completed session or fail closed."""


class CurrentSessionAuthority(Protocol):
    """Independent proof for whether the current Shanghai session is live."""

    def current_session_state(self, as_of: datetime) -> str:
        """Return ``open``/``completed`` only when the current date has dual proof."""


class SqliteTradingSessionAuthority:
    """Read only the observed ``trading_sessions`` table; never guess holidays.

    A missing weekday is only known to be a holiday when the persisted session
    set extends beyond it.  For an unobserved current weekday, the authority
    returns ``unknown`` so an intraday Provider blocks rather than relabeling
    an older quote as current.
    """

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path).expanduser().resolve()

    def session_state(self, as_of: datetime) -> str:
        local = _to_shanghai(as_of)
        if local.date().weekday() >= 5:
            return "closed"
        connection = self._connection()
        try:
            exact = connection.execute(
                "SELECT 1 FROM trading_sessions WHERE trade_date = ?", (local.date().isoformat(),)
            ).fetchone()
            if exact is not None:
                return "open"
            later = connection.execute(
                "SELECT 1 FROM trading_sessions WHERE trade_date > ? LIMIT 1", (local.date().isoformat(),)
            ).fetchone()
            return "closed" if later is not None else "unknown"
        except sqlite3.Error as error:
            raise ProductUnavailable("whole-market trading session authority is unavailable", retryable=False) from error
        finally:
            connection.close()

    def latest_completed_session(self, as_of: datetime) -> date:
        local = _to_shanghai(as_of)
        cutoff = local.date() if local.time() >= time(15, 0) else local.date() - timedelta(days=1)
        connection = self._connection()
        try:
            exact = connection.execute(
                "SELECT 1 FROM trading_sessions WHERE trade_date = ?", (cutoff.isoformat(),)
            ).fetchone()
            later = connection.execute(
                "SELECT 1 FROM trading_sessions WHERE trade_date > ? LIMIT 1", (cutoff.isoformat(),)
            ).fetchone()
            # Weekend absence is intrinsically known.  A weekday absence is
            # only safe if a later persisted session proves it was a holiday.
            if exact is None and cutoff.weekday() < 5 and later is None:
                raise ProductUnavailable("whole-market completed session authority is unavailable", retryable=False)
            row = connection.execute(
                "SELECT MAX(trade_date) FROM trading_sessions WHERE trade_date <= ?", (cutoff.isoformat(),)
            ).fetchone()
        except sqlite3.Error as error:
            raise ProductUnavailable("whole-market trading session authority is unavailable", retryable=False) from error
        finally:
            connection.close()
        if row is None or not isinstance(row[0], str):
            raise ProductUnavailable("whole-market completed session authority is unavailable", retryable=False)
        return date.fromisoformat(row[0])

    def _connection(self) -> sqlite3.Connection:
        if self.database_path.is_symlink() or not self.database_path.is_file():
            raise ProductUnavailable("whole-market trading session authority is unavailable", retryable=False)
        return sqlite3.connect(f"file:{self.database_path}?mode=ro", uri=True)


class SinaBenchmarkCurrentSessionAuthority:
    """Prove a live trading day from fresh SH/SZ benchmark bar timestamps.

    ``trading_sessions`` deliberately contains only completed observations.
    During 09:30--15:00 this bounded, independent probe establishes whether
    both exchange benchmarks are reporting the current local date.  Between
    the close and the 21:00 completed-session persistence job it can also
    prove ``completed`` only from both current-date closing timestamps.
    Failure is ``unknown`` rather than a guessed holiday or implicit session.
    """

    endpoint = _SINA_KLINE_ENDPOINT
    _SYMBOLS = ("sh000001", "sz399001")

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = 5.0,
        max_attempts: int = 2,
        freshness_seconds: int = 600,
        cache_seconds: float = 30.0,
        request_limiter: _RequestStartLimiter | None = None,
        minimum_interval_seconds: float = 0.5,
    ) -> None:
        if timeout <= 0 or not 1 <= max_attempts <= 3 or freshness_seconds < 1 or cache_seconds < 0:
            raise ValueError("current trading session authority configuration is invalid")
        self._session = session or requests.Session()
        if session is None and hasattr(self._session, "trust_env"):
            self._session.trust_env = False
        self._timeout = float(timeout)
        self._max_attempts = max_attempts
        self._freshness_seconds = freshness_seconds
        self._cache_seconds = float(cache_seconds)
        self._cache_lock = threading.Lock()
        self._cached_at = 0.0
        self._cached_date: date | None = None
        self._cached_phase: str | None = None
        self._cached_state = "unknown"
        self._request_limiter = request_limiter or (
            _SINA_REQUEST_LIMITER
            if minimum_interval_seconds == 0.5
            else _RequestStartLimiter(minimum_interval_seconds)
        )

    def current_session_state(self, as_of: datetime) -> str:
        local = _to_shanghai(as_of)
        active = _session_hours(local)
        after_close = time(15, 0) <= local.time() < time(21, 0)
        if not active and not after_close:
            return "unknown"
        phase = "active" if active else "after_close"
        with self._cache_lock:
            if (
                self._cached_date == local.date()
                and self._cached_phase == phase
                and wall_time.monotonic() - self._cached_at <= self._cache_seconds
            ):
                return self._cached_state
        try:
            timestamps = tuple(self._benchmark_timestamp(symbol) for symbol in self._SYMBOLS)
            if active and all(_fresh_current_session(item, local, self._freshness_seconds) for item in timestamps):
                state = "open"
            elif after_close and all(_completed_current_session(item, local) for item in timestamps):
                state = "completed"
            else:
                state = "unknown"
        except (requests.RequestException, ValueError, OSError):
            state = "unknown"
        with self._cache_lock:
            self._cached_date = local.date()
            self._cached_phase = phase
            self._cached_at = wall_time.monotonic()
            self._cached_state = state
        return state

    def _benchmark_timestamp(self, symbol: str) -> datetime:
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            self._request_limiter.acquire()
            try:
                response = self._session.get(
                    self.endpoint,
                    params={"symbol": symbol, "scale": "5", "ma": "no", "datalen": "1"},
                    timeout=self._timeout,
                    headers={
                        "Accept": "application/json, text/javascript, */*;q=0.8",
                        "Referer": "https://finance.sina.com.cn/",
                        "User-Agent": "A-Hunter/1.0",
                    },
                )
                response.raise_for_status()
                payload = response.json()
                result = payload.get("result") if isinstance(payload, dict) else None
                status = result.get("status") if isinstance(result, dict) else None
                rows = result.get("data") if isinstance(result, dict) else None
                status_code = status.get("code") if isinstance(status, dict) else None
                row = rows[-1] if isinstance(rows, list) and rows else None
                close = _number(row.get("close")) if isinstance(row, dict) else None
                if status_code not in {0, "0"} or not isinstance(row, dict) or close is None or close <= 0:
                    raise ValueError("benchmark bar is incomplete")
                raw_day = row.get("day")
                if not isinstance(raw_day, str) or not raw_day.strip():
                    raise ValueError("benchmark bar timestamp is invalid")
                observed = datetime.fromisoformat(raw_day.strip().replace("Z", "+00:00"))
                if observed.tzinfo is None or observed.utcoffset() is None:
                    observed = observed.replace(tzinfo=_SHANGHAI)
                return observed
            except (requests.RequestException, ValueError, OSError) as error:
                last_error = error
                if attempt + 1 < self._max_attempts:
                    wall_time.sleep(min(0.5, 0.1 * (2 ** attempt)))
        assert last_error is not None
        raise last_error


class HybridTradingSessionAuthority:
    """Combine completed-session facts with a current-day open-session proof."""

    def __init__(
        self,
        completed_sessions: SqliteTradingSessionAuthority,
        current_session: CurrentSessionAuthority,
    ) -> None:
        self._completed_sessions = completed_sessions
        self._current_session = current_session

    def session_state(self, as_of: datetime) -> str:
        persisted = self._completed_sessions.session_state(as_of)
        if persisted != "unknown":
            return persisted
        return self._current_session.current_session_state(as_of)

    def latest_completed_session(self, as_of: datetime) -> date:
        try:
            return self._completed_sessions.latest_completed_session(as_of)
        except ProductUnavailable:
            if self._current_session.current_session_state(as_of) == "completed":
                return _to_shanghai(as_of).date()
            raise


class SqliteExpectedUniverse:
    """Read the locally maintained eligible A-share universe without writing."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path).expanduser().resolve()

    def active(self, as_of: datetime) -> tuple[ExpectedSecurity, ...]:
        if self.database_path.is_symlink() or not self.database_path.is_file():
            raise ProductUnavailable("whole-market expected universe is unavailable", retryable=False)
        connection = sqlite3.connect(f"file:{self.database_path}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                """
                SELECT code, name, list_date, delist_date
                FROM securities
                WHERE security_type = 'a_share'
                  AND list_date IS NOT NULL
                  AND list_date <= ?
                  AND (delist_date IS NULL OR delist_date >= ?)
                ORDER BY code
                """,
                (a_share_date(as_of).isoformat(), a_share_date(as_of).isoformat()),
            ).fetchall()
        except sqlite3.Error as error:
            raise ProductUnavailable("whole-market expected universe is unavailable", retryable=False) from error
        finally:
            connection.close()
        result = tuple(
            ExpectedSecurity(
                str(row[0]),
                str(row[1]) if row[1] is not None else None,
                date.fromisoformat(str(row[2])) if row[2] else None,
                date.fromisoformat(str(row[3])) if row[3] else None,
            )
            for row in rows
            if isinstance(row[0], str) and _CODE.fullmatch(row[0])
        )
        if not result:
            raise ProductUnavailable("whole-market expected universe is empty", retryable=False)
        return result


class StaticExpectedUniverse:
    """Small fixture-friendly universe implementation."""

    def __init__(self, securities: Iterable[ExpectedSecurity | dict[str, Any] | str]) -> None:
        normalized: list[ExpectedSecurity] = []
        for item in securities:
            if isinstance(item, ExpectedSecurity):
                security = item
            elif isinstance(item, str):
                security = ExpectedSecurity(item)
            elif isinstance(item, dict):
                security = ExpectedSecurity(
                    str(item.get("code", "")),
                    str(item["name"]) if item.get("name") is not None else None,
                    _as_date(item.get("list_date")),
                    _as_date(item.get("delist_date")),
                )
            else:
                raise ValueError("expected universe item is invalid")
            if not _CODE.fullmatch(security.code):
                raise ValueError("expected universe contains an invalid A-share code")
            normalized.append(security)
        if len({item.code for item in normalized}) != len(normalized):
            raise ValueError("expected universe contains duplicate codes")
        self._securities = tuple(sorted(normalized, key=lambda item: item.code))

    def active(self, as_of: datetime) -> tuple[ExpectedSecurity, ...]:
        local_date = a_share_date(as_of)
        result = tuple(
            item for item in self._securities
            if (item.list_date is None or item.list_date <= local_date)
            and (item.delist_date is None or item.delist_date >= local_date)
        )
        if not result:
            raise ProductUnavailable("whole-market expected universe is empty", retryable=False)
        return result


class PagedWholeMarketIntradayProvider:
    """Aggregate one provider's paginated response into one sealed package.

    A Market Request fixes its logical ``as_of`` when the one-shot capture
    starts.  Pages are consequently allowed to arrive *during* that bounded
    capture window; requiring every HTTP response to predate the start would
    make a real provider fail deterministically.  The source observation
    window and the fetch proof remain in the sealed payload, while the Product
    observation is the immutable capture boundary supplied to the agents.
    """

    provider_id = "whole-market"
    source_name = "whole-market"

    def __init__(
        self,
        universe: ExpectedUniverse,
        page_fetcher: PageFetcher,
        *,
        industry_membership: Callable[[datetime], dict[str, str]] | None = None,
        page_size: int = 200,
        max_pages: int = 100,
        max_window_seconds: int = 180,
        intraday_freshness_seconds: int = 600,
        session_authority: TradingSessionAuthority | None = None,
    ) -> None:
        if page_size < 1 or max_pages < 1 or max_window_seconds < 1 or intraday_freshness_seconds < 1:
            raise ValueError("whole-market pagination configuration is invalid")
        self.universe = universe
        self.page_fetcher = page_fetcher
        self.industry_membership = industry_membership
        self.page_size = page_size
        self.max_pages = max_pages
        self.max_window_seconds = max_window_seconds
        self.intraday_freshness_seconds = intraday_freshness_seconds
        self.session_authority = session_authority

    def fetch(self, request: ProductRequest, *, dependencies: dict[str, Any] | None = None) -> ProviderObservation:
        if request.product.id != "whole_market_intraday_snapshot":
            raise ValueError(f"{self.provider_id} does not provide {request.product}")
        if not request.subject.is_market:
            raise ProductUnavailable("whole-market snapshot requires a Market Subject", retryable=False)
        expected = self.universe.active(request.boundary.as_of)
        pages = self._fetch_all_pages(request.boundary.as_of)
        payload, observed_at, fetched_at, locator = self._seal(expected, pages, request.boundary.as_of)
        return ProviderObservation(
            provider=self.provider_id,
            payload=payload,
            observed_at=observed_at,
            fetched_at=fetched_at,
            source_locator=locator,
            coverage=float(payload["coverage"]["overall"]),
            schema_version="whole-market-intraday@1",
        )

    def _fetch_all_pages(self, boundary: datetime) -> tuple[BulkPage, ...]:
        pages: list[BulkPage] = []
        total_pages: int | None = None
        unknown_total = False
        capture_deadline = boundary + timedelta(seconds=self.max_window_seconds)
        for page_number in range(1, self.max_pages + 1):
            raw = self.page_fetcher(page_number, self.page_size)
            page = _bulk_page(raw, expected_page=page_number)
            if page.observed_at > capture_deadline or page.fetched_at > capture_deadline:
                raise ProductUnavailable("whole-market page is outside the bounded capture window", retryable=False)
            pages.append(page)
            if page.total_pages == 0:
                if total_pages is not None:
                    raise ProductUnavailable("whole-market source page total changed", retryable=False)
                unknown_total = True
                # A list-only response supplies no trustworthy total.  A
                # short page is the only proof that all preceding pages were
                # read; an exact multiple requires one final empty page.
                if len(page.rows) < self.page_size:
                    break
                continue
            if unknown_total:
                raise ProductUnavailable("whole-market source page total changed", retryable=False)
            if total_pages is None:
                total_pages = page.total_pages
            if page.total_pages != total_pages:
                raise ProductUnavailable("whole-market page count is inconsistent", retryable=False)
            if page_number > total_pages:
                raise ProductUnavailable("whole-market source returned an unexpected extra page", retryable=False)
            if page_number == total_pages:
                break
        if not pages or (not unknown_total and (total_pages is None or len(pages) != total_pages)):
            raise ProductUnavailable("whole-market source pages are incomplete", retryable=False)
        if unknown_total and len(pages[-1].rows) >= self.page_size:
            raise ProductUnavailable("whole-market source pages exceed the bounded page limit", retryable=False)
        window_start = min(item.observed_at for item in pages)
        window_end = max(item.observed_at for item in pages)
        if (window_end - window_start).total_seconds() > self.max_window_seconds:
            raise ProductUnavailable("whole-market page observation window is too wide", retryable=False)
        return tuple(pages)

    def _seal(
        self,
        expected: tuple[ExpectedSecurity, ...],
        pages: tuple[BulkPage, ...],
        boundary: datetime,
    ) -> tuple[dict[str, Any], datetime, datetime, str]:
        expected_by_code = {item.code: item for item in expected}
        all_rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        legal_absences: list[dict[str, str]] = []
        for page in pages:
            for raw in page.rows:
                if not isinstance(raw, dict):
                    raise ProductUnavailable("whole-market row is invalid", retryable=False)
                candidate = _market_code(raw.get("code") or raw.get("symbol") or raw.get("f12"))
                if not _CODE.fullmatch(candidate) or candidate not in expected_by_code:
                    # Bulk endpoints can include BSE shares, indices, funds,
                    # B-shares and listings outside the sealed local scope.
                    # Rejecting their quote shape before scope filtering made
                    # a valid Shanghai/Shenzhen package fail atomically.
                    continue
                normalized = _normalize_row(raw)
                code = normalized["code"]
                if normalized.get("absence_reason") is not None:
                    if code in seen:
                        raise ProductUnavailable("whole-market row conflicts with a legal absence", retryable=False)
                    seen.add(code)
                    legal_absences.append({"code": code, "reason": str(normalized.pop("absence_reason"))})
                    continue
                if code in seen:
                    raise ProductUnavailable("whole-market source contains duplicate or conflicting codes", retryable=False)
                seen.add(code)
                all_rows.append(normalized)
        all_rows.sort(key=lambda item: item["code"])
        legal_absences.sort(key=lambda item: item["code"])
        unknown_missing = sorted(set(expected_by_code) - seen)
        coverage = _coverage_proof(
            expected_by_code,
            {item["code"] for item in all_rows},
            {item["code"] for item in legal_absences},
            unknown_missing,
            self.industry_membership(boundary) if self.industry_membership is not None else None,
        )
        if coverage["unknown_gap_count"] / coverage["expected_count"] > 0.01:
            raise ProductUnavailable("whole-market unknown coverage gap exceeds 1%", retryable=False)
        if any(float(item["coverage"]) < 0.95 for item in coverage["industry_groups"]):
            raise ProductUnavailable("whole-market industry coverage is below 95%", retryable=False)
        source_observed_at = max(item.observed_at for item in pages)
        _validate_session_freshness(
            boundary,
            source_observed_at,
            rows=all_rows,
            intraday_freshness_seconds=self.intraday_freshness_seconds,
            max_capture_seconds=self.max_window_seconds,
            session_authority=self.session_authority,
        )
        expected_codes = sorted(expected_by_code)
        window_start = min(item.observed_at for item in pages)
        window_end = max(item.observed_at for item in pages)
        payload: dict[str, Any] = {
            "status": "passed",
            "source": self.source_name,
            "expected_universe": {
                "codes": expected_codes,
                "count": len(expected_codes),
                "content_hash": _hash(expected_codes),
                # Keep the locally observed identity mapping sealed with the
                # universe. A legally absent security has no quote row, but
                # Market-output safety still needs to recognize its name.
                "securities": [
                    {"code": item.code, "name": item.name}
                    for item in sorted(expected, key=lambda item: item.code)
                    if item.name is not None
                ],
            },
            "observation_window": {
                "started_at": window_start.isoformat(),
                "ended_at": window_end.isoformat(),
                "seconds": int((window_end - window_start).total_seconds()),
                "capture_boundary_at": boundary.isoformat(),
            },
            "rows": all_rows,
            "legal_absences": legal_absences,
            "coverage": coverage,
            "page_proof": {
                "page_count": len(pages),
                "pages": [item.page for item in pages],
                "first_fetched_at": min(item.fetched_at for item in pages).isoformat(),
                "last_fetched_at": max(item.fetched_at for item in pages).isoformat(),
            },
        }
        payload["content_hash"] = _hash(payload)
        return (
            payload,
            # The pages prove the source observation window.  ``observed_at``
            # on the Product is intentionally the fixed logical boundary so
            # generic Data Product validation can continue to reject future
            # evidence without treating normal capture latency as future data.
            boundary,
            max(item.fetched_at for item in pages),
            pages[0].source_locator,
        )


class _RateLimitedJsonSource:
    """Small shared page limiter with bounded retry for one bulk adapter."""

    def _configure_http_source(
        self,
        *,
        session: requests.Session,
        timeout: float,
        max_attempts: int,
        minimum_interval_seconds: float,
        request_limiter: _RequestStartLimiter | None = None,
    ) -> None:
        if timeout <= 0 or max_attempts < 1 or max_attempts > 5 or minimum_interval_seconds < 0:
            raise ValueError("whole-market HTTP configuration is invalid")
        self._session = session
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._request_limiter = request_limiter or (
            _SINA_REQUEST_LIMITER
            if minimum_interval_seconds == 0.5
            else _RequestStartLimiter(minimum_interval_seconds)
        )

    def _get_json(self, endpoint: str, params: dict[str, Any]) -> tuple[Any, datetime]:
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            self._request_limiter.acquire()
            try:
                response = self._session.get(
                    endpoint,
                    params=params,
                    timeout=self._timeout,
                    headers={"User-Agent": "A-Hunter/1.0"},
                )
                response.raise_for_status()
                return response.json(), datetime.now(timezone.utc)
            except (requests.RequestException, ValueError) as error:
                last_error = error
                if attempt + 1 < self._max_attempts:
                    wall_time.sleep(min(1.0, 0.1 * (2 ** attempt)))
        assert last_error is not None
        raise last_error


class SinaWholeMarketIntradayProvider(_RateLimitedJsonSource, PagedWholeMarketIntradayProvider):
    provider_id = "sina-whole-market"
    source_name = "sina"

    def __init__(
        self,
        universe: ExpectedUniverse,
        page_fetcher: PageFetcher | None = None,
        *,
        session: requests.Session | None = None,
        timeout: float = 10.0,
        max_attempts: int = 3,
        minimum_interval_seconds: float = 0.5,
        request_limiter: _RequestStartLimiter | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("page_size", 100)
        current_session = session or requests.Session()
        if session is None and hasattr(current_session, "trust_env"):
            current_session.trust_env = False
        self._configure_http_source(
            session=current_session,
            timeout=timeout,
            max_attempts=max_attempts,
            minimum_interval_seconds=minimum_interval_seconds,
            request_limiter=request_limiter,
        )
        super().__init__(universe, page_fetcher or self._fetch_page, **kwargs)

    def _fetch_page(self, page: int, page_size: int) -> BulkPage:
        endpoint = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
        raw, fetched_at = self._get_json(
            endpoint,
            {"page": page, "num": page_size, "sort": "symbol", "asc": "1", "node": "hs_a"},
        )
        rows = raw if isinstance(raw, list) else raw.get("data", []) if isinstance(raw, dict) else []
        total = raw.get("total") if isinstance(raw, dict) else None
        # Sina commonly returns a bare JSON list without a total.  Mark that
        # explicitly so the shared collector reads to a short final page.
        total_pages = max(page, math.ceil(int(total) / page_size)) if isinstance(total, (int, float, str)) and str(total).isdigit() else 0
        return BulkPage(
            page,
            total_pages,
            _attach_session_date(rows if isinstance(rows, list) else (), _capture_session_date(self.session_authority, fetched_at)),
            fetched_at,
            fetched_at,
            endpoint,
        )

class WholeMarketIntradayProvider:
    """Optional explicit coordinator for callers outside ``DataProductEngine``.

    It invokes the backup only when the primary package is rejected.  The
    returned observation is exactly one source package; no row or field merge
    occurs here or in the engine.
    """

    provider_id = "whole-market-intraday"

    def __init__(self, primary: PagedWholeMarketIntradayProvider, fallback: PagedWholeMarketIntradayProvider) -> None:
        self.primary = primary
        self.fallback = fallback

    def fetch(self, request: ProductRequest, *, dependencies: dict[str, Any] | None = None) -> ProviderObservation:
        try:
            return self.primary.fetch(request, dependencies=dependencies)
        except Exception:
            # The coordinator is used by callers outside DataProductEngine.
            # Mirror the engine's whole-package fallback policy even when a
            # transport/JSON error escapes an adapter.
            return self.fallback.fetch(request, dependencies=dependencies)


def _bulk_page(value: BulkPage | dict[str, Any], *, expected_page: int) -> BulkPage:
    if isinstance(value, BulkPage):
        page = value
    elif isinstance(value, dict):
        rows = value.get("rows", value.get("data", ()))
        page = BulkPage(
            int(value.get("page", expected_page)),
            int(value.get("total_pages", 1)),
            tuple(rows) if isinstance(rows, list) else (),
            _as_datetime(value.get("observed_at")),
            _as_datetime(value.get("fetched_at")),
            str(value.get("source_locator", "fixture://whole-market")),
        )
    else:
        raise ProductUnavailable("whole-market page is invalid", retryable=False)
    if page.page != expected_page or (page.total_pages != 0 and page.total_pages < page.page):
        raise ProductUnavailable("whole-market page sequence is invalid", retryable=False)
    if page.observed_at.tzinfo is None or page.observed_at.utcoffset() is None or page.fetched_at.tzinfo is None or page.fetched_at.utcoffset() is None:
        raise ProductUnavailable("whole-market page time is invalid", retryable=False)
    if not page.source_locator or len(page.source_locator) > 500:
        raise ProductUnavailable("whole-market source locator is invalid", retryable=False)
    return page


def _normalize_row(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ProductUnavailable("whole-market row is invalid", retryable=False)
    code_value = raw.get("code") or raw.get("symbol") or raw.get("f12")
    code = _market_code(code_value)
    if not _CODE.fullmatch(code):
        raise ProductUnavailable("whole-market row contains an invalid code", retryable=False)
    suspended = raw.get("suspended", raw.get("is_suspended", raw.get("status")))
    if suspended is True or str(suspended).lower() in {"suspended", "halted", "停牌"}:
        return {"code": code, "absence_reason": "suspended"}
    name = str(raw.get("name", raw.get("f14", ""))).strip()
    # Sina's list endpoint names these fields differently from the Eastmoney
    # column keys. Normalize source schemas here before sealing the common
    # package; no consumer has to infer a provider-specific quote shape.
    price = _number(raw.get("price", raw.get("trade", raw.get("f2"))))
    previous_close = _number(raw.get("previous_close", raw.get("pre_close", raw.get("settlement", raw.get("f18")))))
    change_pct = _number(raw.get("change_pct", raw.get("changepercent", raw.get("f3"))))
    volume = _number(raw.get("volume", raw.get("f5")))
    amount = _number(raw.get("amount", raw.get("f6")))
    open_price = _number(raw.get("open", raw.get("f17")))
    high_price = _number(raw.get("high", raw.get("f15")))
    low_price = _number(raw.get("low", raw.get("f16")))
    if (
        price == 0
        and previous_close is not None
        and previous_close > 0
        and open_price == 0
        and high_price == 0
        and low_price == 0
        and volume == 0
        and amount == 0
    ):
        # Sina represents a suspended security as an identity row with a
        # positive previous close and an otherwise complete all-zero quote.
        # Requiring every field prevents a malformed or truncated row from
        # being reclassified as a legal absence.
        return {"code": code, "absence_reason": "suspended"}
    if not name or price is None or price <= 0 or previous_close is None or previous_close <= 0 or volume is None or volume < 0 or amount is None or amount < 0:
        raise ProductUnavailable("whole-market row is missing a required quote field", retryable=False)
    if change_pct is None:
        change_pct = (price / previous_close - 1) * 100
    if not math.isfinite(change_pct):
        raise ProductUnavailable("whole-market row change is invalid", retryable=False)
    result = {
        "code": code,
        "name": name[:120],
        "price": price,
        "previous_close": previous_close,
        "change_pct": change_pct,
        "volume": int(volume) if float(volume).is_integer() else volume,
        "amount": amount,
    }
    session_date = raw.get("session_date", raw.get("trade_date", raw.get("date")))
    if session_date not in (None, ""):
        try:
            result["session_date"] = _as_date(session_date).isoformat()  # type: ignore[union-attr]
        except (TypeError, ValueError):
            raise ProductUnavailable("whole-market row session date is invalid", retryable=False) from None
    return result


def _coverage_proof(
    expected: dict[str, ExpectedSecurity],
    observed: set[str],
    legal_absent: set[str],
    unknown_missing: list[str],
    industries: dict[str, str] | None,
) -> dict[str, Any]:
    expected_count = len(expected)
    effective = len(observed | legal_absent)
    groups: list[dict[str, Any]] = []
    if industries is not None:
        known_industries = {code: str(group) for code, group in industries.items() if code in expected and isinstance(group, str) and group}
        for group in sorted(set(known_industries.values())):
            group_codes = {code for code, value in known_industries.items() if value == group}
            if not group_codes:
                continue
            group_effective = len(group_codes & (observed | legal_absent))
            groups.append({
                "industry_id": group,
                "expected_count": len(group_codes),
                "observed_count": len(group_codes & observed),
                "legal_absence_count": len(group_codes & legal_absent),
                "unknown_gap_count": len(group_codes - observed - legal_absent),
                "coverage": group_effective / len(group_codes),
            })
    return {
        "expected_count": expected_count,
        "observed_count": len(observed),
        "legal_absence_count": len(legal_absent),
        "unknown_gap_count": len(unknown_missing),
        "unknown_gap_codes": unknown_missing,
        "overall": effective / expected_count,
        "industry_taxonomy_available": industries is not None,
        "industry_groups": groups,
    }


def _validate_session_freshness(
    boundary: datetime,
    observed_at: datetime,
    *,
    rows: list[dict[str, Any]],
    intraday_freshness_seconds: int,
    max_capture_seconds: int,
    session_authority: TradingSessionAuthority | None = None,
) -> None:
    local = boundary.astimezone(_SHANGHAI)
    observed_local = observed_at.astimezone(_SHANGHAI)
    session_hours = _session_hours(local)
    if session_authority is None:
        in_session = local.weekday() < 5 and session_hours
    else:
        state = session_authority.session_state(boundary)
        if state not in {"open", "completed", "closed", "unknown"}:
            raise ProductUnavailable("whole-market trading session authority is invalid", retryable=False)
        if session_hours and state == "unknown":
            raise ProductUnavailable("whole-market trading session authority is unavailable", retryable=False)
        in_session = session_hours and state == "open"
    if in_session:
        if (
            observed_at > boundary + timedelta(seconds=max_capture_seconds)
            or (boundary - observed_at).total_seconds() > intraday_freshness_seconds
        ):
            raise ProductUnavailable("whole-market intraday snapshot is stale", retryable=False)
        session_dates = {str(row.get("session_date", "")) for row in rows if row.get("session_date") is not None}
        if session_dates and session_dates != {local.date().isoformat()}:
            raise ProductUnavailable("whole-market intraday snapshot session is invalid", retryable=False)
        return
    # Outside the active session the source must explicitly identify a
    # completed session rather than reuse yesterday's snapshot as if current.
    session_dates = {str(row.get("session_date", "")) for row in rows if row.get("session_date") is not None}
    if len(session_dates) != 1:
        raise ProductUnavailable("whole-market completed session proof is invalid", retryable=False)
    session_date = next(iter(session_dates))
    expected_session_date = (
        session_authority.latest_completed_session(boundary)
        if session_authority is not None
        else _latest_completed_session(local)
    )
    if session_date != expected_session_date.isoformat() or observed_local.date() > local.date():
        raise ProductUnavailable("whole-market snapshot session is invalid", retryable=False)


def _latest_completed_session(local: datetime) -> date:
    """Return the latest completed business-session date at the boundary.

    The bulk source does not carry a trading-calendar authority, so a holiday
    cannot be guessed safely.  Requiring the latest weekday session may reject
    a valid long-holiday snapshot, but it cannot admit an arbitrarily old one
    as current.  A calendar-aware provider can still seal its own Product
    before this fail-closed check.
    """
    candidate = local.date()
    if candidate.weekday() >= 5 or local.time() < time(15, 0):
        candidate -= timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _capture_session_date(
    session_authority: TradingSessionAuthority | None,
    captured_at: datetime,
) -> date | None:
    """Attach an authority-backed session date to default bulk source rows."""
    if session_authority is None:
        return None
    local = _to_shanghai(captured_at)
    session_hours = _session_hours(local)
    state = session_authority.session_state(captured_at)
    if state == "open":
        return local.date() if session_hours else session_authority.latest_completed_session(captured_at)
    if state == "completed":
        return local.date()
    if state == "closed":
        return session_authority.latest_completed_session(captured_at)
    if state == "unknown" and local.time() < time(9, 30):
        # Before today's market can have opened, the latest completed session
        # remains the only possible source snapshot.  The persisted authority
        # still fails closed across an unproved weekday/holiday gap.
        return session_authority.latest_completed_session(captured_at)
    # An unproved weekday (including the noon break) must not have a bare
    # quote relabeled as yesterday's completed session.
    return None


def _attach_session_date(rows: Iterable[Any], session_date: date | None) -> tuple[dict[str, Any] | Any, ...]:
    """Do not overwrite source-provided dates; add only authority-backed proof."""
    result: list[dict[str, Any] | Any] = []
    for row in rows:
        if isinstance(row, dict) and session_date is not None and row.get("session_date") in (None, ""):
            result.append({**row, "session_date": session_date.isoformat()})
        else:
            result.append(row)
    return tuple(result)


def _to_shanghai(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProductUnavailable("whole-market session time is invalid", retryable=False)
    return value.astimezone(_SHANGHAI)


def _session_hours(local: datetime) -> bool:
    return time(9, 30) <= local.time() <= time(11, 30) or time(13, 0) <= local.time() < time(15, 0)


def _fresh_current_session(timestamp: datetime, local: datetime, freshness_seconds: int) -> bool:
    observed = _to_shanghai(timestamp)
    return observed.date() == local.date() and abs((local - observed).total_seconds()) <= freshness_seconds


def _completed_current_session(timestamp: datetime, local: datetime) -> bool:
    observed = _to_shanghai(timestamp)
    return observed.date() == local.date() and observed.time() >= time(15, 0) and observed <= local


def _number(value: Any) -> float | None:
    if value in (None, "", "--", "-", "None"):
        return None
    try:
        result = float(str(value).strip().replace(",", "").rstrip("%"))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value)
    else:
        raise ProductUnavailable("whole-market page time is invalid", retryable=False)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ProductUnavailable("whole-market page time is invalid", retryable=False)
    return result


def _as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    return value if isinstance(value, date) and not isinstance(value, datetime) else date.fromisoformat(str(value))


def _market_code(value: Any) -> str:
    if isinstance(value, int):
        return str(value).zfill(6)
    if isinstance(value, float) and value.is_integer():
        return str(int(value)).zfill(6)
    text = str(value or "").strip()
    match = re.fullmatch(r"(?:(?:sh|sz|bj)[._-]?)?([0-9]{6})", text, flags=re.IGNORECASE)
    return match.group(1) if match is not None else text


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


__all__ = [
    "BulkPage",
    "CurrentSessionAuthority",
    "ExpectedSecurity",
    "HybridTradingSessionAuthority",
    "PagedWholeMarketIntradayProvider",
    "SinaBenchmarkCurrentSessionAuthority",
    "SinaWholeMarketIntradayProvider",
    "SqliteExpectedUniverse",
    "SqliteTradingSessionAuthority",
    "StaticExpectedUniverse",
    "TradingSessionAuthority",
    "WholeMarketIntradayProvider",
]
