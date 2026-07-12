from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from advisor.evidence.mx_adapter import CollectorSnapshot, MxEvidence, redact_sensitive_text


_STOCK_CODE_RE = re.compile(r"\b([03468]\d{5})\b")


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

    events = [event for event in snapshot.events if _event_is_bounded(event, as_of)]
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


def _event_is_bounded(event: MxEvidence, as_of: datetime) -> bool:
    timestamps = (event.received_at, event.source_created_at)
    try:
        if any(
            value is not None
            and (
                not isinstance(value, datetime)
                or value.tzinfo is None
                or value.utcoffset() is None
                or value > as_of
            )
            for value in timestamps
        ):
            return False
        return all(
            isinstance(item.downloaded_at, datetime)
            and item.downloaded_at.tzinfo is not None
            and item.downloaded_at.utcoffset() is not None
            and item.downloaded_at <= as_of
            for item in event.media
        )
    except (TypeError, ValueError, OverflowError):
        return False
