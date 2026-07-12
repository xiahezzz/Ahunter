from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePath
from urllib.parse import quote

import yaml


_MAX_CONFIG_BYTES = 64 * 1024
_MAX_SUMMARY_CHARS = 800
_MAX_LOCAL_PATH_CHARS = 512
_MAX_SCAN_ROWS = 1_000
_MAX_MEDIA_PER_EVENT = 20
_HASH_CHARS = frozenset("0123456789abcdef")
_COUNTER_KINDS = frozenset({"accepted", "duplicate", "failed", "ignored", "media_failed", "rejected"})
_AUTHORIZATION = re.compile(
    r"\bauthorization\s*[:=]\s*(?:bearer\s+)?[^\s,;]+",
    re.IGNORECASE,
)
_COOKIE = re.compile(r"\bcookie\s*[:=]\s*[^\r\n]+", re.IGNORECASE)
_BEARER = re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_JWT = re.compile(
    r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"\b(?:api[_-]?key|secret|password|credential|token|session|socket(?:[_.-]?id)?|debug(?:ger|[_.-]?id)?|cdp)\s*[:=]\s*[^\s,;]+",
    re.IGNORECASE,
)
_SECRET_PREFIX = re.compile(r"\b(?:sk|pk|sess)_[A-Za-z0-9_-]{8,}", re.IGNORECASE)
_REQUIRED_COLUMNS = {
    "events": {
        "event_id", "schema_version", "rid", "source_message_id", "oid",
        "received_at", "source_created_at", "raw_payload_hash", "raw_payload",
        "raw_payload_expires_at", "decoded_text", "parsed_content_json",
        "content_hash", "ingest_run_id",
    },
    "media": {
        "event_id", "rid", "source_url", "url_hash", "content_hash",
        "content_type", "local_path", "downloaded_at",
    },
    "media_jobs": {
        "event_id", "rid", "source_url", "url_hash", "status", "attempts",
        "next_attempt_at", "error_code",
    },
    "decode_failures": {"payload_hash", "error_class", "bucket_start", "count"},
    "ingest_counters": {"bucket_start", "kind", "count"},
    "ingest_runs": {"run_id", "started_at"},
}


@dataclass(frozen=True)
class CollectorQuality:
    check_name: str
    severity: str
    passed: bool
    details: str

    @property
    def blocking_failure(self) -> bool:
        return self.severity == "blocking" and not self.passed


@dataclass(frozen=True)
class MediaMetadata:
    content_hash: str
    content_type: str
    local_path: str
    downloaded_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "content_hash": self.content_hash,
            "content_type": redact_sensitive_text(self.content_type),
            "local_path": redact_sensitive_text(self.local_path),
            "downloaded_at": self.downloaded_at.isoformat(),
        }


@dataclass(frozen=True)
class MxEvidence:
    evidence_id: str
    source_type: str
    source_id: str
    rid: int
    content_hash: str
    summary: str
    received_at: datetime
    source_created_at: datetime | None
    media: tuple[MediaMetadata, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "rid": self.rid,
            "content_hash": self.content_hash,
            "summary": self.summary,
            "received_at": self.received_at.isoformat(),
            "source_created_at": self.source_created_at.isoformat() if self.source_created_at else None,
            "media": [item.to_dict() for item in self.media],
        }


@dataclass(frozen=True)
class CollectorSnapshot:
    events: tuple[MxEvidence, ...]
    quality: object
    as_of: datetime


def read_collector_snapshot(
    events_db: Path,
    allowed_rids_path: Path,
    *,
    as_of: datetime,
    limit: int = 100,
) -> CollectorSnapshot:
    _validate_request(events_db, allowed_rids_path, as_of, limit)
    try:
        allowed_rids = _read_allowed_rids(allowed_rids_path)
    except (OSError, UnicodeError, yaml.YAMLError, ValueError):
        return _blocked(as_of, "allowed RID configuration is invalid")
    if not allowed_rids:
        return _blocked(as_of, "collector is intentionally inactive: no allowed RIDs")

    try:
        connection = _open_read_only(events_db)
    except (OSError, sqlite3.Error):
        return _blocked(as_of, "collector database is unavailable")
    try:
        connection.row_factory = sqlite3.Row
        if not _valid_schema(connection):
            return _blocked(as_of, "collector schema is missing required columns")
        return _snapshot_from_connection(connection, allowed_rids, as_of, limit)
    except (sqlite3.Error, TypeError, ValueError, OverflowError):
        return _blocked(as_of, "collector data is invalid or ambiguous")
    finally:
        connection.close()


def _validate_request(events_db: Path, config: Path, as_of: datetime, limit: int) -> None:
    if not isinstance(events_db, Path) or not isinstance(config, Path):
        raise TypeError("collector paths must be Path values")
    for path in (events_db, config):
        try:
            mode = path.lstat().st_mode
        except OSError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError("symlinked collector input is not allowed")
        if not stat.S_ISREG(mode):
            raise ValueError("collector input must be a regular file")
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")


def _read_allowed_rids(path: Path) -> tuple[int, ...]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("RID configuration must be a regular file")
        raw = os.read(descriptor, _MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise ValueError("RID configuration is too large")
    finally:
        os.close(descriptor)
    payload = yaml.safe_load(raw.decode("utf-8"))
    if not isinstance(payload, dict) or set(payload) != {"allowed_rids"}:
        raise ValueError("RID configuration must contain only allowed_rids")
    values = payload["allowed_rids"]
    if not isinstance(values, list):
        raise ValueError("allowed_rids must be a list")
    if any(type(value) is not int or value <= 0 for value in values):
        raise ValueError("allowed RIDs must be positive integers")
    if len(values) > 1_000 or len(set(values)) != len(values):
        raise ValueError("allowed RIDs must be bounded and unique")
    return tuple(values)


def _open_read_only(path: Path) -> sqlite3.Connection:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    pin_dir: Path | None = None
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("collector database must be a regular file")
        pin_dir = Path(tempfile.mkdtemp(prefix=".mx-read-", dir=path.parent))
        pinned_db = pin_dir / "events.sqlite"
        os.link(path, pinned_db, follow_symlinks=False)
        pinned = pinned_db.stat()
        if (opened.st_dev, opened.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise ValueError("collector database changed while pinning")
        for suffix in ("-wal", "-shm"):
            _pin_optional_sqlite_sidecar(Path(f"{path}{suffix}"), Path(f"{pinned_db}{suffix}"))
        uri = f"file:{quote(str(pinned_db))}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, factory=_PinnedReadOnlyConnection)
        connection.pin_dir = pin_dir
        connection.execute("PRAGMA query_only = ON")
        return connection
    except BaseException:
        if pin_dir is not None:
            shutil.rmtree(pin_dir, ignore_errors=True)
        raise
    finally:
        os.close(descriptor)


class _PinnedReadOnlyConnection(sqlite3.Connection):
    pin_dir: Path | None = None

    def close(self) -> None:
        pin_dir = self.pin_dir
        try:
            super().close()
        finally:
            if pin_dir is not None:
                shutil.rmtree(pin_dir, ignore_errors=True)
                self.pin_dir = None


def _pin_optional_sqlite_sidecar(source: Path, target: Path) -> None:
    try:
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("collector SQLite sidecar must be a regular file")
        os.link(source, target, follow_symlinks=False)
        pinned = target.stat()
        if (opened.st_dev, opened.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise ValueError("collector SQLite sidecar changed while pinning")
    finally:
        os.close(descriptor)


def _valid_schema(connection: sqlite3.Connection) -> bool:
    for table, required in _REQUIRED_COLUMNS.items():
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        if not required <= {row[1] for row in rows}:
            return False
    return True


def _snapshot_from_connection(
    connection: sqlite3.Connection,
    allowed_rids: tuple[int, ...],
    as_of: datetime,
    limit: int,
) -> CollectorSnapshot:
    as_of_ms = int(as_of.timestamp() * 1_000)
    problems: list[str] = []
    all_events = connection.execute(
        "SELECT event_id, rid, received_at FROM events WHERE received_at <= ? ORDER BY received_at, event_id LIMIT ?",
        (as_of_ms, _MAX_SCAN_ROWS + 1),
    ).fetchall()
    if len(all_events) > _MAX_SCAN_ROWS:
        problems.append("collector event scan limit exceeded")
        all_events = all_events[:_MAX_SCAN_ROWS]

    counters = connection.execute(
        "SELECT bucket_start, kind, count FROM ingest_counters WHERE bucket_start <= ? ORDER BY bucket_start, kind LIMIT ?",
        (as_of_ms, _MAX_SCAN_ROWS + 1),
    ).fetchall()
    if len(counters) > _MAX_SCAN_ROWS or any(
        type(row["bucket_start"]) is not int
        or row["bucket_start"] < 0
        or type(row["count"]) is not int
        or row["count"] < 0
        or row["kind"] not in _COUNTER_KINDS
        for row in counters
    ):
        problems.append("collector counters are ambiguous")
    else:
        accepted = sum(row["count"] for row in counters if row["kind"] == "accepted")
        if accepted != len(all_events):
            problems.append("collector accepted counter does not match stored events")

    future = connection.execute(
        "SELECT 1 FROM events WHERE received_at > ? OR source_created_at > ? LIMIT 1",
        (as_of_ms, as_of_ms),
    ).fetchone()
    future_counter = connection.execute(
        "SELECT 1 FROM ingest_counters WHERE bucket_start > ? LIMIT 1", (as_of_ms,)
    ).fetchone()
    future_media = connection.execute(
        "SELECT 1 FROM media WHERE downloaded_at > ? LIMIT 1", (as_of_ms,)
    ).fetchone()
    future_run = connection.execute(
        "SELECT 1 FROM ingest_runs WHERE started_at > ? LIMIT 1", (as_of_ms,)
    ).fetchone()
    future_failure = connection.execute(
        "SELECT 1 FROM decode_failures WHERE bucket_start > ? LIMIT 1", (as_of_ms,)
    ).fetchone()
    if future or future_counter or future_media or future_run or future_failure:
        problems.append("collector contains future-dated records")

    placeholders = ",".join("?" for _ in allowed_rids)
    rows = connection.execute(
        f"""
        SELECT event_id, rid, received_at, source_created_at, decoded_text, content_hash
        FROM events
        WHERE rid IN ({placeholders}) AND received_at <= ?
          AND (source_created_at IS NULL OR source_created_at <= ?)
        ORDER BY received_at DESC, event_id DESC
        LIMIT ?
        """,
        (*allowed_rids, as_of_ms, as_of_ms, limit),
    ).fetchall()
    event_ids = [row["event_id"] for row in rows]
    media_by_event = _read_media(connection, event_ids, allowed_rids, as_of_ms, problems)
    events = tuple(_event_from_row(row, media_by_event.get(row["event_id"], ()), as_of) for row in rows)

    jobs = connection.execute(
        f"SELECT status FROM media_jobs WHERE rid IN ({placeholders}) AND status <> 'completed' LIMIT ?",
        (*allowed_rids, _MAX_SCAN_ROWS + 1),
    ).fetchall()
    if len(jobs) > _MAX_SCAN_ROWS:
        problems.append("collector media job scan limit exceeded")
    else:
        statuses = sorted({row["status"] for row in jobs})
        if statuses:
            problems.append("collector media jobs are " + "/".join(statuses))
    failures = connection.execute(
        "SELECT count FROM decode_failures WHERE bucket_start <= ? LIMIT ?",
        (as_of_ms, _MAX_SCAN_ROWS + 1),
    ).fetchall()
    if len(failures) > _MAX_SCAN_ROWS or any(type(row["count"]) is not int or row["count"] <= 0 for row in failures):
        problems.append("collector decode failure records are ambiguous")
    elif failures:
        problems.append("collector has decode failures")

    quality = CollectorQuality(
        check_name="collector_state",
        severity="blocking",
        passed=not problems,
        details="; ".join(problems) if problems else f"collector snapshot valid for {len(events)} authorized events",
    )
    return CollectorSnapshot(events=events, quality=quality, as_of=as_of)


def _read_media(
    connection: sqlite3.Connection,
    event_ids: list[str],
    allowed_rids: tuple[int, ...],
    as_of_ms: int,
    problems: list[str],
) -> dict[str, tuple[MediaMetadata, ...]]:
    if not event_ids:
        return {}
    event_marks = ",".join("?" for _ in event_ids)
    rid_marks = ",".join("?" for _ in allowed_rids)
    cap = len(event_ids) * _MAX_MEDIA_PER_EVENT
    rows = connection.execute(
        f"""
        SELECT event_id, content_hash, content_type, local_path, downloaded_at
        FROM media
        WHERE event_id IN ({event_marks}) AND rid IN ({rid_marks}) AND downloaded_at <= ?
        ORDER BY event_id, downloaded_at, content_hash
        LIMIT ?
        """,
        (*event_ids, *allowed_rids, as_of_ms, cap + 1),
    ).fetchall()
    if len(rows) > cap:
        problems.append("collector media metadata limit exceeded")
        rows = rows[:cap]
    grouped: dict[str, list[MediaMetadata]] = {}
    for row in rows:
        local_path = row["local_path"]
        if (
            not _valid_hash(row["content_hash"])
            or not isinstance(row["content_type"], str)
            or len(row["content_type"]) > 128
            or not isinstance(local_path, str)
            or not local_path
            or len(local_path) > _MAX_LOCAL_PATH_CHARS
            or ".." in PurePath(local_path).parts
        ):
            raise ValueError("invalid media metadata")
        grouped.setdefault(row["event_id"], []).append(
            MediaMetadata(
                content_hash=row["content_hash"],
                content_type=row["content_type"],
                local_path=local_path,
                downloaded_at=_timestamp(row["downloaded_at"], timezone.utc),
            )
        )
    return {key: tuple(value) for key, value in grouped.items()}


def _event_from_row(row: sqlite3.Row, media: tuple[MediaMetadata, ...], as_of: datetime) -> MxEvidence:
    source_id = row["event_id"]
    content_hash = row["content_hash"]
    rid = row["rid"]
    if (
        not isinstance(source_id, str) or not source_id or len(source_id) > 256
        or type(rid) is not int or rid <= 0
        or not _valid_hash(content_hash)
        or not isinstance(row["decoded_text"], str)
    ):
        raise ValueError("invalid accepted event")
    summary = redact_sensitive_text(" ".join(row["decoded_text"].split()))[:_MAX_SUMMARY_CHARS]
    received_at = _timestamp(row["received_at"], as_of.tzinfo)
    source_created_at = (
        _timestamp(row["source_created_at"], as_of.tzinfo)
        if row["source_created_at"] is not None
        else None
    )
    digest_input = f"a-hunter:evidence:v1\0mx\0{source_id}\0{content_hash}".encode("utf-8")
    return MxEvidence(
        evidence_id=hashlib.sha256(digest_input).hexdigest(),
        source_type="mx",
        source_id=source_id,
        rid=rid,
        content_hash=content_hash,
        summary=summary,
        received_at=received_at,
        source_created_at=source_created_at,
        media=media,
    )


def _timestamp(value: object, zone) -> datetime:
    if type(value) is not int or value < 0:
        raise ValueError("invalid collector timestamp")
    return datetime.fromtimestamp(value / 1_000, tz=timezone.utc).astimezone(zone)


def _valid_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value.lower()) <= _HASH_CHARS


def redact_sensitive_text(value: str) -> str:
    redacted = _AUTHORIZATION.sub("[redacted]", value)
    redacted = _COOKIE.sub("[redacted]", redacted)
    redacted = _BEARER.sub("[redacted]", redacted)
    redacted = _JWT.sub("[redacted]", redacted)
    redacted = _SENSITIVE_ASSIGNMENT.sub("[redacted]", redacted)
    return _SECRET_PREFIX.sub("[redacted]", redacted)


def _blocked(as_of: datetime, details: str) -> CollectorSnapshot:
    return CollectorSnapshot(
        events=(),
        quality=CollectorQuality("collector_state", "blocking", False, details),
        as_of=as_of,
    )
