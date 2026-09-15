from __future__ import annotations

from datetime import datetime, timezone

import pytest

from advisor.research.providers.fundamentals import (
    normalize_earnings_forecast,
    normalize_financial_statements,
    normalize_industry_context,
    normalize_insider_activity,
)


AS_OF = datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc)
FETCHED = datetime(2026, 8, 6, 8, 31, tzinfo=timezone.utc)


def test_financial_rows_are_structured_with_period_disclosure_unit_and_zero_values():
    html = """
    <table><tr><th>report_period</th><th>disclosed_at</th><th>revenue</th><th>net_profit</th><th>unit</th></tr>
    <tr><td>2025-12-31</td><td>2026-03-30T08:00:00Z</td><td>100.5</td><td>0</td><td>CNY million</td></tr></table>
    """
    result = normalize_financial_statements(html, code="600519", as_of=AS_OF, source_locator="fixture://finance", fetched_at=FETCHED)
    record = result.payload["items"][0]
    assert record["report_period"] == "2025-12-31"
    assert record["values"]["net_profit"]["value"] == 0
    assert record["values"]["net_profit"]["unit"] == "CNY"
    assert record["values"]["revenue"]["value"] == 100_500_000
    assert record["values"]["revenue"]["reported_unit"] == "CNY million"
    assert record["values"]["net_profit"]["disclosed_at"].startswith("2026-03-30")


def test_financial_unit_conversion_does_not_invent_non_cny_fx():
    result = normalize_financial_statements(
        [
            {"report_period": "2025-12-31", "disclosed_at": "2026-03-30", "revenue": 2, "unit": "CNY billion"},
            {"report_period": "2025-12-31", "disclosed_at": "2026-03-30", "revenue": 2, "unit": "USD million", "currency": "USD"},
        ],
        code="600519", as_of=AS_OF, source_locator="fixture://finance", fetched_at=FETCHED,
    )
    assert result.payload["items"][0]["values"]["revenue"]["value"] == 200_000_000
    assert "revenue:unsupported_currency" in result.payload["uncovered_fields"]


def test_future_disclosure_is_rejected_even_when_report_period_is_historical():
    with pytest.raises(ValueError, match="after as_of"):
        normalize_financial_statements(
            [{"report_period": "2025-12-31", "disclosed_at": "2026-08-07", "revenue": 1}],
            code="600519", as_of=AS_OF, source_locator="fixture://finance", fetched_at=FETCHED,
        )


def test_empty_and_uncovered_fields_are_visible_without_inventing_values():
    result = normalize_financial_statements(
        [{"report_period": "2025-12-31", "disclosed_at": "2026-03-30", "revenue": 0}],
        code="600519", as_of=AS_OF, source_locator="fixture://finance", fetched_at=FETCHED,
    )
    assert "net_profit" in result.payload["uncovered_fields"]
    assert result.payload["items"][0]["values"]["revenue"]["value"] == 0
    empty = normalize_financial_statements([], code="600519", as_of=AS_OF, source_locator="fixture://finance", fetched_at=FETCHED)
    assert empty.payload["status"] == "empty"


def test_forecast_industry_and_insider_products_preserve_their_time_semantics():
    forecast = normalize_earnings_forecast(
        [{"forecast_year": 2026, "published_at": "2026-07-01", "eps": 2.1, "institution_count": 8}],
        code="600519", as_of=AS_OF, source_locator="fixture://forecast", fetched_at=FETCHED,
    )
    industry = normalize_industry_context(
        [{"industry": "food", "sample_date": "2026-08-05", "classification_version": "v1", "target_rank": 2, "sample_size": 10}],
        code="600519", as_of=AS_OF, source_locator="fixture://industry", fetched_at=FETCHED,
    )
    insider = normalize_insider_activity(
        [{"holder": "fixture holder", "direction": "增持", "shares": 0, "event_date": "2026-07-01", "disclosed_at": "2026-07-02"}],
        code="600519", as_of=AS_OF, source_locator="fixture://insider", fetched_at=FETCHED,
    )
    assert forecast.payload["items"][0]["institution_count"] == 8
    assert industry.payload["items"][0]["classification_version"] == "v1"
    assert insider.payload["items"][0]["shares"] == 0
    assert insider.payload["items"][0]["direction"] == "buy"
