from __future__ import annotations

from datetime import datetime, timezone

from advisor.research.providers.market_structure import normalize_structure_rows


AS_OF = datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc)
FETCHED = datetime(2026, 8, 6, 8, 31, tzinfo=timezone.utc)


def test_capital_flow_and_dragon_tiger_rows_have_direction_units_and_deterministic_net():
    flows = normalize_structure_rows(
        "capital_flows",
        [{"trade_date": "2026-08-05", "direction": "流入", "amount": 12.5, "amount_unit": "CNY million"}],
        code="600519", as_of=AS_OF, source_locator="fixture://flow", fetched_at=FETCHED,
    )
    dragon = normalize_structure_rows(
        "dragon_tiger",
        [{"trade_date": "2026-08-05", "buy_amount": 20, "sell_amount": 8, "reason": "limit"}],
        code="600519", as_of=AS_OF, source_locator="fixture://dragon", fetched_at=FETCHED,
    )
    assert flows.payload["items"][0]["direction"] == "inflow"
    assert flows.payload["items"][0]["amount_unit"] == "CNY million"
    assert flows.payload["items"][0]["amount_cny"] == 12_500_000
    assert dragon.payload["items"][0]["net_amount"] == 12


def test_empty_events_differ_from_format_drift_and_future_lockup_is_excluded():
    empty = normalize_structure_rows("lockup_calendar", [], code="600519", as_of=AS_OF, source_locator="fixture://lockup", fetched_at=FETCHED)
    drift = normalize_structure_rows("capital_flows", [{"unexpected": "shape"}], code="600519", as_of=AS_OF, source_locator="fixture://flow", fetched_at=FETCHED)
    future = normalize_structure_rows(
        "lockup_calendar",
        [{"plan_date": "2026-08-08", "announcement_at": "2026-08-07", "shares": 10}],
        code="600519", as_of=AS_OF, source_locator="fixture://lockup", fetched_at=FETCHED,
    )
    assert empty.payload["status"] == "empty"
    assert drift.payload["status"] == "warning"
    assert drift.quality_status == "warning"
    assert future.payload["status"] == "empty"


def test_future_lockup_is_allowed_only_when_its_announcement_was_visible():
    visible = normalize_structure_rows(
        "lockup_calendar",
        [{"plan_date": "2026-08-08", "announcement_at": "2026-08-01", "shares": 10}],
        code="600519", as_of=AS_OF, source_locator="fixture://lockup", fetched_at=FETCHED,
    )
    assert visible.payload["items"][0]["plan_date"] == "2026-08-08"


def test_concepts_are_stable_when_source_order_changes():
    rows = [
        {"concept": "tea", "industry": "food", "sample_date": "2026-08-05"},
        {"concept": "consumer", "industry": "retail", "sample_date": "2026-08-04"},
    ]
    first = normalize_structure_rows("concepts", rows, code="600519", as_of=AS_OF, source_locator="fixture://concepts", fetched_at=FETCHED)
    second = normalize_structure_rows("concepts", list(reversed(rows)), code="600519", as_of=AS_OF, source_locator="fixture://concepts", fetched_at=FETCHED)
    assert first.payload == second.payload
