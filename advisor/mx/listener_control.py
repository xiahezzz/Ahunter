"""Read-only projection of the Node-owned MX Listener control plane."""

from __future__ import annotations

import re
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_READINESS = frozenset({
    "starting", "waiting_for_chrome", "waiting_for_authorization",
    "connecting", "listening", "stopping",
})
_HEALTH = frozenset({"healthy", "degraded", "failed"})
_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class ListenerControlUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class ListenerControlSnapshot:
    liveness: str
    readiness: str
    health: str
    reason_code: str
    connected_at: str | None
    last_frame_at: str | None
    last_accepted_event_at: str | None
    lease_expires_at: str | None


def read_listener_control_snapshot(path: Path, *, now: datetime) -> ListenerControlSnapshot:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    filename = Path(path)
    try:
        mode = filename.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise ListenerControlUnavailable("control unavailable")
        connection = sqlite3.connect(
            f"file:{quote(str(filename.absolute()))}?mode=ro",
            uri=True,
            timeout=1,
        )
    except (OSError, sqlite3.Error) as error:
        raise ListenerControlUnavailable("control unavailable") from error
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        if not _has_control_schema(connection):
            raise ListenerControlUnavailable("control unavailable")
        status = connection.execute(
            """SELECT readiness, health, reason_code, connected_at, last_frame_at,
                      last_accepted_event_at
               FROM listener_service_status WHERE singleton = 1"""
        ).fetchone()
        lease = connection.execute(
            "SELECT heartbeat_at, expires_at FROM listener_service_lease WHERE singleton = 1"
        ).fetchone()
    except sqlite3.Error as error:
        raise ListenerControlUnavailable("control unavailable") from error
    finally:
        connection.close()

    if status is None:
        readiness, health, reason = "stopping", "failed", "not_started"
        activity = (None, None, None)
    else:
        readiness = status["readiness"] if status["readiness"] in _READINESS else "stopping"
        health = status["health"] if status["health"] in _HEALTH else "failed"
        reason = status["reason_code"] if isinstance(status["reason_code"], str) and _REASON.fullmatch(status["reason_code"]) else "state_invalid"
        activity = (status["connected_at"], status["last_frame_at"], status["last_accepted_event_at"])
    now_ms = int(now.timestamp() * 1000)
    live = bool(
        lease
        and _safe_millis(lease["heartbeat_at"]) is not None
        and _safe_millis(lease["expires_at"]) is not None
        and int(lease["heartbeat_at"]) <= now_ms < int(lease["expires_at"])
    )
    return ListenerControlSnapshot(
        liveness="live" if live else "offline",
        readiness=readiness,
        health=health,
        reason_code=reason,
        connected_at=_format_millis(activity[0]),
        last_frame_at=_format_millis(activity[1]),
        last_accepted_event_at=_format_millis(activity[2]),
        lease_expires_at=_format_millis(lease["expires_at"] if lease else None),
    )


def _has_control_schema(connection: sqlite3.Connection) -> bool:
    expected = {
        "listener_service_lease": {"singleton", "heartbeat_at", "expires_at"},
        "listener_service_status": {
            "singleton", "readiness", "health", "reason_code", "connected_at",
            "last_frame_at", "last_accepted_event_at",
        },
    }
    for table, columns in expected.items():
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        if not columns.issubset({row[1] for row in rows}):
            return False
    return True


def _safe_millis(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _format_millis(value: object) -> str | None:
    parsed = _safe_millis(value)
    if parsed is None:
        return None
    try:
        return datetime.fromtimestamp(parsed / 1000, tz=_SHANGHAI).isoformat()
    except (OSError, OverflowError, ValueError):
        return None
