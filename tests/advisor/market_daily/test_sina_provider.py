from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import requests

from advisor.market_daily.providers.contracts import MarketProviderError
from advisor.market_daily.providers.sina import (
    SinaDailyBarProvider,
    SinaRateLimiter,
    SinaUniverseAdapter,
    parse_sina_daily_bars,
    parse_sina_qfq_values,
)
from advisor.market_daily.providers.sessions import SinaIndexSessionProvider
from advisor.market_daily.providers.registry import SingleProvider


NOW = datetime(2026, 8, 7, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _row(day: date, *, close: str = "10.05", volume: str = "100000") -> dict[str, str]:
    return {
        "day": day.isoformat(),
        "open": "10.00",
        "high": "10.10",
        "low": "9.90",
        "close": close,
        "volume": volume,
    }


def test_parser_accepts_more_than_800_rows_and_keeps_missing_amount_explicit():
    start = date(2021, 8, 9)
    payload = [_row(start + timedelta(days=offset)) for offset in range(1_201)]

    bars = parse_sina_daily_bars(
        payload,
        code="600519",
        start=start,
        end=start + timedelta(days=1_200),
        fetched_at=NOW,
    )

    assert len(bars) == 1_201
    assert bars[0].trade_date == start
    assert bars[-1].trade_date == start + timedelta(days=1_200)
    assert bars[0].volume == 100_000
    assert bars[0].amount is None
    assert {bar.source for bar in bars} == {"sina"}


@pytest.mark.parametrize(
    "payload",
    [
        [_row(date(2026, 8, 7)), _row(date(2026, 8, 7))],
        [_row(date(2026, 8, 7)), _row(date(2026, 8, 6))],
        [_row(date(2026, 8, 7), volume="1.5")],
        [{**_row(date(2026, 8, 7)), "high": "9.00"}],
        [{**_row(date(2026, 8, 7)), "amount": "1000"}],
    ],
)
def test_parser_rejects_noncanonical_or_unexpected_rows(payload):
    with pytest.raises(MarketProviderError):
        parse_sina_daily_bars(
            payload,
            code="600519",
            start=date(2026, 8, 6),
            end=date(2026, 8, 7),
            fetched_at=NOW,
        )


def test_qfq_parser_requires_the_requested_sina_variable_and_complete_count():
    body = (
        'var sh600519qfq=[{total:2,data:{_2026_08_07:"8.000000",'
        '_2026_08_06:"2.000000"}}]\n/* official response comment */'
    )

    assert parse_sina_qfq_values(body, symbol="sh600519") == {
        date(2026, 8, 6): 2.0,
        date(2026, 8, 7): 8.0,
    }
    with pytest.raises(MarketProviderError):
        parse_sina_qfq_values(body.replace("total:2", "total:3"), symbol="sh600519")
    with pytest.raises(MarketProviderError):
        parse_sina_qfq_values(body, symbol="sz600519")


class _NoopLimiter:
    def __init__(self) -> None:
        self.calls = 0

    def acquire(self) -> None:
        self.calls += 1


class _Response:
    def __init__(
        self,
        *,
        payload: object | None = None,
        text: str = "",
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.text = text
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "application/json; charset=gbk"}

    def json(self) -> object:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class _Session:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append((url, kwargs))
        if url.endswith("/pqfq.js"):
            return _Response(
                text=(
                    'var sh600519qfq=[{total:2,data:{_2026_08_07:"4.000000",'
                    '_2026_08_06:"2.000000"}}]'
                ),
                headers={"Content-Type": "application/x-javascript"},
            )
        return _Response(
            payload={
                "result": {
                    "status": {"code": 0},
                    "data": [
                        _row(date(2026, 8, 6), close="10.00"),
                        _row(date(2026, 8, 7), close="10.00"),
                    ],
                }
            }
        )


def test_provider_uses_only_sina_endpoints_and_reuses_raw_rows_for_factors():
    session = _Session()
    limiter = _NoopLimiter()
    provider = SinaDailyBarProvider(session=session, limiter=limiter, clock=lambda: NOW)

    bars = provider.fetch_daily_bars("600519", date(2026, 8, 6), date(2026, 8, 7))
    factors = provider.fetch_adjustment_factors(
        "600519", date(2026, 8, 6), date(2026, 8, 7)
    )

    assert len(session.calls) == 2
    assert session.calls[0][0].startswith(
        "https://quotes.sina.cn/cn/api/openapi.php/CN_MarketDataService.getKLineData"
    )
    assert session.calls[0][1]["params"] == {
        "symbol": "sh600519",
        "scale": "240",
        "ma": "no",
        "datalen": "10",
    }
    assert session.calls[1][0].endswith("/sh600519/pqfq.js")
    assert limiter.calls == 2
    assert [factor.factor for factor in factors] == [0.5, 1.0]
    assert {factor.source for factor in factors} == {"sina_adjustment"}
    assert [bar.trade_date for bar in bars] == [date(2026, 8, 6), date(2026, 8, 7)]


def test_rate_limiter_enforces_a_shared_two_requests_per_second_ceiling():
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    limiter = SinaRateLimiter(requests_per_second=2.0, monotonic=lambda: now[0], sleep=sleep)

    limiter.acquire()
    limiter.acquire()
    limiter.acquire()

    assert sleeps == [0.5, 0.5]


def test_provider_honors_retry_after_for_429_then_succeeds_within_bound():
    class RateLimitedSession:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, _url: str, **_kwargs: object) -> _Response:
            self.calls += 1
            if self.calls == 1:
                return _Response(status_code=429, headers={"Retry-After": "3"})
            return _Response(payload={"result": {"status": {"code": 0}, "data": [_row(date(2026, 8, 7))]}})

    session = RateLimitedSession()
    limiter = _NoopLimiter()
    sleeps: list[float] = []
    provider = SinaDailyBarProvider(
        session=session,
        limiter=limiter,
        clock=lambda: NOW,
        sleep=sleeps.append,
        max_attempts=2,
    )

    bars = provider.fetch_daily_bars("600519", date(2026, 8, 7), date(2026, 8, 7))

    assert len(bars) == 1
    assert session.calls == 2
    assert limiter.calls == 2
    assert sleeps == [3.0]


def test_session_providers_use_two_sina_benchmarks_through_the_same_adapter():
    class IndexBars:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, date, date]] = []

        def fetch_index_daily_bars(self, code, market, start, end):
            self.calls.append((code, market, start, end))
            returned = "000001" if market == "SH" else "399001"
            return parse_sina_daily_bars(
                [_row(date(2026, 8, 6)), _row(date(2026, 8, 7))],
                code=returned,
                start=start,
                end=end,
                fetched_at=NOW,
            )

    bars = IndexBars()
    sh = SinaIndexSessionProvider(bars, market="SH")
    sz = SinaIndexSessionProvider(bars, market="SZ")

    assert sh.source == "sina_sh_index"
    assert sz.source == "sina_sz_index"
    assert sh.observe_sessions(date(2026, 8, 6), date(2026, 8, 7)) == (
        date(2026, 8, 6),
        date(2026, 8, 7),
    )
    assert sz.observe_sessions(date(2026, 8, 6), date(2026, 8, 7)) == (
        date(2026, 8, 6),
        date(2026, 8, 7),
    )
    assert bars.calls == [
        ("000001", "SH", date(2026, 8, 6), date(2026, 8, 7)),
        ("399001", "SZ", date(2026, 8, 6), date(2026, 8, 7)),
    ]


def test_sina_universe_uses_only_sh_sz_nodes_filters_bse_and_reuses_known_listing_dates():
    class MarketData:
        def __init__(self) -> None:
            self.nodes: list[str] = []
            self.listing_calls: list[str] = []

        def fetch_market_node(self, node: str):
            self.nodes.append(node)
            if node == "sh_a":
                return (
                    {"symbol": "sh600000", "code": "600000", "name": "浦发银行"},
                    {"symbol": "sh689009", "code": "689009", "name": "九号公司-WD"},
                    {"symbol": "bj920001", "code": "920001", "name": "北交所"},
                )
            return (
                {"symbol": "sz000001", "code": "000001", "name": "平安银行"},
                {"symbol": "sz300001", "code": "300001", "name": "特锐德"},
            )

        def fetch_listing_date(self, symbol: str) -> date:
            self.listing_calls.append(symbol)
            return {"sz000001": date(1991, 4, 3), "sz300001": date(2009, 10, 30)}[symbol]

    market = MarketData()
    progress: list[str] = []
    adapter = SinaUniverseAdapter(
        market,
        known_listing_dates=lambda: {"600000": date(1999, 11, 10)},
        clock=lambda: NOW,
        progress=lambda: progress.append("tick"),
    )

    records = adapter.fetch()

    assert market.nodes == ["sh_a", "sz_a"]
    assert [record.code for record in records] == ["000001", "300001", "600000"]
    assert market.listing_calls == ["sz000001", "sz300001"]
    assert [record.list_date for record in records] == [
        date(1991, 4, 3),
        date(2009, 10, 30),
        date(1999, 11, 10),
    ]
    assert {record.source for record in records} == {"sina_universe"}
    assert len(progress) == 4


def test_sina_universe_probe_does_not_expand_listing_metadata():
    class MarketData:
        def __init__(self) -> None:
            self.listing_calls = 0

        def fetch_market_node_count(self, node: str) -> int:
            return 2_310 if node == "sh_a" else 2_895

        def fetch_listing_date(self, _symbol: str) -> date:
            self.listing_calls += 1
            raise AssertionError("probe must not fetch per-security metadata")

    market = MarketData()

    assert SinaUniverseAdapter(market, clock=lambda: NOW).probe() == {"SH": 2_310, "SZ": 2_895}
    assert market.listing_calls == 0


def test_market_node_uses_reported_count_and_fetches_every_100_row_page():
    class PaginatedSession:
        def __init__(self) -> None:
            self.pages: list[int] = []

        def get(self, url: str, **kwargs: object) -> _Response:
            if "getHQNodeStockCount" in url:
                return _Response(payload="101")
            params = kwargs["params"]
            page = int(params["page"])
            self.pages.append(page)
            size = 100 if page == 1 else 1
            rows = [
                {
                    "symbol": f"sh{600000 + (page - 1) * 100 + offset:06d}",
                    "code": f"{600000 + (page - 1) * 100 + offset:06d}",
                    "name": str(offset),
                }
                for offset in range(size)
            ]
            return _Response(payload=rows)

    session = PaginatedSession()
    provider = SinaDailyBarProvider(
        session=session,
        limiter=_NoopLimiter(),
        clock=lambda: NOW,
    )

    rows = provider.fetch_market_node("sh_a")

    assert len(rows) == 101
    assert session.pages == [1, 2]


def test_missing_sina_rows_become_explicit_suspension_evidence_without_another_request():
    class SparseSession:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, _url: str, **_kwargs: object) -> _Response:
            self.calls += 1
            return _Response(payload={"result": {"status": {"code": 0}, "data": [_row(date(2026, 8, 6))]}})

    session = SparseSession()
    provider = SinaDailyBarProvider(
        session=session,
        limiter=_NoopLimiter(),
        clock=lambda: NOW,
    )

    bars = provider.fetch_daily_bars("600519", date(2026, 8, 6), date(2026, 8, 7))
    absences = provider.absences_for("600519", (date(2026, 8, 7),), NOW)

    assert [bar.trade_date for bar in bars] == [date(2026, 8, 6)]
    assert [(item.trade_date, item.reason, item.source) for item in absences] == [
        (date(2026, 8, 7), "suspended", "sina_no_daily_bar")
    ]
    assert session.calls == 1


def test_zero_ohlc_sina_placeholder_becomes_explicit_suspension_evidence():
    """Sina sometimes includes a zero-volume suspension row instead of omitting it."""

    suspended = {
        "day": "2024-11-06",
        "open": "0.000",
        "high": "0.000",
        "low": "0.000",
        "close": "20.920",
        "volume": "0",
    }

    class PlaceholderSession:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, _url: str, **_kwargs: object) -> _Response:
            self.calls += 1
            return _Response(
                payload={
                    "result": {
                        "status": {"code": 0},
                        "data": [_row(date(2024, 11, 5)), suspended, _row(date(2024, 11, 7))],
                    }
                }
            )

    session = PlaceholderSession()
    provider = SinaDailyBarProvider(
        session=session,
        limiter=_NoopLimiter(),
        clock=lambda: NOW,
    )

    bars = provider.fetch_daily_bars("688089", date(2024, 11, 5), date(2024, 11, 7))
    absences = provider.absences_for("688089", (date(2024, 11, 6),), NOW)

    assert [bar.trade_date for bar in bars] == [date(2024, 11, 5), date(2024, 11, 7)]
    assert [(item.trade_date, item.reason, item.source) for item in absences] == [
        (date(2024, 11, 6), "suspended", "sina_no_daily_bar")
    ]
    assert session.calls == 1


def test_zero_price_row_with_volume_is_not_treated_as_a_suspension_placeholder():
    malformed = {
        "day": "2024-11-06",
        "open": "0.000",
        "high": "0.000",
        "low": "0.000",
        "close": "20.920",
        "volume": "1",
    }

    with pytest.raises(MarketProviderError, match="开盘价无效"):
        parse_sina_daily_bars(
            [malformed],
            code="688089",
            start=date(2024, 11, 6),
            end=date(2024, 11, 6),
            fetched_at=NOW,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("open", False),
        ("high", False),
        ("low", False),
        ("close", True),
        ("volume", False),
        ("volume", "0.0"),
        ("volume", "0.5"),
    ],
)
def test_suspension_placeholder_rejects_boolean_or_noninteger_contract_values(field, value):
    malformed = {
        "day": "2024-11-06",
        "open": "0.000",
        "high": "0.000",
        "low": "0.000",
        "close": "20.920",
        "volume": "0",
    }
    malformed[field] = value

    with pytest.raises(MarketProviderError):
        parse_sina_daily_bars(
            [malformed],
            code="688089",
            start=date(2024, 11, 6),
            end=date(2024, 11, 6),
            fetched_at=NOW,
        )


def test_a_valid_sina_response_can_evidence_a_fully_suspended_requested_window():
    class EarlierOnlySession:
        def get(self, _url: str, **_kwargs: object) -> _Response:
            return _Response(payload={"result": {"status": {"code": 0}, "data": [_row(date(2026, 8, 5))]}})

    provider = SinaDailyBarProvider(
        session=EarlierOnlySession(),
        limiter=_NoopLimiter(),
        clock=lambda: NOW,
    )

    result = SingleProvider(provider).fetch_daily_bars(
        "600519", date(2026, 8, 6), date(2026, 8, 7)
    )
    absences = provider.absences_for(
        "600519", (date(2026, 8, 6), date(2026, 8, 7)), NOW
    )

    assert result.bars == ()
    assert [item.trade_date for item in absences] == [date(2026, 8, 6), date(2026, 8, 7)]


def test_openapi_empty_data_can_evidence_a_fully_suspended_requested_window():
    class EmptySession:
        def get(self, _url: str, **_kwargs: object) -> _Response:
            return _Response(payload={"result": {"status": {"code": 0}, "data": []}})

    provider = SinaDailyBarProvider(
        session=EmptySession(),
        limiter=_NoopLimiter(),
        clock=lambda: NOW,
    )

    result = SingleProvider(provider).fetch_daily_bars(
        "600519", date(2026, 8, 6), date(2026, 8, 7)
    )
    absences = provider.absences_for(
        "600519", (date(2026, 8, 6), date(2026, 8, 7)), NOW
    )

    assert result.bars == ()
    assert [item.trade_date for item in absences] == [date(2026, 8, 6), date(2026, 8, 7)]


def test_daily_parser_uses_the_shanghai_date_at_a_utc_day_boundary():
    fetched_at = datetime(2026, 8, 9, 16, 30, tzinfo=ZoneInfo("UTC"))  # Monday 00:30 Shanghai.

    bars = parse_sina_daily_bars(
        [_row(date(2026, 8, 10))],
        code="600519",
        start=date(2026, 8, 10),
        end=date(2026, 8, 10),
        fetched_at=fetched_at,
    )

    assert [bar.trade_date for bar in bars] == [date(2026, 8, 10)]
