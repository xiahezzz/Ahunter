from dataclasses import FrozenInstanceError
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from advisor.market_daily.contracts import (
    AdjustmentFactor,
    CanonicalDailyBar,
    MarketAbsence,
    MarketContractError,
    MarketSecurity,
    ObservedTradingSession,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
SOURCE_TIME = datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI)


def make_security(**overrides):
    values = {
        "code": "600519",
        "name": "贵州茅台",
        "exchange": "SH",
        "list_date": date(2001, 8, 27),
        "delist_date": None,
        "status": "active",
        "is_st": False,
        "source": "sse",
        "source_at": SOURCE_TIME,
        "fetched_at": SOURCE_TIME,
    }
    values.update(overrides)
    return MarketSecurity(**values)


def make_bar(**overrides):
    values = {
        "code": "600519",
        "trade_date": date(2026, 8, 7),
        "open": 1400.0,
        "high": 1410.0,
        "low": 1390.0,
        "close": 1405.0,
        "volume": 100_000,
        "amount": 140_500_000.0,
        "source": "eastmoney",
        "source_at": SOURCE_TIME,
        "fetched_at": SOURCE_TIME,
    }
    values.update(overrides)
    return CanonicalDailyBar(**values)


def test_market_security_is_immutable_and_hashes_stably():
    first = make_security()
    second = make_security()

    assert first.content_hash == second.content_hash
    with pytest.raises(FrozenInstanceError):
        first.name = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("code", "exchange"),
    [
        ("920001", "SH"),
        ("830001", "SZ"),
        ("200001", "SZ"),
        ("600519", "SZ"),
        ("000001", "SH"),
    ],
)
def test_market_security_rejects_non_sh_sz_a_share_identity(code, exchange):
    with pytest.raises(MarketContractError):
        make_security(code=code, exchange=exchange)


def test_canonical_daily_bar_normalizes_only_integer_shares_and_stable_hash():
    first = make_bar()
    second = make_bar()

    assert first.content_hash == second.content_hash
    assert first.volume == 100_000
    with pytest.raises(MarketContractError):
        make_bar(volume=100_000.5)
    with pytest.raises(MarketContractError):
        make_bar(low=1411.0)


def test_canonical_daily_bar_keeps_unreported_amount_as_missing_instead_of_fabricating_zero():
    first = make_bar(amount=None, source="sina")
    second = make_bar(amount=None, source="sina")

    assert first.amount is None
    assert first.content_hash == second.content_hash
    with pytest.raises(MarketContractError):
        make_bar(amount=-1)


def test_adjustment_factor_is_separate_from_daily_bar_and_requires_positive_value():
    factor = AdjustmentFactor(
        code="600519",
        trade_date=date(2026, 8, 7),
        factor=1.0,
        source="tdx",
        source_at=SOURCE_TIME,
        fetched_at=SOURCE_TIME,
        algorithm_version="factors@1",
    )

    assert factor.factor == 1.0
    assert len(factor.content_hash) == 64
    with pytest.raises(MarketContractError):
        AdjustmentFactor(
            code="600519",
            trade_date=date(2026, 8, 7),
            factor=0.0,
            source="tdx",
            source_at=SOURCE_TIME,
            fetched_at=SOURCE_TIME,
            algorithm_version="factors@1",
        )


def test_observed_session_requires_two_distinct_sources_for_same_completed_date():
    session = ObservedTradingSession(
        trade_date=date(2026, 8, 7),
        primary_source="eastmoney",
        fallback_source="tdx",
        primary_observed_at=SOURCE_TIME,
        fallback_observed_at=SOURCE_TIME,
        fetched_at=SOURCE_TIME,
    )

    assert session.trade_date == date(2026, 8, 7)
    with pytest.raises(MarketContractError):
        ObservedTradingSession(
            trade_date=date(2026, 8, 7),
            primary_source="eastmoney",
            fallback_source="eastmoney",
            primary_observed_at=SOURCE_TIME,
            fallback_observed_at=SOURCE_TIME,
            fetched_at=SOURCE_TIME,
        )


def test_market_absence_only_persists_evidenced_suspensions():
    absence = MarketAbsence(
        code="600519",
        trade_date=date(2026, 8, 7),
        reason="suspended",
        source="exchange",
        source_at=SOURCE_TIME,
        fetched_at=SOURCE_TIME,
    )

    assert absence.reason == "suspended"
    with pytest.raises(MarketContractError):
        MarketAbsence(
            code="600519",
            trade_date=date(2026, 8, 7),
            reason="source_missing",
            source="exchange",
            source_at=SOURCE_TIME,
            fetched_at=SOURCE_TIME,
        )
