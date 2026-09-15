from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
import requests

from advisor.market_daily.providers.adjustments import EastmoneyAdjustmentFactorProvider
from advisor.market_daily.providers.contracts import MarketProviderError


NOW = datetime(2026, 8, 7, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _payload(*, close: float) -> dict[str, object]:
    return {
        "rc": 0,
        "data": {
            "code": "600519",
            "klines": ["2026-08-07,10,{close},10.1,9.9,1000,100000,1,0,0,1".format(close=close)],
        },
    }


class _Response:
    headers = {"Content-Type": "application/json; charset=utf-8"}

    def __init__(self, body: object) -> None:
        self.body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.body


class _FlakySession:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, _url: str, **kwargs: object) -> _Response:
        fqt = str(kwargs["params"]["fqt"])  # type: ignore[index]
        self.calls.append(fqt)
        if len(self.calls) == 1:
            raise requests.ConnectionError("temporary disconnect")
        return _Response(_payload(close=10.0 if fqt == "0" else 5.0))


class _FailingSession:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, _url: str, **_kwargs: object) -> _Response:
        self.calls += 1
        raise requests.ConnectionError("offline")


def test_retries_the_complete_factor_pair_once_after_a_transient_failure():
    session = _FlakySession()
    sleeps: list[float] = []
    provider = EastmoneyAdjustmentFactorProvider(session=session, clock=lambda: NOW, sleep=sleeps.append)

    factors = provider.fetch_adjustment_factors("600519", date(2026, 8, 7), date(2026, 8, 7))

    assert session.calls == ["0", "0", "1"]
    assert sleeps == [1.0]
    assert len(factors) == 1
    assert factors[0].factor == 1.0


def test_stops_after_two_complete_factor_attempts():
    session = _FailingSession()
    sleeps: list[float] = []
    provider = EastmoneyAdjustmentFactorProvider(session=session, clock=lambda: NOW, sleep=sleeps.append)

    with pytest.raises(MarketProviderError, match="复权来源在两次尝试后仍不可用"):
        provider.fetch_adjustment_factors("600519", date(2026, 8, 7), date(2026, 8, 7))

    assert session.calls == 2
    assert sleeps == [1.0]


def test_factor_adapter_reuses_fallback_after_primary_route_fails():
    class WorkingRoute:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get(self, _url: str, **kwargs: object) -> _Response:
            fqt = str(kwargs["params"]["fqt"])  # type: ignore[index]
            self.calls.append(fqt)
            return _Response(_payload(close=10.0 if fqt == "0" else 5.0))

    primary = _FailingSession()
    fallback = WorkingRoute()
    provider = EastmoneyAdjustmentFactorProvider(
        session=primary,
        fallback_sessions=(fallback,),
        clock=lambda: NOW,
        max_attempts=1,
    )

    factors = provider.fetch_adjustment_factors(
        "600519", date(2026, 8, 7), date(2026, 8, 7)
    )

    assert len(factors) == 1
    assert primary.calls == 1
    assert fallback.calls == ["0", "1"]


def test_default_factor_adapter_builds_verified_eastmoney_cdn_fallbacks(monkeypatch):
    from advisor.market_daily.providers import adjustments

    class WorkingRoute:
        def get(self, _url: str, **kwargs: object) -> _Response:
            fqt = str(kwargs["params"]["fqt"])  # type: ignore[index]
            return _Response(_payload(close=10.0 if fqt == "0" else 5.0))

    pinned: list[tuple[str, str]] = []
    working = WorkingRoute()
    monkeypatch.setattr(adjustments.requests, "Session", lambda: _FailingSession())
    monkeypatch.setattr(
        adjustments,
        "pinned_https_session",
        lambda hostname, address: pinned.append((hostname, address)) or working,
        raising=False,
    )

    factors = EastmoneyAdjustmentFactorProvider(
        clock=lambda: NOW,
        max_attempts=1,
    ).fetch_adjustment_factors("600519", date(2026, 8, 7), date(2026, 8, 7))

    assert len(factors) == 1
    assert pinned == [
        ("push2his.eastmoney.com", "117.184.45.167"),
        ("push2his.eastmoney.com", "61.129.129.48"),
        ("push2his.eastmoney.com", "101.226.30.136"),
    ]
