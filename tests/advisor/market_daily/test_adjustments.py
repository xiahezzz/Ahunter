from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from advisor.market_daily.adjustments import AdjustmentError, normalize_forward_factors


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI)


def test_normalization_makes_latest_factor_one_and_is_stable():
    first = normalize_forward_factors(
        "600519",
        ((date(2026, 8, 6), 0.25), (date(2026, 8, 7), 0.5)),
        source="fixture",
        source_at=NOW,
        fetched_at=NOW,
    )
    second = normalize_forward_factors(
        "600519",
        ((date(2026, 8, 6), 0.25), (date(2026, 8, 7), 0.5)),
        source="fixture",
        source_at=NOW,
        fetched_at=NOW,
    )

    assert [factor.factor for factor in first] == [0.5, 1.0]
    assert [factor.content_hash for factor in first] == [factor.content_hash for factor in second]


@pytest.mark.parametrize(
    "raw_factors",
    [
        (),
        ((date(2026, 8, 7), 0.0),),
        ((date(2026, 8, 7), float("nan")),),
        ((date(2026, 8, 7), 1.0), (date(2026, 8, 7), 1.0)),
    ],
)
def test_invalid_or_incomplete_factor_inputs_fail_closed(raw_factors):
    with pytest.raises(AdjustmentError):
        normalize_forward_factors(
            "600519", raw_factors, source="fixture", source_at=NOW, fetched_at=NOW
        )
