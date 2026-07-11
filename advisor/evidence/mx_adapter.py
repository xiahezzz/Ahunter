import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path


STOCK_CODE_RE = re.compile(r"\b([036]\d{5})\b")


@dataclass(frozen=True)
class NormalizedEvent:
    source_type: str
    source_id: str
    code: str | None
    as_of: str
    summary: str
    quality_status: str = "passed"


def read_mx_events(events_db: Path, limit: int = 100) -> list[NormalizedEvent]:
    connection = sqlite3.connect(events_db)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT event_id, received_at, decoded_text
            FROM events
            ORDER BY received_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        connection.close()

    events: list[NormalizedEvent] = []
    for row in rows:
        match = STOCK_CODE_RE.search(row["decoded_text"])
        events.append(
            NormalizedEvent(
                source_type="mx",
                source_id=row["event_id"],
                code=match.group(1) if match else None,
                as_of=str(row["received_at"]),
                summary=row["decoded_text"],
            )
        )
    return events
