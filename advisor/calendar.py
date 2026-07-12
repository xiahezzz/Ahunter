from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MARKET_CLOSE = dt.time(15, 0)
_SUPPORTED_START = dt.date(2025, 1, 1)
_SUPPORTED_END = dt.date(2026, 12, 31)

# Bounded local XSHG/XSHE holiday table for the scheduler's no-key production path.
# Weekend makeup workdays are intentionally not exchange sessions unless explicitly listed.
_A_SHARE_HOLIDAYS = frozenset(
    {
        dt.date(2025, 1, 1),
        dt.date(2025, 1, 28),
        dt.date(2025, 1, 29),
        dt.date(2025, 1, 30),
        dt.date(2025, 1, 31),
        dt.date(2025, 2, 3),
        dt.date(2025, 2, 4),
        dt.date(2025, 4, 4),
        dt.date(2025, 5, 1),
        dt.date(2025, 5, 2),
        dt.date(2025, 5, 5),
        dt.date(2025, 6, 2),
        dt.date(2025, 10, 1),
        dt.date(2025, 10, 2),
        dt.date(2025, 10, 3),
        dt.date(2025, 10, 6),
        dt.date(2025, 10, 7),
        dt.date(2025, 10, 8),
        dt.date(2026, 1, 1),
        dt.date(2026, 1, 2),
        dt.date(2026, 2, 16),
        dt.date(2026, 2, 17),
        dt.date(2026, 2, 18),
        dt.date(2026, 2, 19),
        dt.date(2026, 2, 20),
        dt.date(2026, 2, 23),
        dt.date(2026, 4, 6),
        dt.date(2026, 5, 1),
        dt.date(2026, 5, 4),
        dt.date(2026, 5, 5),
        dt.date(2026, 6, 19),
        dt.date(2026, 9, 25),
        dt.date(2026, 10, 1),
        dt.date(2026, 10, 2),
        dt.date(2026, 10, 5),
        dt.date(2026, 10, 6),
        dt.date(2026, 10, 7),
    }
)
_A_SHARE_EXTRA_SESSIONS = frozenset()


class UnsupportedTradingCalendarError(ValueError):
    pass


def latest_expected_session(as_of: dt.datetime) -> dt.date:
    local_as_of = _to_shanghai(as_of)
    _ensure_supported(local_as_of.date())
    session = local_as_of.date()
    if not (is_trading_session(session) and local_as_of.time() >= _MARKET_CLOSE):
        session -= dt.timedelta(days=1)
    return previous_trading_session(session)


def previous_trading_session(session: dt.date) -> dt.date:
    _ensure_supported(session)
    candidate = session
    while not is_trading_session(candidate):
        candidate -= dt.timedelta(days=1)
        _ensure_supported(candidate)
    return candidate


def is_trading_session(session: dt.date) -> bool:
    if not _is_supported(session):
        return False
    if session in _A_SHARE_EXTRA_SESSIONS:
        return True
    if session in _A_SHARE_HOLIDAYS:
        return False
    return session.weekday() < 5


def _to_shanghai(as_of: dt.datetime) -> dt.datetime:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        return as_of.replace(tzinfo=_SHANGHAI)
    return as_of.astimezone(_SHANGHAI)


def _ensure_supported(session: dt.date) -> None:
    if not _is_supported(session):
        raise UnsupportedTradingCalendarError(
            "unsupported A-share trading calendar range"
        )


def _is_supported(session: dt.date) -> bool:
    return _SUPPORTED_START <= session <= _SUPPORTED_END
