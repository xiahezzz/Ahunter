from __future__ import annotations

from datetime import datetime, timezone

import pytest

from advisor.research.providers.market import derive_indicators, normalize_daily_rows, normalize_quote


BOUNDARY = datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc)
FETCHED = datetime(2026, 8, 6, 8, 31, tzinfo=timezone.utc)


def _row(day: str, close: float = 10.0, **overrides):
    value = {
        "trade_date": day,
        "open": close - 0.5,
        "high": close + 0.5,
        "low": close - 1,
        "close": close,
        "volume": 100,
        "amount": 1000,
    }
    value.update(overrides)
    return value


def test_daily_rows_are_sorted_bounded_and_carry_units_and_provenance():
    rows = normalize_daily_rows(
        [_row("2026-08-05"), _row("2026-08-04")],
        code="600519",
        as_of=BOUNDARY,
        source="fixture-sina",
        fetched_at=FETCHED,
    )
    assert [row["trade_date"] for row in rows] == ["2026-08-04", "2026-08-05"]
    assert rows[0]["adjustment"] == "unadjusted"
    assert rows[0]["price_unit"] == "CNY/share"
    assert rows[0]["fetched_at"] == FETCHED.isoformat()


@pytest.mark.parametrize(
    "rows, message",
    [
        ([_row("2026-08-05"), _row("2026-08-05")], "unique"),
        ([_row("2026-08-07")], "after as_of"),
        ([_row("2026-08-05", high=8)], "OHLC"),
        ([_row("2026-08-05", close=float("nan"))], "invalid"),
    ],
)
def test_daily_rows_fail_closed_on_future_nonfinite_duplicate_or_bad_ohlc(rows, message):
    with pytest.raises(ValueError, match=message):
        normalize_daily_rows(rows, code="600519", as_of=BOUNDARY, source="fixture", fetched_at=FETCHED)


def test_indicator_recalculation_is_deterministic_and_does_not_use_future_rows():
    rows = [_row("2026-08-04", 10), _row("2026-08-05", 11)]
    assert derive_indicators(rows) == derive_indicators(list(rows))
    assert derive_indicators(rows)["latest_close"] == 11


def test_quote_preserves_zero_and_declares_units():
    quote = normalize_quote({"name": "Fixture", "price": "0", "change_pct": "0"}, code="600519", source="fixture", fetched_at=FETCHED)
    assert quote["price"] == 0
    assert quote["change_pct"] == 0
    assert quote["currency"] == "CNY"


def test_public_feed_without_offset_uses_a_share_local_time():
    from advisor.research.providers.normalization import parse_public_time

    parsed = parse_public_time("2026-08-06 08:00:00")
    assert parsed is not None
    assert parsed.isoformat() == "2026-08-06T08:00:00+08:00"
