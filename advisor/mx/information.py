"""Bounded, read-only MX Information View and safe media descriptors."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Iterator
from urllib.parse import quote
from zoneinfo import ZoneInfo

from advisor.evidence.mx_adapter import redact_sensitive_text
from advisor.mx.rid_authorization import (
    RidAuthorizationStore,
    RidAuthorizationUnavailable,
    RidAuthorizationValidationError,
)


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_EVENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_WORD = re.compile(r"[\w\u3400-\u9fff]+", re.UNICODE)
_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp", "image/avif"})
_MAX_CURSOR_BYTES = 2_048
_MAX_QUERY_CHARS = 160
_MAX_SUMMARY_CHARS = 800
_MAX_DETAIL_CHARS = 12_000
_MAX_MEDIA_PER_EVENT = 20
_MAX_MEDIA_BYTES = 10 * 1024 * 1024


class MxInformationError(ValueError):
    pass


class MxInformationUnavailable(MxInformationError):
    pass


class MxInformationNotFound(MxInformationError):
    pass


class MxInformationValidationError(MxInformationError):
    pass


class MxInformationCursorConflict(MxInformationError):
    pass


@dataclass(frozen=True)
class _Filters:
    rids: tuple[int, ...]
    authorization: str | None
    start_at: int | None
    end_at: int | None
    has_media: bool | None
    query: str | None

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "rids": self.rids,
                "authorization": self.authorization,
                "start_at": self.start_at,
                "end_at": self.end_at,
                "has_media": self.has_media,
                "query": self.query,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass
class MediaDescriptor:
    descriptor: int
    content_type: str
    size: int

    def stream(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        try:
            while True:
                chunk = os.read(self.descriptor, chunk_size)
                if not chunk:
                    return
                yield chunk
        finally:
            os.close(self.descriptor)


class MxInformationStore:
    """The only SQL/media seam used by MX historical-information routes."""

    def __init__(
        self,
        events_database: Path,
        allowed_rids_path: Path,
        *,
        repository_root: Path,
        token_secret: bytes,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(token_secret, bytes) or len(token_secret) < 16:
            raise ValueError("information token secret is invalid")
        self.events_database = Path(events_database)
        self.allowed_rids_path = Path(allowed_rids_path)
        self.repository_root = Path(repository_root)
        self._secret = token_secret
        self._clock = clock or (lambda: datetime.now(tz=_SHANGHAI))

    def list_events(
        self,
        *,
        rids: Iterable[int] = (),
        authorization: str | None = None,
        start_at: int | None = None,
        end_at: int | None = None,
        has_media: bool | None = None,
        query: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, object]:
        filters = _Filters(
            rids=_normalize_rids(rids),
            authorization=_normalize_authorization(authorization),
            start_at=_normalize_millis(start_at),
            end_at=_normalize_millis(end_at),
            has_media=_normalize_bool(has_media),
            query=_normalize_query(query),
        )
        if filters.start_at is not None and filters.end_at is not None and filters.start_at > filters.end_at:
            raise MxInformationValidationError("invalid time range")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise MxInformationValidationError("invalid page size")
        cursor_state = self._decode_cursor(cursor, filters) if cursor else None
        allowed = self._allowed_rids()
        connection = self._open()
        try:
            where, parameters = self._where_clause(filters, allowed, cursor_state)
            rows = connection.execute(
                f"""
                SELECT e.event_id, e.rid, e.received_at, e.source_created_at,
                       e.decoded_text, e.content_hash,
                       (SELECT count(*) FROM media AS m WHERE m.event_id = e.event_id) AS media_count,
                       (SELECT count(*) FROM media_jobs AS j
                        WHERE j.event_id = e.event_id AND j.status <> 'completed') AS pending_media_count
                FROM events AS e
                WHERE {' AND '.join(where)}
                ORDER BY e.received_at DESC, e.event_id DESC
                LIMIT ?
                """,
                (*parameters, limit + 1),
            ).fetchall()
        except sqlite3.Error as error:
            raise MxInformationUnavailable("information unavailable") from error
        finally:
            connection.close()
        if len(rows) > limit:
            page_rows = rows[:limit]
            last = page_rows[-1]
            next_cursor = self._encode_cursor(
                filters,
                received_at=_require_millis(last["received_at"]),
                event_id=_require_event_id(last["event_id"]),
            )
        else:
            page_rows = rows
            next_cursor = None
        return {
            "events": [self._event_item(row, allowed) for row in page_rows],
            "next_cursor": next_cursor,
            "limit": limit,
        }

    def read_accepted_rid_feed(
        self,
        *,
        rid: int,
        start_at: int,
        end_at: int,
        limit: int = 5_000,
    ) -> dict[str, object]:
        """Read one bounded, normalized MX Feed for the Research Engine.

        This is deliberately a narrow extension of the MX Information View
        seam: it reads only Accepted Events and its result never contains raw
        payloads, source URLs, local media paths, or a complete RID set.
        """
        rid = _require_rid(rid)
        start_at = _require_millis(start_at)
        end_at = _require_millis(end_at)
        if start_at >= end_at or type(limit) is not int or not 1 <= limit <= 5_000:
            raise MxInformationValidationError("invalid research feed range")
        allowed = self._allowed_rids()
        if rid not in allowed:
            return _research_feed(rid, "unavailable", "RID authorization is not current", ())
        connection = self._open()
        try:
            if not _has_research_feed_schema(connection):
                raise MxInformationUnavailable("information unavailable")
            global_failure = connection.execute(
                """
                SELECT 1 FROM decode_failures
                WHERE bucket_start > ? AND bucket_start <= ? AND count > 0
                LIMIT 1
                """,
                (start_at, end_at),
            ).fetchone()
            if global_failure is not None:
                return _research_feed(rid, "blocked", "unattributed decode or integrity failure", ())
            rows = connection.execute(
                """
                SELECT e.event_id, e.rid, e.received_at, e.source_created_at,
                       e.decoded_text, e.content_hash,
                       (SELECT count(*) FROM media AS m WHERE m.event_id = e.event_id) AS media_count,
                       (SELECT count(*) FROM media_jobs AS j
                        WHERE j.event_id = e.event_id AND j.status <> 'completed') AS pending_media_count
                FROM events AS e
                WHERE e.rid = ? AND e.received_at > ? AND e.received_at <= ?
                ORDER BY e.received_at DESC, e.event_id DESC
                LIMIT ?
                """,
                (rid, start_at, end_at, limit + 1),
            ).fetchall()
        except MxInformationUnavailable:
            raise
        except sqlite3.Error as error:
            raise MxInformationUnavailable("information unavailable") from error
        finally:
            connection.close()
        if len(rows) > limit:
            return _research_feed(rid, "blocked", "RID feed item limit exceeded", ())
        if any(
            row["source_created_at"] is not None and _require_millis(row["source_created_at"]) > end_at
            for row in rows
        ):
            return _research_feed(rid, "blocked", "event is after the research boundary", ())
        if any(_safe_count(row["pending_media_count"]) > 0 for row in rows):
            return _research_feed(rid, "blocked", "media processing is pending or failed", ())
        items: list[dict[str, object]] = []
        for row in rows:
            item = self._event_item(row, allowed)
            # The Research Feed needs the event's normalized content, not the
            # operator-facing authorization presentation state.  The current
            # authorization check has already happened above.
            item.pop("authorization", None)
            items.append(item)
        return _research_feed(rid, "passed", "", tuple(items))

    def event_detail(self, opaque_event_id: str) -> dict[str, object]:
        event_id = self._unseal("event", opaque_event_id)
        event_id = _require_event_id(event_id)
        allowed = self._allowed_rids()
        connection = self._open()
        try:
            row = connection.execute(
                """SELECT event_id, rid, received_at, source_created_at, decoded_text, content_hash
                   FROM events WHERE event_id = ? AND received_at <= ?""",
                (event_id, _now_millis(self._clock)),
            ).fetchone()
            if row is None:
                raise MxInformationNotFound("event not found")
            media_rows = connection.execute(
                """SELECT url_hash, content_hash, content_type
                   FROM media WHERE event_id = ? ORDER BY downloaded_at, content_hash LIMIT ?""",
                (event_id, _MAX_MEDIA_PER_EVENT + 1),
            ).fetchall()
            pending_row = connection.execute(
                "SELECT count(*) AS count FROM media_jobs WHERE event_id = ? AND status <> 'completed'",
                (event_id,),
            ).fetchone()
        except MxInformationNotFound:
            raise
        except sqlite3.Error as error:
            raise MxInformationUnavailable("information unavailable") from error
        finally:
            connection.close()
        if len(media_rows) > _MAX_MEDIA_PER_EVENT:
            raise MxInformationUnavailable("information unavailable")
        item = self._event_item(row, allowed, include_summary=False)
        blocks: list[dict[str, object]] = []
        text = _safe_text(row["decoded_text"], _MAX_DETAIL_CHARS)
        if text:
            blocks.append({"type": "text", "text": text})
        for media in media_rows:
            url_hash = _require_hash(media["url_hash"])
            blocks.append(
                {
                    "type": "media",
                    "media_id": self._seal("media", f"{event_id}\0{url_hash}"),
                    "content_hash": _require_hash(media["content_hash"]),
                    "content_type": _require_media_type(media["content_type"]),
                }
            )
        if not blocks and _safe_count(pending_row["count"] if pending_row else 0) > 0:
            blocks.append({"type": "text", "text": "该资讯包含图片，正在安全处理。"})
        if not blocks:
            blocks.append({"type": "text", "text": "该资讯没有可展示的规范化内容。"})
        return {**item, "blocks": blocks}

    def open_media(self, opaque_event_id: str, opaque_media_id: str) -> MediaDescriptor:
        event_id = _require_event_id(self._unseal("event", opaque_event_id))
        media_reference = self._unseal("media", opaque_media_id)
        try:
            token_event, url_hash = media_reference.split("\0", 1)
        except ValueError as error:
            raise MxInformationNotFound("media not found") from error
        if token_event != event_id:
            raise MxInformationNotFound("media not found")
        url_hash = _require_hash(url_hash)
        connection = self._open()
        try:
            row = connection.execute(
                """SELECT local_path, content_type
                   FROM media WHERE event_id = ? AND url_hash = ?""",
                (event_id, url_hash),
            ).fetchone()
        except sqlite3.Error as error:
            raise MxInformationUnavailable("information unavailable") from error
        finally:
            connection.close()
        if row is None:
            raise MxInformationNotFound("media not found")
        return self._open_media_file(row["local_path"], row["content_type"])

    def _allowed_rids(self) -> frozenset[int]:
        try:
            return frozenset(RidAuthorizationStore(self.allowed_rids_path).read().rids)
        except (RidAuthorizationUnavailable, RidAuthorizationValidationError) as error:
            raise MxInformationUnavailable("information unavailable") from error

    def _open(self) -> sqlite3.Connection:
        try:
            mode = self.events_database.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise MxInformationUnavailable("information unavailable")
            connection = sqlite3.connect(
                f"file:{quote(str(self.events_database.absolute()))}?mode=ro",
                uri=True,
                timeout=1,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            if not _has_information_schema(connection):
                raise MxInformationUnavailable("information unavailable")
            return connection
        except MxInformationUnavailable:
            raise
        except (OSError, sqlite3.Error) as error:
            raise MxInformationUnavailable("information unavailable") from error

    def _where_clause(
        self,
        filters: _Filters,
        allowed: frozenset[int],
        cursor: tuple[int, str] | None,
    ) -> tuple[list[str], list[object]]:
        where = ["e.received_at <= ?"]
        parameters: list[object] = [_now_millis(self._clock)]
        if filters.rids:
            where.append(f"e.rid IN ({','.join('?' for _ in filters.rids)})")
            parameters.extend(filters.rids)
        if filters.authorization == "current":
            if not allowed:
                where.append("0")
            else:
                where.append(f"e.rid IN ({','.join('?' for _ in allowed)})")
                parameters.extend(sorted(allowed))
        elif filters.authorization == "revoked" and allowed:
            where.append(f"e.rid NOT IN ({','.join('?' for _ in allowed)})")
            parameters.extend(sorted(allowed))
        if filters.start_at is not None:
            where.append("e.received_at >= ?")
            parameters.append(filters.start_at)
        if filters.end_at is not None:
            where.append("e.received_at <= ?")
            parameters.append(filters.end_at)
        if filters.has_media is not None:
            where.append(
                "EXISTS(SELECT 1 FROM media AS media_filter WHERE media_filter.event_id = e.event_id)"
                if filters.has_media
                else "NOT EXISTS(SELECT 1 FROM media AS media_filter WHERE media_filter.event_id = e.event_id)"
            )
        if filters.query:
            where.append("e.rowid IN (SELECT rowid FROM mx_event_search WHERE mx_event_search MATCH ?)")
            parameters.append(filters.query)
        if cursor is not None:
            where.append("(e.received_at < ? OR (e.received_at = ? AND e.event_id < ?))")
            parameters.extend((cursor[0], cursor[0], cursor[1]))
        return where, parameters

    def _event_item(self, row: sqlite3.Row, allowed: frozenset[int], *, include_summary: bool = True) -> dict[str, object]:
        event_id = _require_event_id(row["event_id"])
        rid = _require_rid(row["rid"])
        received_at = _format_millis(row["received_at"])
        source_created_at = _format_millis_optional(row["source_created_at"])
        item: dict[str, object] = {
            "event_id": self._seal("event", event_id),
            "rid": rid,
            "authorization": "current" if rid in allowed else "revoked",
            "received_at": received_at,
            "source_created_at": source_created_at,
            "content_hash": _require_hash(row["content_hash"]),
        }
        if include_summary:
            available = _safe_count(row["media_count"])
            pending = _safe_count(row["pending_media_count"])
            item["summary"] = _safe_text(row["decoded_text"], _MAX_SUMMARY_CHARS)
            item["media"] = {
                "available_count": available,
                "pending_count": pending,
                "has_media": available > 0 or pending > 0,
            }
        return item

    def _encode_cursor(self, filters: _Filters, *, received_at: int, event_id: str) -> str:
        return self._seal(
            "cursor",
            json.dumps(
                {"filters": filters.fingerprint(), "received_at": received_at, "event_id": event_id},
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    def _decode_cursor(self, token: str, filters: _Filters) -> tuple[int, str]:
        raw = self._unseal("cursor", token)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise MxInformationCursorConflict("cursor invalid") from error
        if not isinstance(value, dict) or value.get("filters") != filters.fingerprint():
            raise MxInformationCursorConflict("cursor conflict")
        try:
            return _require_millis(value["received_at"]), _require_event_id(value["event_id"])
        except (KeyError, MxInformationValidationError) as error:
            raise MxInformationCursorConflict("cursor invalid") from error

    def _seal(self, kind: str, value: str) -> str:
        raw = value.encode("utf-8")
        nonce = hmac.new(self._secret, f"nonce:{kind}:".encode("ascii") + raw, hashlib.sha256).digest()[:16]
        stream = _keystream(self._secret, kind, nonce, len(raw))
        cipher = bytes(left ^ right for left, right in zip(raw, stream, strict=True))
        body = nonce + cipher
        signature = hmac.new(self._secret, kind.encode("ascii") + b"\0" + body, hashlib.sha256).digest()
        return f"{_b64(body)}.{_b64(signature)}"

    def _unseal(self, kind: str, token: str) -> str:
        if not isinstance(token, str) or len(token) > _MAX_CURSOR_BYTES or token.count(".") != 1:
            raise MxInformationNotFound("opaque identifier invalid")
        encoded, signature = token.split(".", 1)
        try:
            body = _unb64(encoded)
            actual_signature = _unb64(signature)
        except ValueError as error:
            raise MxInformationNotFound("opaque identifier invalid") from error
        expected = hmac.new(self._secret, kind.encode("ascii") + b"\0" + body, hashlib.sha256).digest()
        if len(body) < 16 or not hmac.compare_digest(actual_signature, expected):
            raise MxInformationNotFound("opaque identifier invalid")
        nonce, cipher = body[:16], body[16:]
        raw = bytes(left ^ right for left, right in zip(cipher, _keystream(self._secret, kind, nonce, len(cipher)), strict=True))
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise MxInformationNotFound("opaque identifier invalid") from error

    def _open_media_file(self, local_path: object, content_type: object) -> MediaDescriptor:
        relative = _safe_media_relative_path(local_path)
        media_type = _require_media_type(content_type)
        root = self.repository_root
        try:
            root_mode = root.lstat().st_mode
            if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
                raise MxInformationNotFound("media not found")
            current = root
            for index, part in enumerate(relative.parts):
                current = current / part
                mode = current.lstat().st_mode
                if index < len(relative.parts) - 1:
                    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                        raise MxInformationNotFound("media not found")
                elif stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                    raise MxInformationNotFound("media not found")
            descriptor = os.open(current, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except MxInformationNotFound:
            raise
        except OSError as error:
            raise MxInformationNotFound("media not found") from error
        try:
            opened = os.fstat(descriptor)
            named = os.stat(current, follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_size < 1
                or opened.st_size > _MAX_MEDIA_BYTES
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise MxInformationNotFound("media not found")
            head = os.read(descriptor, 32)
            os.lseek(descriptor, 0, os.SEEK_SET)
            if not _matches_image_type(head, media_type):
                raise MxInformationNotFound("media not found")
            return MediaDescriptor(descriptor=descriptor, content_type=media_type, size=opened.st_size)
        except BaseException:
            os.close(descriptor)
            raise


def _has_information_schema(connection: sqlite3.Connection) -> bool:
    expected = {
        "events": {"event_id", "rid", "received_at", "source_created_at", "decoded_text", "content_hash"},
        "media": {"event_id", "url_hash", "content_hash", "content_type", "local_path", "downloaded_at"},
        "media_jobs": {"event_id", "status"},
    }
    try:
        for table, columns in expected.items():
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
            if not columns.issubset({row[1] for row in rows}):
                return False
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'mx_event_search'"
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _has_research_feed_schema(connection: sqlite3.Connection) -> bool:
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(decode_failures)").fetchall()
        }
        return {"payload_hash", "error_class", "bucket_start", "count"}.issubset(columns)
    except sqlite3.Error:
        return False


def _research_feed(
    rid: int,
    status: str,
    reason: str,
    items: tuple[dict[str, object], ...],
) -> dict[str, object]:
    return {
        "rid": rid,
        "quality": {"status": status, "reason": reason},
        "items": list(items),
    }


def _normalize_rids(values: Iterable[int]) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)):
        raise MxInformationValidationError("invalid RIDs")
    try:
        result = tuple(values)
    except TypeError as error:
        raise MxInformationValidationError("invalid RIDs") from error
    if len(result) > 100 or any(type(value) is not int or not 0 < value <= 2**53 - 1 for value in result):
        raise MxInformationValidationError("invalid RIDs")
    if len(set(result)) != len(result):
        raise MxInformationValidationError("invalid RIDs")
    return tuple(sorted(result))


def _normalize_authorization(value: object) -> str | None:
    if value is None:
        return None
    if value not in {"current", "revoked"}:
        raise MxInformationValidationError("invalid authorization state")
    return str(value)


def _normalize_millis(value: object) -> int | None:
    if value is None:
        return None
    return _require_millis(value)


def _normalize_bool(value: object) -> bool | None:
    if value is None or type(value) is bool:
        return value
    raise MxInformationValidationError("invalid media filter")


def _normalize_query(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > _MAX_QUERY_CHARS:
        raise MxInformationValidationError("invalid query")
    words = _WORD.findall(value)
    if not words:
        return None
    if len(words) > 10 or any(len(word) > 64 for word in words):
        raise MxInformationValidationError("invalid query")
    # Quote terms to keep FTS operators supplied by a client from changing the
    # query plan or expanding the scan scope.
    return " AND ".join(f'"{word.replace(chr(34), "")}"' for word in words)


def _require_event_id(value: object) -> str:
    if not isinstance(value, str) or not _EVENT_ID.fullmatch(value):
        raise MxInformationValidationError("invalid event")
    return value


def _require_hash(value: object) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise MxInformationValidationError("invalid hash")
    return value


def _require_rid(value: object) -> int:
    if type(value) is not int or not 0 < value <= 2**53 - 1:
        raise MxInformationValidationError("invalid RID")
    return value


def _require_millis(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise MxInformationValidationError("invalid timestamp")
    return value


def _safe_count(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_MEDIA_PER_EVENT * 1000:
        raise MxInformationUnavailable("information unavailable")
    return value


def _require_media_type(value: object) -> str:
    if not isinstance(value, str) or value not in _IMAGE_TYPES:
        raise MxInformationNotFound("media not found")
    return value


def _format_millis(value: object) -> str:
    return datetime.fromtimestamp(_require_millis(value) / 1000, tz=_SHANGHAI).isoformat()


def _format_millis_optional(value: object) -> str | None:
    return None if value is None else _format_millis(value)


def _safe_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        raise MxInformationUnavailable("information unavailable")
    normalized = " ".join(redact_sensitive_text(value).split())
    normalized = _URL.sub("[链接已隐藏]", normalized)
    return normalized[:limit]


def _safe_media_relative_path(value: object) -> PurePosixPath:
    if not isinstance(value, str) or len(value) > 512 or "\\" in value or any(ord(char) < 32 for char in value):
        raise MxInformationNotFound("media not found")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.parts[:2] != ("data", "media")
        or len(relative.parts) < 4
        or any(part in {"", ".", ".."} for part in relative.parts)
        or str(relative) != value
    ):
        raise MxInformationNotFound("media not found")
    return relative


def _matches_image_type(head: bytes, content_type: str) -> bool:
    if content_type == "image/jpeg":
        return head.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    if content_type == "image/webp":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if content_type == "image/avif":
        return len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in {b"avif", b"avis"}
    return False


def _now_millis(clock: Callable[[], datetime]) -> int:
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise MxInformationUnavailable("information unavailable")
    return int(now.timestamp() * 1000)


def _keystream(secret: bytes, kind: str, nonce: bytes, length: int) -> bytes:
    parts: list[bytes] = []
    counter = 0
    while sum(map(len, parts)) < length:
        parts.append(hmac.new(secret, kind.encode("ascii") + b"\0" + nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest())
        counter += 1
    return b"".join(parts)[:length]


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,2048}", value):
        raise ValueError("invalid base64")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
