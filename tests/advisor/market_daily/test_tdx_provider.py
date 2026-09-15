from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from advisor.market_daily.providers.contracts import MarketProviderError
from advisor.market_daily.providers.tdx import TdxDailyBarProvider, TdxServer


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI)


def row(day: str, *, amount: float = 1_005_000.0) -> dict[str, object]:
    return {
        "datetime": f"{day} 15:00",
        "open": 10.0,
        "close": 10.05,
        "high": 10.1,
        "low": 9.9,
        "vol": 100_000.0,
        "amount": amount,
    }


class FakeTdxClient:
    def __init__(self, pages: dict[int, list[dict[str, object]]], *, connects: object = True) -> None:
        self.pages = pages
        self.connects = connects
        self.calls: list[tuple[int, int, str, int, int]] = []
        self.disconnected = False

    def connect(self, _host: str, _port: int, time_out: float = 5.0) -> object:
        assert time_out == 2.0
        return self.connects

    def disconnect(self) -> None:
        self.disconnected = True

    def get_security_bars(self, category: int, market: int, code: str, start: int, count: int):
        self.calls.append((category, market, code, start, count))
        return self.pages.get(start, [])


def provider(client: FakeTdxClient) -> TdxDailyBarProvider:
    return TdxDailyBarProvider(
        client_factory=lambda: client,
        clock=lambda: NOW,
        servers=(TdxServer("example.test", 7709),),
        timeout_seconds=2.0,
        page_size=2,
        max_pages=3,
    )


def test_pages_direct_tdx_bars_and_normalizes_same_contract_as_primary():
    client = FakeTdxClient(
        {
            0: [row("2026-08-06"), row("2026-08-07")],
            2: [row("2026-08-04"), row("2026-08-05")],
        }
    )

    bars = provider(client).fetch_daily_bars("600519", date(2026, 8, 5), date(2026, 8, 7))

    assert [bar.trade_date.isoformat() for bar in bars] == ["2026-08-05", "2026-08-06", "2026-08-07"]
    assert bars[0].volume == 100_000
    assert bars[0].amount == 1_005_000.0
    assert client.calls == [(9, 1, "600519", 0, 2), (9, 1, "600519", 2, 2)]
    assert client.disconnected is True


def test_accepts_the_truthy_client_object_returned_by_tdxpy_connect():
    client = FakeTdxClient({0: [row("2026-08-07")]})
    client.connects = client

    bars = provider(client).fetch_daily_bars("600519", date(2026, 8, 7), date(2026, 8, 7))

    assert [bar.trade_date.isoformat() for bar in bars] == ["2026-08-07"]
    assert client.disconnected is True


def test_rejects_duplicate_page_boundary_and_missing_turnover():
    duplicated = FakeTdxClient(
        {
            0: [row("2026-08-06"), row("2026-08-07")],
            2: [row("2026-08-05"), row("2026-08-06")],
        }
    )
    with pytest.raises(MarketProviderError):
        provider(duplicated).fetch_daily_bars("600519", date(2026, 8, 5), date(2026, 8, 7))

    incomplete = FakeTdxClient({0: [row("2026-08-07", amount=0.0)]})
    with pytest.raises(MarketProviderError):
        provider(incomplete).fetch_daily_bars("600519", date(2026, 8, 7), date(2026, 8, 7))


def test_connection_failure_is_finite_and_does_not_leak_server_details():
    client = FakeTdxClient({}, connects=False)
    with pytest.raises(MarketProviderError, match="通达信日线连接或读取失败"):
        provider(client).fetch_daily_bars("000001", date(2026, 8, 7), date(2026, 8, 7))
