"""Narrow calendar compatibility facade backed only by observed sessions.

This module intentionally has no holiday table.  A caller must supply the
locally persisted sessions it has already proved from independent sources.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from zoneinfo import ZoneInfo


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_READY_AT = dt.time(21, 0)


class ObservedTradingSessionsUnavailableError(ValueError):
    """No suitable local observed-session fact exists for the requested time."""


# Kept as a narrow import compatibility alias for older callers.  It no longer
# means a finite, hard-coded calendar range is being used.
UnsupportedTradingCalendarError = ObservedTradingSessionsUnavailableError


def latest_expected_session(as_of: dt.datetime, sessions: Iterable[dt.date] | None = None) -> dt.date:
    """Return the latest completed date from explicit observed session facts."""
    local_as_of = _to_shanghai(as_of)
    dates = _normalize_sessions(sessions)
    cutoff = local_as_of.date() if local_as_of.time() >= _READY_AT else local_as_of.date() - dt.timedelta(days=1)
    return previous_trading_session(cutoff, dates)


def previous_trading_session(session: dt.date, sessions: Iterable[dt.date] | None = None) -> dt.date:
    """Return the greatest observed session no later than *session*."""
    if not isinstance(session, dt.date) or isinstance(session, dt.datetime):
        raise ObservedTradingSessionsUnavailableError("交易日参数无效")
    dates = _normalize_sessions(sessions)
    eligible = [value for value in dates if value <= session]
    if not eligible:
        raise ObservedTradingSessionsUnavailableError("本地没有已证明的交易日")
    return eligible[-1]


def is_trading_session(session: dt.date, sessions: Iterable[dt.date] | None = None) -> bool:
    """Whether *session* exists in explicit observed session facts."""
    if not isinstance(session, dt.date) or isinstance(session, dt.datetime):
        return False
    return session in _normalize_sessions(sessions)


def _normalize_sessions(sessions: Iterable[dt.date] | None) -> tuple[dt.date, ...]:
    if sessions is None:
        raise ObservedTradingSessionsUnavailableError("交易日必须来自本地已观测事实")
    try:
        values = tuple(sessions)
    except TypeError as error:
        raise ObservedTradingSessionsUnavailableError("交易日事实无效") from error
    if not values or any(not isinstance(value, dt.date) or isinstance(value, dt.datetime) for value in values):
        raise ObservedTradingSessionsUnavailableError("本地没有已证明的交易日")
    if values != tuple(sorted(values)) or len(values) != len(set(values)):
        raise ObservedTradingSessionsUnavailableError("交易日事实必须升序且唯一")
    return values


def _to_shanghai(as_of: object) -> dt.datetime:
    if not isinstance(as_of, dt.datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ObservedTradingSessionsUnavailableError("当前时间必须带时区")
    return as_of.astimezone(_SHANGHAI)
