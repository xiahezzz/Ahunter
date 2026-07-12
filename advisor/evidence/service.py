from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from advisor.evidence.mx_adapter import (
    CollectorSnapshot,
    MxEvidence,
    evidence_id_for,
    redact_sensitive_text,
    valid_content_hash,
    valid_media_metadata,
    valid_opaque_identifier,
)


_STOCK_CODE_RE = re.compile(r"\b([03468]\d{5})\b")
_MAX_SUMMARY_CHARS = 800
_MAX_MEDIA_PER_EVENT = 20


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    run_id: str
    code: str | None
    as_of: str
    source_type: str
    source_id: str
    summary: str


def persist_evidence(
    connection: sqlite3.Connection,
    run_id: str,
    snapshot: CollectorSnapshot,
    *,
    as_of: datetime,
) -> list[EvidenceRecord]:
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be sqlite3.Connection")
    if not isinstance(run_id, str) or not run_id or len(run_id) > 64:
        raise ValueError("invalid run_id")
    if not isinstance(snapshot, CollectorSnapshot):
        raise TypeError("snapshot must be CollectorSnapshot")
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if (
        not isinstance(snapshot.as_of, datetime)
        or snapshot.as_of.tzinfo is None
        or snapshot.as_of.utcoffset() is None
        or snapshot.as_of > as_of
    ):
        raise ValueError("snapshot as_of exceeds run as_of")

    if not isinstance(snapshot.events, tuple) or len(snapshot.events) > 100:
        raise ValueError("invalid snapshot events")
    for event in snapshot.events:
        _validate_event(event, as_of)
    events = list(snapshot.events)
    records = [_record(run_id, event) for event in events]
    owns_transaction = not connection.in_transaction
    savepoint = "mx_evidence_handoff"
    try:
        connection.execute("BEGIN IMMEDIATE" if owns_transaction else f"SAVEPOINT {savepoint}")
        for event, record in zip(events, records):
            raw_ref = json.dumps(
                {
                    "content_hash": event.content_hash,
                    "media": [item.to_dict() for item in event.media],
                    "received_at": event.received_at.isoformat(),
                    "rid": event.rid,
                    "source_created_at": event.source_created_at.isoformat() if event.source_created_at else None,
                    "source_id": redact_sensitive_text(event.source_id),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO events_normalized (
                  evidence_source_id, source_type, source_id, code, as_of, summary,
                  raw_ref_json, quality_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'passed')
                """,
                (
                    event.evidence_id, event.source_type, event.source_id, record.code,
                    record.as_of, record.summary, raw_ref,
                ),
            )
            normalized = connection.execute(
                """
                SELECT source_type, source_id, code, as_of, summary, raw_ref_json, quality_status
                FROM events_normalized WHERE evidence_source_id = ?
                """,
                (event.evidence_id,),
            ).fetchone()
            if normalized is None or tuple(normalized) != (
                event.source_type, event.source_id, record.code, record.as_of,
                record.summary, raw_ref, "passed",
            ):
                raise ValueError("conflicting normalized evidence identity")
            connection.execute(
                """
                INSERT OR IGNORE INTO evidence (
                  evidence_id, run_id, code, as_of, source_type, source_id, summary,
                  confidence, facts_json, inferences_json, conflicts_json, quality_flags_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0.5, '[]', '[]', '[]', '[]')
                """,
                (
                    record.evidence_id, record.run_id, record.code, record.as_of,
                    record.source_type, record.source_id, record.summary,
                ),
            )
            persisted = connection.execute(
                """
                SELECT run_id, code, as_of, source_type, source_id, summary
                FROM evidence WHERE evidence_id = ?
                """,
                (record.evidence_id,),
            ).fetchone()
            if persisted is None or tuple(persisted) != (
                record.run_id, record.code, record.as_of, record.source_type,
                record.source_id, record.summary,
            ):
                raise ValueError("conflicting evidence identity")
        if owns_transaction:
            connection.commit()
        else:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
    except BaseException:
        if owns_transaction:
            connection.rollback()
        else:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    return records


def _record(run_id: str, event: MxEvidence) -> EvidenceRecord:
    summary = redact_sensitive_text(event.summary)
    match = _STOCK_CODE_RE.search(summary)
    return EvidenceRecord(
        evidence_id=event.evidence_id,
        run_id=run_id,
        code=match.group(1) if match else None,
        as_of=event.received_at.isoformat(),
        source_type=event.source_type,
        source_id=event.source_id,
        summary=summary,
    )


def _validate_event(event: object, as_of: datetime) -> None:
    if not isinstance(event, MxEvidence):
        raise ValueError("invalid evidence DTO")
    if event.source_type != "mx":
        raise ValueError("invalid source_type")
    if type(event.rid) is not int or event.rid <= 0:
        raise ValueError("invalid rid")
    if not valid_content_hash(event.content_hash):
        raise ValueError("invalid content_hash")
    if not valid_opaque_identifier(event.source_id):
        raise ValueError("invalid source_id")
    if not valid_content_hash(event.evidence_id) or event.evidence_id != evidence_id_for(
        event.source_id, event.content_hash
    ):
        raise ValueError("invalid evidence_id")
    _validate_timestamp("received_at", event.received_at, as_of)
    if event.source_created_at is not None:
        _validate_timestamp("source_created_at", event.source_created_at, as_of)
    if (
        not isinstance(event.summary, str)
        or len(event.summary) > _MAX_SUMMARY_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in event.summary)
        or " ".join(event.summary.split()) != event.summary
        or redact_sensitive_text(event.summary) != event.summary
    ):
        raise ValueError("invalid summary")
    if not isinstance(event.media, tuple) or len(event.media) > _MAX_MEDIA_PER_EVENT:
        raise ValueError("invalid media metadata")
    for item in event.media:
        if not valid_media_metadata(item):
            raise ValueError("invalid media metadata")
        _validate_timestamp("media downloaded_at", item.downloaded_at, as_of)


def _validate_timestamp(name: str, value: object, as_of: datetime) -> None:
    try:
        valid = (
            isinstance(value, datetime)
            and value.tzinfo is not None
            and value.utcoffset() is not None
            and value <= as_of
        )
    except (TypeError, ValueError, OverflowError):
        valid = False
    if not valid:
        raise ValueError(f"invalid {name}")
