from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from advisor.market_daily.repository import MarketDailyRepository
from advisor.market_daily.providers.contracts import MarketProviderError
from advisor.market_daily.sessions import ObservedSessionService, SessionObservationError


SHANGHAI = ZoneInfo("Asia/Shanghai")
AFTER_CLOSE = datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI)


class FakeSessionProvider:
    def __init__(self, source: str, dates: tuple[date, ...] | BaseException) -> None:
        self.source = source
        self.dates = dates
        self.calls = 0

    def observe_sessions(self, start: date, end: date) -> tuple[date, ...]:
        self.calls += 1
        if isinstance(self.dates, BaseException):
            raise self.dates
        return tuple(item for item in self.dates if start <= item <= end)


def service(tmp_path, primary_dates, fallback_dates):
    return ObservedSessionService(
        MarketDailyRepository(tmp_path / "advisor.sqlite"),
        FakeSessionProvider("eastmoney", primary_dates),
        FakeSessionProvider("tdx", fallback_dates),
    )


def test_matching_sources_persist_stable_ordered_session_facts_and_then_noop(tmp_path):
    dates = (date(2026, 8, 5), date(2026, 8, 6), date(2026, 8, 7))
    observed = service(tmp_path, dates, dates)

    first = observed.refresh(date(2026, 8, 5), date(2026, 8, 7), AFTER_CLOSE)
    second = observed.refresh(date(2026, 8, 5), date(2026, 8, 7), AFTER_CLOSE)

    assert first.status == "updated"
    assert first.inserted == 3
    assert second.status == "no_op"
    assert second.inserted == 0
    assert first.session_hash == second.session_hash
    assert observed.sessions_for(date(2026, 8, 5), date(2026, 8, 7)) == dates
    receipt = MarketDailyRepository(tmp_path / "advisor.sqlite").latest_session_observation_on_or_before(AFTER_CLOSE)
    assert receipt is not None
    assert receipt.latest_session == date(2026, 8, 7)


def test_two_same_moment_observations_with_different_windows_are_both_auditable(tmp_path):
    dates = (date(2026, 8, 5), date(2026, 8, 6), date(2026, 8, 7))
    observed = service(tmp_path, dates, dates)

    observed.refresh(date(2026, 8, 6), date(2026, 8, 7), AFTER_CLOSE)
    observed.refresh(date(2026, 8, 5), date(2026, 8, 7), AFTER_CLOSE)

    repository = MarketDailyRepository(tmp_path / "advisor.sqlite")
    receipt = repository.latest_session_observation_on_or_before(AFTER_CLOSE)
    assert receipt is not None
    assert receipt.latest_session == date(2026, 8, 7)


def test_weekend_or_holiday_is_noop_when_observed_latest_date_is_unchanged(tmp_path):
    friday = (date(2026, 8, 7),)
    observed = service(tmp_path, friday, friday)
    observed.refresh(date(2026, 8, 7), date(2026, 8, 7), AFTER_CLOSE)

    again = observed.refresh(
        date(2026, 8, 7),
        date(2026, 8, 9),
        datetime(2026, 8, 9, 21, 0, tzinfo=SHANGHAI),
    )

    assert again.status == "no_op"
    assert again.latest_session == date(2026, 8, 7)


@pytest.mark.parametrize(
    "primary_dates,fallback_dates",
    [
        ((date(2026, 8, 7),), RuntimeError("down")),
        (RuntimeError("down"), (date(2026, 8, 7),)),
        ((date(2026, 8, 6),), (date(2026, 8, 7),)),
    ],
)
def test_unavailable_or_conflicting_sources_do_not_guess_a_closed_session(
    tmp_path, primary_dates, fallback_dates
):
    observed = service(tmp_path, primary_dates, fallback_dates)
    with pytest.raises((RuntimeError, SessionObservationError)):
        observed.refresh(date(2026, 8, 6), date(2026, 8, 7), AFTER_CLOSE)
    assert observed.sessions_for(date(2026, 8, 6), date(2026, 8, 7)) == ()


def test_provider_error_is_normalized_at_the_session_boundary(tmp_path):
    observed = service(
        tmp_path,
        MarketProviderError("fixture source unavailable"),
        (date(2026, 8, 7),),
    )

    with pytest.raises(SessionObservationError, match="eastmoney.*fixture source unavailable"):
        observed.refresh(date(2026, 8, 6), date(2026, 8, 7), AFTER_CLOSE)


def test_2100_boundary_rejects_early_run_and_uses_persisted_sessions_afterward(tmp_path):
    dates = (date(2026, 8, 7),)
    observed = service(tmp_path, dates, dates)

    with pytest.raises(SessionObservationError, match="21:00"):
        observed.refresh(
            date(2026, 8, 7), date(2026, 8, 7), datetime(2026, 8, 7, 20, 59, 59, tzinfo=SHANGHAI)
        )
    observed.refresh(date(2026, 8, 7), date(2026, 8, 7), AFTER_CLOSE)

    assert observed.latest_completed_session(AFTER_CLOSE) == date(2026, 8, 7)
    assert observed.latest_completed_session(
        datetime(2026, 8, 8, 8, 0, tzinfo=SHANGHAI)
    ) == date(2026, 8, 7)
