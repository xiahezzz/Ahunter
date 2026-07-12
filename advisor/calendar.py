from __future__ import annotations

import datetime as dt


def latest_expected_session(as_of: dt.datetime) -> dt.date:
    session = as_of.date()
    while session.weekday() >= 5:
        session -= dt.timedelta(days=1)
    return session
