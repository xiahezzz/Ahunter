from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from advisor.research.contracts import ResearchBoundary, ResearchScope, ResearchSubject, VersionRef
from advisor.research.data_products.engine import ProductRequest, ProductUnavailable
from advisor.research.providers.whole_market_intraday import (
    BulkPage,
    HybridTradingSessionAuthority,
    PagedWholeMarketIntradayProvider,
    SinaBenchmarkCurrentSessionAuthority,
    SinaWholeMarketIntradayProvider,
    SqliteTradingSessionAuthority,
    StaticExpectedUniverse,
    WholeMarketIntradayProvider,
    _capture_session_date,
)
from advisor.research.repository import ResearchRepository
from advisor.sina_rate_limit import PROCESS_SINA_LIMITER


BOUNDARY = datetime(2026, 8, 6, 2, 0, tzinfo=timezone.utc)  # 10:00 Shanghai, in session.


def _request(boundary: datetime = BOUNDARY) -> ProductRequest:
    return ProductRequest(
        product=VersionRef.parse("whole_market_intraday_snapshot@1"),
        subject=ResearchSubject(scope=ResearchScope.market),
        boundary=ResearchBoundary(as_of=boundary),
    )


def _row(code: str = "600000", **extra: object) -> dict[str, object]:
    return {
        "code": code,
        "name": "样例股份",
        "price": 10,
        "previous_close": 9.8,
        "change_pct": 2.04,
        "volume": 1000,
        "amount": 10000,
        **extra,
    }


def _page(
    *,
    observed_at: datetime,
    fetched_at: datetime | None = None,
    rows: tuple[dict[str, object], ...] | None = None,
) -> BulkPage:
    return BulkPage(
        page=1,
        total_pages=1,
        rows=rows or (_row(),),
        observed_at=observed_at,
        fetched_at=fetched_at or observed_at,
        source_locator="fixture://whole-market/page/1",
    )


class _SessionAuthority:
    def __init__(self, *, state: str, completed: date) -> None:
        self.state = state
        self.completed = completed

    def session_state(self, _as_of: datetime) -> str:
        return self.state

    def latest_completed_session(self, _as_of: datetime) -> date:
        return self.completed


class _CurrentSessionAuthority:
    def __init__(self, state: str = "open") -> None:
        self.state = state

    def current_session_state(self, _as_of: datetime) -> str:
        return self.state


def test_capture_window_allows_normal_post_start_page_latency_but_seals_logical_boundary():
    provider = PagedWholeMarketIntradayProvider(
        StaticExpectedUniverse(["600000"]),
        lambda _page_number, _page_size: _page(observed_at=BOUNDARY + timedelta(seconds=4), fetched_at=BOUNDARY + timedelta(seconds=5)),
        max_window_seconds=10,
    )

    observation = provider.fetch(_request())

    assert observation.observed_at == BOUNDARY
    assert observation.fetched_at == BOUNDARY + timedelta(seconds=5)
    assert observation.payload["observation_window"] == {
        "started_at": (BOUNDARY + timedelta(seconds=4)).isoformat(),
        "ended_at": (BOUNDARY + timedelta(seconds=4)).isoformat(),
        "seconds": 0,
        "capture_boundary_at": BOUNDARY.isoformat(),
    }


def test_sealed_expected_universe_keeps_names_for_legal_quote_absences():
    provider = PagedWholeMarketIntradayProvider(
        StaticExpectedUniverse([{"code": "600000", "name": "样例股份"}]),
        lambda _page_number, _page_size: _page(
            observed_at=BOUNDARY,
            rows=({"code": "600000", "suspended": True},),
        ),
    )

    observation = provider.fetch(_request())

    assert observation.payload["legal_absences"] == [{"code": "600000", "reason": "suspended"}]
    assert observation.payload["expected_universe"]["securities"] == [
        {"code": "600000", "name": "样例股份"}
    ]


def test_complete_all_zero_quote_with_positive_previous_close_is_a_legal_suspension():
    provider = PagedWholeMarketIntradayProvider(
        StaticExpectedUniverse(["600000"]),
        lambda _page_number, _page_size: _page(
            observed_at=BOUNDARY,
            rows=({
                "code": "600000",
                "name": "停牌样例",
                "trade": "0.000",
                "settlement": "2.500",
                "open": "0.000",
                "high": "0.000",
                "low": "0.000",
                "volume": 0,
                "amount": 0,
            },),
        ),
    )

    observation = provider.fetch(_request())

    assert observation.payload["rows"] == []
    assert observation.payload["legal_absences"] == [{"code": "600000", "reason": "suspended"}]


def test_capture_window_rejects_a_page_arriving_after_its_bounded_seal_deadline():
    provider = PagedWholeMarketIntradayProvider(
        StaticExpectedUniverse(["600000"]),
        lambda _page_number, _page_size: _page(observed_at=BOUNDARY + timedelta(seconds=11)),
        max_window_seconds=10,
    )

    with pytest.raises(ProductUnavailable, match="bounded capture window"):
        provider.fetch(_request())


def test_off_session_snapshot_requires_explicit_completed_session_proof():
    boundary = datetime(2026, 8, 6, 20, 0, tzinfo=timezone.utc)
    provider = PagedWholeMarketIntradayProvider(
        StaticExpectedUniverse(["600000"]),
        lambda _page_number, _page_size: _page(
            observed_at=boundary,
            rows=(_row(session_date="2026-08-06"),),
        ),
    )
    assert provider.fetch(_request(boundary)).payload["rows"][0]["session_date"] == "2026-08-06"

    unproven = PagedWholeMarketIntradayProvider(
        StaticExpectedUniverse(["600000"]),
        lambda _page_number, _page_size: _page(observed_at=boundary),
    )
    with pytest.raises(ProductUnavailable, match="completed session proof"):
        unproven.fetch(_request(boundary))


def test_preopen_unknown_current_day_uses_only_the_authority_proved_completed_session():
    authority = _SessionAuthority(state="unknown", completed=date(2026, 8, 7))
    monday_preopen = datetime(2026, 8, 9, 16, 10, tzinfo=timezone.utc)  # Monday 00:10 Shanghai.
    monday_lunch = datetime(2026, 8, 10, 4, 0, tzinfo=timezone.utc)  # Monday 12:00 Shanghai.

    assert _capture_session_date(authority, monday_preopen) == date(2026, 8, 7)
    assert _capture_session_date(authority, monday_lunch) is None


def test_expected_universe_uses_the_shanghai_calendar_date_at_a_utc_day_boundary():
    monday_in_shanghai = datetime(2026, 8, 9, 16, 30, tzinfo=timezone.utc)
    universe = StaticExpectedUniverse([
        {"code": "600000", "list_date": "2026-08-10"},
    ])

    assert [item.code for item in universe.active(monday_in_shanghai)] == ["600000"]


def test_off_session_snapshot_rejects_an_old_completed_session_instead_of_relabeling_it_current():
    boundary = datetime(2026, 8, 6, 20, 0, tzinfo=timezone.utc)  # Shanghai Friday pre-open; Thursday is latest.
    provider = PagedWholeMarketIntradayProvider(
        StaticExpectedUniverse(["600000"]),
        lambda _page_number, _page_size: _page(
            observed_at=boundary,
            rows=(_row(session_date="2026-08-05"),),
        ),
    )

    with pytest.raises(ProductUnavailable, match="snapshot session is invalid"):
        provider.fetch(_request(boundary))


def test_after_close_snapshot_accepts_the_same_completed_session():
    boundary = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)  # 16:00 Shanghai, Wednesday close complete.
    provider = PagedWholeMarketIntradayProvider(
        StaticExpectedUniverse(["600000"]),
        lambda _page_number, _page_size: _page(
            observed_at=boundary,
            rows=(_row(session_date="2026-08-06"),),
        ),
    )

    assert provider.fetch(_request(boundary)).payload["rows"][0]["session_date"] == "2026-08-06"


def test_holiday_boundary_uses_the_authority_proved_latest_completed_session(tmp_path: Path):
    database = tmp_path / "advisor.sqlite"
    repository = ResearchRepository.open(database)
    with repository.transaction():
        for index, trade_date in enumerate(("2026-09-30", "2026-10-09")):
            repository.connection.execute(
                """
                INSERT INTO trading_sessions(
                    trade_date, primary_source, fallback_source, primary_observed_at,
                    fallback_observed_at, fetched_at, content_hash
                ) VALUES (?, 'fixture-primary', 'fixture-fallback', ?, ?, ?, ?)
                """,
                (trade_date, BOUNDARY.isoformat(), BOUNDARY.isoformat(), BOUNDARY.isoformat(), f"{index + 1:064x}"),
            )
    authority = SqliteTradingSessionAuthority(database)
    boundary = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)  # 16:00 Shanghai; fixture proves this weekday is closed.
    provider = PagedWholeMarketIntradayProvider(
        StaticExpectedUniverse(["600000"]),
        lambda _page_number, _page_size: _page(
            observed_at=boundary,
            rows=(_row(session_date="2026-09-30"),),
        ),
        session_authority=authority,
    )

    assert authority.session_state(boundary) == "closed"
    assert authority.latest_completed_session(boundary) == date(2026, 9, 30)
    assert provider.fetch(_request(boundary)).payload["rows"][0]["session_date"] == "2026-09-30"


def test_current_day_benchmark_authority_allows_a_live_default_bulk_snapshot_before_session_close(tmp_path: Path, monkeypatch):
    database = tmp_path / "advisor.sqlite"
    repository = ResearchRepository.open(database)
    with repository.transaction():
        repository.connection.execute(
            """
            INSERT INTO trading_sessions(
                trade_date, primary_source, fallback_source, primary_observed_at,
                fallback_observed_at, fetched_at, content_hash
            ) VALUES ('2026-08-05', 'fixture-primary', 'fixture-fallback', ?, ?, ?, ?)
            """,
            (BOUNDARY.isoformat(), BOUNDARY.isoformat(), BOUNDARY.isoformat(), "c" * 64),
        )
    authority = HybridTradingSessionAuthority(
        SqliteTradingSessionAuthority(database),
        _CurrentSessionAuthority(),
    )
    provider = SinaWholeMarketIntradayProvider(
        StaticExpectedUniverse(["600000"]),
        page_size=1,
        max_pages=3,
        session_authority=authority,
        minimum_interval_seconds=0,
    )
    pages = {1: [_row()], 2: []}
    monkeypatch.setattr(provider, "_get_json", lambda _endpoint, params: (pages[int(params["page"])], BOUNDARY))

    observation = provider.fetch(_request())

    assert authority.session_state(BOUNDARY) == "open"
    assert observation.payload["rows"][0]["session_date"] == "2026-08-06"


def test_current_day_benchmark_authority_proves_completed_session_after_close_before_persistence(tmp_path: Path, monkeypatch):
    database = tmp_path / "advisor.sqlite"
    repository = ResearchRepository.open(database)
    with repository.transaction():
        repository.connection.execute(
            """
            INSERT INTO trading_sessions(
                trade_date, primary_source, fallback_source, primary_observed_at,
                fallback_observed_at, fetched_at, content_hash
            ) VALUES ('2026-08-05', 'fixture-primary', 'fixture-fallback', ?, ?, ?, ?)
            """,
            (BOUNDARY.isoformat(), BOUNDARY.isoformat(), BOUNDARY.isoformat(), "d" * 64),
        )
    authority = HybridTradingSessionAuthority(
        SqliteTradingSessionAuthority(database),
        _CurrentSessionAuthority("completed"),
    )
    boundary = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)  # 16:00 Shanghai; 21:00 persistence has not run.
    provider = SinaWholeMarketIntradayProvider(
        StaticExpectedUniverse(["600000"]),
        page_size=1,
        max_pages=3,
        session_authority=authority,
        minimum_interval_seconds=0,
    )
    pages = {1: [_row()], 2: []}
    monkeypatch.setattr(provider, "_get_json", lambda _endpoint, params: (pages[int(params["page"])], boundary))

    observation = provider.fetch(_request(boundary))

    assert authority.session_state(boundary) == "completed"
    assert authority.latest_completed_session(boundary) == date(2026, 8, 6)
    assert observation.payload["rows"][0]["session_date"] == "2026-08-06"


def test_current_session_probe_requires_a_15_00_closing_timestamp_before_proving_completion(monkeypatch):
    boundary = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)  # 16:00 Shanghai.
    early = SinaBenchmarkCurrentSessionAuthority(cache_seconds=0)
    monkeypatch.setattr(early, "_benchmark_timestamp", lambda _symbol: datetime(2026, 8, 6, 6, 55, tzinfo=timezone.utc))
    complete = SinaBenchmarkCurrentSessionAuthority(cache_seconds=0)
    monkeypatch.setattr(complete, "_benchmark_timestamp", lambda _symbol: datetime(2026, 8, 6, 7, 0, tzinfo=timezone.utc))

    assert early.current_session_state(boundary) == "unknown"
    assert complete.current_session_state(boundary) == "completed"


@pytest.mark.parametrize(
    "boundary",
    [
        datetime(2026, 8, 6, 4, 0, tzinfo=timezone.utc),  # Shanghai noon break.
        datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc),  # after close with no proof.
    ],
)
def test_default_bulk_adapter_never_relabels_bare_quotes_when_current_session_is_unproved(tmp_path: Path, monkeypatch, boundary: datetime):
    database = tmp_path / "advisor.sqlite"
    repository = ResearchRepository.open(database)
    with repository.transaction():
        repository.connection.execute(
            """
            INSERT INTO trading_sessions(
                trade_date, primary_source, fallback_source, primary_observed_at,
                fallback_observed_at, fetched_at, content_hash
            ) VALUES ('2026-08-05', 'fixture-primary', 'fixture-fallback', ?, ?, ?, ?)
            """,
            (BOUNDARY.isoformat(), BOUNDARY.isoformat(), BOUNDARY.isoformat(), "e" * 64),
        )
    authority = HybridTradingSessionAuthority(
        SqliteTradingSessionAuthority(database),
        _CurrentSessionAuthority("unknown"),
    )
    provider = SinaWholeMarketIntradayProvider(
        StaticExpectedUniverse(["600000"]),
        page_size=1,
        max_pages=3,
        session_authority=authority,
        minimum_interval_seconds=0,
    )
    pages = {1: [_row()], 2: []}
    monkeypatch.setattr(provider, "_get_json", lambda _endpoint, params: (pages[int(params["page"])], boundary))

    with pytest.raises(ProductUnavailable, match="completed session proof"):
        provider.fetch(_request(boundary))


def test_default_bulk_adapter_attaches_authority_backed_completed_session_date(monkeypatch):
    boundary = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)  # 16:00 Shanghai, after close.
    provider = SinaWholeMarketIntradayProvider(
        StaticExpectedUniverse(["600000"]),
        session_authority=_SessionAuthority(state="closed", completed=date(2026, 8, 6)),
        minimum_interval_seconds=0,
    )
    monkeypatch.setattr(provider, "_get_json", lambda _endpoint, _params: ([_row()], boundary))

    page = provider._fetch_page(1, 200)

    assert page.rows[0]["session_date"] == "2026-08-06"


def test_coordinator_falls_back_for_an_unexpected_primary_transport_failure():
    primary = PagedWholeMarketIntradayProvider(
        StaticExpectedUniverse(["600000"]),
        lambda _page_number, _page_size: (_ for _ in ()).throw(RuntimeError("transport")),
    )
    fallback = PagedWholeMarketIntradayProvider(
        StaticExpectedUniverse(["600000"]),
        lambda _page_number, _page_size: _page(observed_at=BOUNDARY),
    )

    result = WholeMarketIntradayProvider(primary, fallback).fetch(_request())

    assert result.provider == fallback.provider_id


def test_repository_owned_bulk_adapter_retries_a_transient_page_failure_with_a_bounded_count():
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [_row()]

    class Session:
        def __init__(self):
            self.calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise requests.ConnectionError("fixture")
            return Response()

    session = Session()
    provider = SinaWholeMarketIntradayProvider(
        StaticExpectedUniverse(["600000"]),
        session=session,  # type: ignore[arg-type]
        max_attempts=2,
        minimum_interval_seconds=0,
    )

    page = provider._fetch_page(1, 200)

    assert session.calls == 2
    assert page.page == 1 and page.total_pages == 0


def test_default_sina_bulk_adapters_share_one_process_limiter():
    first = SinaWholeMarketIntradayProvider(StaticExpectedUniverse(["600000"]))
    second = SinaWholeMarketIntradayProvider(StaticExpectedUniverse(["600001"]))
    authority = SinaBenchmarkCurrentSessionAuthority()

    assert first._request_limiter is second._request_limiter
    assert first._request_limiter is authority._request_limiter
    assert first._request_limiter is PROCESS_SINA_LIMITER


def test_sina_list_payload_paginates_to_a_short_page_and_normalizes_sina_quote_fields(monkeypatch):
    pages = {
        1: [{
            "symbol": "sh600000",
            "name": "样例股份",
            "trade": "10.00",
            "settlement": "9.80",
            "changepercent": "2.04",
            "volume": "1000",
            "amount": "10000",
        }],
        2: [],
    }
    requested_pages: list[int] = []
    provider = SinaWholeMarketIntradayProvider(
        StaticExpectedUniverse(["600000"]),
        page_size=1,
        max_pages=3,
        minimum_interval_seconds=0,
    )

    def fetch_json(_endpoint, params):
        page = int(params["page"])
        requested_pages.append(page)
        return pages[page], BOUNDARY

    monkeypatch.setattr(provider, "_get_json", fetch_json)
    observation = provider.fetch(_request())

    assert requested_pages == [1, 2]
    assert observation.payload["page_proof"]["page_count"] == 2
    assert observation.payload["rows"] == [{
        "code": "600000", "name": "样例股份", "price": 10.0,
        "previous_close": 9.8, "change_pct": 2.04, "volume": 1000,
        "amount": 10000.0,
    }]


def test_sina_bulk_snapshot_skips_bse_rows_and_uses_the_source_100_row_page_cap(monkeypatch):
    bse_rows = [
        _row(f"920{index:03d}", name=f"北交所样例{index}")
        for index in range(100)
    ]
    pages = {1: bse_rows, 2: [_row("600000")]}
    requested: list[tuple[int, int]] = []
    provider = SinaWholeMarketIntradayProvider(
        StaticExpectedUniverse(["600000"]),
        max_pages=3,
        minimum_interval_seconds=0,
    )

    def fetch_json(_endpoint, params):
        page = int(params["page"])
        requested.append((page, int(params["num"])))
        return pages[page], BOUNDARY

    monkeypatch.setattr(provider, "_get_json", fetch_json)

    observation = provider.fetch(_request())

    assert requested == [(1, 100), (2, 100)]
    assert [row["code"] for row in observation.payload["rows"]] == ["600000"]
