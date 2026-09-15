from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from advisor.market_daily.adjustments import AdjustmentError, derive_forward_adjusted_prices, normalize_forward_factors
from advisor.market_daily.contracts import CanonicalDailyBar
from advisor.market_daily.repository import MarketDailyRepository


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI)


def raw(day: date, close: float) -> CanonicalDailyBar:
    return CanonicalDailyBar(
        code="600519",
        trade_date=day,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=100_000,
        amount=close * 100_000,
        source="eastmoney",
        source_at=NOW,
        fetched_at=NOW,
    )


def factors():
    return normalize_forward_factors(
        "600519",
        ((date(2026, 8, 6), 0.5), (date(2026, 8, 7), 1.0)),
        source="fixture",
        source_at=NOW,
        fetched_at=NOW,
    )


def test_forward_adjusted_series_removes_mechanical_ex_right_jump_but_raw_remains_raw(tmp_path):
    bars = (raw(date(2026, 8, 6), 100.0), raw(date(2026, 8, 7), 50.0))
    series = derive_forward_adjusted_prices(bars, factors())
    repository = MarketDailyRepository(tmp_path / "advisor.sqlite")
    for bar in bars:
        repository.insert_bar(bar)

    assert [bar.close for bar in series.bars] == [50.0, 50.0]
    assert repository.bar_for("600519", date(2026, 8, 6)).close == 100.0
    assert len(series.factor_set_hash) == 64
    assert len(series.content_hash) == 64


def test_missing_factor_fails_closed():
    with pytest.raises(AdjustmentError, match="不完整匹配"):
        derive_forward_adjusted_prices((raw(date(2026, 8, 7), 50.0),), factors())
