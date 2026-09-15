from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from advisor.market_daily.contracts import CanonicalDailyBar
from advisor.market_daily.providers.contracts import MarketProviderError
from advisor.market_daily.providers.registry import ProviderChain, SingleProvider


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI)


def bar(source: str) -> CanonicalDailyBar:
    return CanonicalDailyBar(
        code="600519",
        trade_date=date(2026, 8, 7),
        open=10.0,
        high=10.1,
        low=9.9,
        close=10.05,
        volume=100_000,
        amount=1_005_000.0,
        source=source,
        source_at=NOW,
        fetched_at=NOW,
    )


class FakeProvider:
    def __init__(self, source: str, outcomes: list[object]) -> None:
        self.source = source
        self.endpoint = source
        self.outcomes = outcomes
        self.calls = 0

    def fetch_daily_bars(self, _code: str, _start: date, _end: date):
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_successful_primary_never_calls_fallback():
    primary = FakeProvider("eastmoney", [(bar("eastmoney"),)])
    fallback = FakeProvider("tdx", [(bar("tdx"),)])

    result = ProviderChain(primary, fallback).fetch_daily_bars("600519", date(2026, 8, 7), date(2026, 8, 7))

    assert result.selected_source == "eastmoney"
    assert primary.calls == 1
    assert fallback.calls == 0


def test_primary_has_two_attempt_budget_then_uses_one_complete_fallback_response():
    primary = FakeProvider("eastmoney", [MarketProviderError("x"), MarketProviderError("x")])
    fallback = FakeProvider("tdx", [(bar("tdx"),)])
    sleeps: list[float] = []

    result = ProviderChain(primary, fallback, sleep=sleeps.append).fetch_daily_bars(
        "600519", date(2026, 8, 7), date(2026, 8, 7)
    )

    assert result.selected_source == "tdx"
    assert primary.calls == 2
    assert fallback.calls == 1
    assert sleeps == [1.0]
    assert [attempt.outcome for attempt in result.attempts] == ["failed", "failed", "passed"]


def test_fallback_cannot_fill_missing_fields_or_return_partial_noncanonical_data():
    primary = FakeProvider("eastmoney", [MarketProviderError("x"), MarketProviderError("x")])
    fallback = FakeProvider("tdx", [tuple()])

    with pytest.raises(MarketProviderError, match="主备行情源均未返回完整日线"):
        ProviderChain(primary, fallback, sleep=lambda _seconds: None).fetch_daily_bars(
            "600519", date(2026, 8, 7), date(2026, 8, 7)
        )
    assert primary.calls == 2
    assert fallback.calls == 1


def test_single_provider_records_one_sina_observation_without_any_fallback():
    sina = FakeProvider("sina", [(bar("sina"),)])

    result = SingleProvider(sina).fetch_daily_bars(
        "600519", date(2026, 8, 7), date(2026, 8, 7)
    )

    assert result.selected_source == "sina"
    assert result.bars[0].source == "sina"
    assert [(attempt.source, attempt.attempt_number, attempt.outcome) for attempt in result.attempts] == [
        ("sina", 1, "passed")
    ]
    assert sina.calls == 1


def test_single_provider_fails_closed_without_calling_another_source():
    sina = FakeProvider("sina", [MarketProviderError("offline")])

    with pytest.raises(MarketProviderError, match="新浪行情源"):
        SingleProvider(sina).fetch_daily_bars(
            "600519", date(2026, 8, 7), date(2026, 8, 7)
        )
    assert sina.calls == 1
