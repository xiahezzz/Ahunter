from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import requests

from advisor.market_daily.providers.eastmoney import EastmoneyDailyBarProvider, parse_eastmoney_daily_bars
from advisor.market_daily.providers.contracts import MarketProviderError


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI)


def line(day: date, *, volume_lots: int = 1_000, amount: float = 1_005_000.0) -> str:
    return f"{day.isoformat()},10,10.05,10.1,9.9,{volume_lots},{amount},2,0.5,0.05,1"


def payload(rows: list[str], *, code: str = "600519") -> dict[str, object]:
    return {"rc": 0, "data": {"code": code, "klines": rows}}


def test_normalizes_lots_to_integer_shares_and_hash_is_stable():
    first = parse_eastmoney_daily_bars(
        payload([line(date(2026, 8, 7))]),
        code="600519",
        start=date(2026, 8, 7),
        end=date(2026, 8, 7),
        fetched_at=NOW,
    )
    second = parse_eastmoney_daily_bars(
        payload([line(date(2026, 8, 7))]),
        code="600519",
        start=date(2026, 8, 7),
        end=date(2026, 8, 7),
        fetched_at=NOW,
    )

    assert first[0].volume == 100_000
    assert first[0].amount == 1_005_000.0
    assert first[0].content_hash == second[0].content_hash


def test_more_than_800_daily_rows_are_supported_with_correct_boundaries():
    start = date(2021, 8, 9)
    rows = [line(start + timedelta(days=offset)) for offset in range(1_201)]
    # The adapter's contract is date-agnostic: sessions are established by
    # MD-006, so a fixture may use consecutive dates for size coverage.
    bars = parse_eastmoney_daily_bars(
        payload(rows, code="000001"),
        code="000001",
        start=start,
        end=start + timedelta(days=1_200),
        fetched_at=datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI),
    )

    assert len(bars) == 1_201
    assert bars[0].trade_date == start
    assert bars[-1].trade_date == start + timedelta(days=1_200)


@pytest.mark.parametrize(
    "rows",
    [
        [line(date(2026, 8, 7)), line(date(2026, 8, 7))],
        [line(date(2026, 8, 7)), line(date(2026, 8, 6))],
        ["2026-08-07,10,10,9,10.1,100,100000,1"],
        ["2026-08-07,10,10.1,10.2,9.9,100,0,1"],
    ],
)
def test_rejects_duplicate_unsorted_invalid_or_fabricated_amount(rows):
    with pytest.raises(MarketProviderError):
        parse_eastmoney_daily_bars(
            payload(rows),
            code="600519",
            start=date(2026, 8, 6),
            end=date(2026, 8, 7),
            fetched_at=NOW,
        )


def test_rejects_wrong_code_and_out_of_range_or_future_rows():
    with pytest.raises(MarketProviderError):
        parse_eastmoney_daily_bars(
            payload([line(date(2026, 8, 7))], code="000001"),
            code="600519",
            start=date(2026, 8, 7),
            end=date(2026, 8, 7),
            fetched_at=NOW,
        )
    with pytest.raises(MarketProviderError):
        parse_eastmoney_daily_bars(
            payload([line(date(2026, 8, 8))]),
            code="600519",
            start=date(2026, 8, 7),
            end=date(2026, 8, 7),
            fetched_at=NOW,
        )


class _Response:
    def __init__(self, body: object) -> None:
        self.body = body
        self.headers = {"Content-Type": "application/json; charset=utf-8"}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.body


class _Session:
    def __init__(self) -> None:
        self.params: dict[str, object] | None = None

    def get(self, _url: str, **kwargs: object) -> _Response:
        self.params = kwargs["params"]  # type: ignore[assignment]
        return _Response(payload([line(date(2026, 8, 7))]))


def test_http_adapter_requests_unadjusted_daily_bars_with_bounded_configuration():
    session = _Session()
    provider = EastmoneyDailyBarProvider(session=session, clock=lambda: NOW, max_concurrency=3)

    bars = provider.fetch_daily_bars("600519", date(2026, 8, 7), date(2026, 8, 7))

    assert len(bars) == 1
    assert session.params is not None
    assert session.params["secid"] == "1.600519"
    assert session.params["fqt"] == "0"
    assert session.params["klt"] == "101"


def test_http_adapter_reuses_fallback_after_primary_route_fails():
    class FailingRoute:
        def __init__(self):
            self.calls = 0

        def get(self, _url, **_kwargs):
            self.calls += 1
            raise requests.ConnectionError("unreachable CDN node")

    class WorkingRoute:
        def __init__(self):
            self.calls = 0

        def get(self, _url, **_kwargs):
            self.calls += 1
            return _Response(payload([line(date(2026, 8, 7))]))

    primary = FailingRoute()
    fallback = WorkingRoute()
    provider = EastmoneyDailyBarProvider(
        session=primary,
        fallback_sessions=(fallback,),
        clock=lambda: NOW,
    )

    provider.fetch_daily_bars("600519", date(2026, 8, 7), date(2026, 8, 7))
    provider.fetch_daily_bars("600519", date(2026, 8, 7), date(2026, 8, 7))

    assert primary.calls == 1
    assert fallback.calls == 2


def test_default_http_adapter_builds_verified_eastmoney_cdn_fallbacks(monkeypatch):
    from advisor.market_daily.providers import eastmoney

    class FailingRoute:
        def get(self, _url, **_kwargs):
            raise requests.ConnectionError("unreachable local CDN node")

    class WorkingRoute:
        def get(self, _url, **_kwargs):
            return _Response(payload([line(date(2026, 8, 7))]))

    pinned: list[tuple[str, str]] = []
    working = WorkingRoute()
    monkeypatch.setattr(eastmoney.requests, "Session", lambda: FailingRoute())
    monkeypatch.setattr(
        eastmoney,
        "pinned_https_session",
        lambda hostname, address: pinned.append((hostname, address)) or working,
        raising=False,
    )

    bars = EastmoneyDailyBarProvider(clock=lambda: NOW).fetch_daily_bars(
        "600519", date(2026, 8, 7), date(2026, 8, 7)
    )

    assert len(bars) == 1
    assert pinned == [
        ("push2his.eastmoney.com", "117.184.45.167"),
        ("push2his.eastmoney.com", "61.129.129.48"),
        ("push2his.eastmoney.com", "101.226.30.136"),
    ]
