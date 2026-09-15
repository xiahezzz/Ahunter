"""Canonical calendar helpers for mainland A-share research boundaries."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def a_share_date(value: datetime) -> date:
    """Return the Shanghai calendar date for a timezone-aware instant."""

    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A-share boundary must be timezone-aware")
    return value.astimezone(_SHANGHAI).date()


__all__ = ["a_share_date"]
