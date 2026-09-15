from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from advisor.market_daily.contracts import CanonicalDailyBar
from advisor.market_daily.providers.adjustment_chain import AdjustmentFactorProviderChain
from advisor.market_daily.providers.contracts import MarketProviderError
from advisor.market_daily.providers.tdx import TdxServer
from advisor.market_daily.providers.tdx_adjustments import TdxAdjustmentFactorProvider, TdxCorporateActionAdapter


NOW = datetime(2026, 8, 7, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _bar(day: date, close: float) -> CanonicalDailyBar:
    return CanonicalDailyBar(
        code="600519",
        trade_date=day,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=100,
        amount=close * 100,
        source="fixture",
        source_at=NOW,
        fetched_at=NOW,
        as_of_date=day,
    )


class _Bars:
    def __init__(self, rows: tuple[CanonicalDailyBar, ...]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, date, date]] = []

    def fetch_daily_bars(self, code: str, start: date, end: date) -> tuple[CanonicalDailyBar, ...]:
        self.calls.append((code, start, end))
        return self.rows


class _Actions:
    def __init__(self, rows: tuple[dict[str, object], ...]) -> None:
        self.rows = rows
        self.calls: list[tuple[int, str]] = []

    def fetch_corporate_actions(self, market: int, code: str) -> tuple[dict[str, object], ...]:
        self.calls.append((market, code))
        return self.rows


def _action(day: date, **values: object) -> dict[str, object]:
    return {
        "year": day.year,
        "month": day.month,
        "day": day.day,
        "category": 1,
        "fenhong": 0.0,
        "songzhuangu": 0.0,
        "peigu": 0.0,
        "peigujia": 0.0,
        **values,
    }


def test_derives_a_complete_tdx_factor_series_from_cash_dividend():
    start = date(2026, 8, 5)
    end = date(2026, 8, 7)
    bars = _Bars((_bar(start, 10.0), _bar(date(2026, 8, 6), 10.0), _bar(end, 9.0)))
    actions = _Actions((_action(end, fenhong=10.0),))
    provider = TdxAdjustmentFactorProvider(bars=bars, actions=actions, clock=lambda: NOW)

    factors = provider.fetch_adjustment_factors("600519", start, end)

    assert [factor.factor for factor in factors] == [0.9, 0.9, 1.0]
    assert all(factor.source == "tdx_adjustment" for factor in factors)
    assert actions.calls == [(1, "600519")]
    assert bars.calls == [("600519", date(2025, 8, 4), end)]


def test_combines_bonus_and_rights_before_deriving_the_factor():
    start = date(2026, 8, 5)
    end = date(2026, 8, 7)
    bars = _Bars((_bar(start, 10.0), _bar(date(2026, 8, 6), 10.0), _bar(end, 5.0)))
    actions = _Actions((_action(end, songzhuangu=10.0, peigu=10.0, peigujia=5.0),))
    provider = TdxAdjustmentFactorProvider(bars=bars, actions=actions, clock=lambda: NOW)

    factors = provider.fetch_adjustment_factors("600519", start, end)

    assert [factor.factor for factor in factors] == [0.5, 0.5, 1.0]


def test_fails_closed_for_unknown_price_affecting_tdx_event():
    start = date(2026, 8, 5)
    end = date(2026, 8, 7)
    bars = _Bars((_bar(start, 10.0), _bar(date(2026, 8, 6), 10.0), _bar(end, 9.0)))
    actions = _Actions((_action(end, category=11, suogu=2.0),))
    provider = TdxAdjustmentFactorProvider(bars=bars, actions=actions, clock=lambda: NOW)

    with pytest.raises(MarketProviderError, match="扩缩股"):
        provider.fetch_adjustment_factors("600519", start, end)


class _FailingFactors:
    source = "eastmoney_adjustment"

    def __init__(self) -> None:
        self.calls = 0

    def fetch_adjustment_factors(self, _code: str, _start: date, _end: date):
        self.calls += 1
        raise MarketProviderError("primary unavailable")


class _FallbackFactors:
    source = "tdx_adjustment"

    def __init__(self, factors) -> None:
        self.factors = factors
        self.calls = 0

    def fetch_adjustment_factors(self, _code: str, _start: date, _end: date):
        self.calls += 1
        return self.factors


def test_chain_uses_tdx_once_only_after_the_primary_provider_fails():
    start = date(2026, 8, 5)
    end = date(2026, 8, 7)
    tdx = TdxAdjustmentFactorProvider(
        bars=_Bars((_bar(start, 10.0), _bar(date(2026, 8, 6), 10.0), _bar(end, 9.0))),
        actions=_Actions((_action(end, fenhong=10.0),)),
        clock=lambda: NOW,
    )
    primary = _FailingFactors()
    fallback = _FallbackFactors(tdx.fetch_adjustment_factors("600519", start, end))
    chain = AdjustmentFactorProviderChain(primary, fallback)

    factors = chain.fetch_adjustment_factors("600519", start, end)

    assert primary.calls == 1
    assert fallback.calls == 1
    assert [factor.source for factor in factors] == ["tdx_adjustment", "tdx_adjustment", "tdx_adjustment"]


class _TdxActionsClient:
    def __init__(self, rows: object, *, connected: object = True) -> None:
        self.rows = rows
        self.connected = connected
        self.disconnected = 0
        self.calls: list[tuple[int, str]] = []

    def connect(self, _host: str, _port: int, time_out: float = 5.0) -> object:
        assert time_out == 8.0
        return self.connected

    def disconnect(self) -> None:
        self.disconnected += 1

    def get_xdxr_info(self, market: int, code: str) -> object:
        self.calls.append((market, code))
        return self.rows


def test_tdx_corporate_action_adapter_returns_one_complete_mapping_response():
    rows = (_action(date(2026, 8, 7), fenhong=10.0),)
    client = _TdxActionsClient(list(rows))
    adapter = TdxCorporateActionAdapter(
        client_factory=lambda: client,
        servers=(TdxServer("fixture.tdx"),),
    )

    result = adapter.fetch_corporate_actions(1, "600519")

    assert result == rows
    assert client.calls == [(1, "600519")]
    assert client.disconnected == 1


def test_tdx_corporate_action_adapter_rejects_a_non_mapping_response():
    client = _TdxActionsClient(["bad-row"])
    adapter = TdxCorporateActionAdapter(
        client_factory=lambda: client,
        servers=(TdxServer("fixture.tdx"),),
    )

    with pytest.raises(MarketProviderError, match="公司行动连接或读取失败"):
        adapter.fetch_corporate_actions(1, "600519")

    assert client.disconnected == 1
