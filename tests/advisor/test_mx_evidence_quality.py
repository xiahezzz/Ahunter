import sqlite3
from pathlib import Path

from advisor.evidence.mx_adapter import read_mx_events
from advisor.quality import has_blocking_failure


def make_events_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE events (
          event_id TEXT PRIMARY KEY,
          rid INTEGER NOT NULL,
          received_at INTEGER NOT NULL,
          decoded_text TEXT NOT NULL,
          content_hash TEXT NOT NULL
        );
        INSERT INTO events VALUES ('evt-1', 123, 1783728000000, '关注 600519 贵州茅台 放量', 'hash-1');
        """
    )
    connection.close()


def test_read_mx_events_maps_accepted_collector_rows(tmp_path: Path):
    db_path = tmp_path / "events.sqlite"
    make_events_db(db_path)
    events = read_mx_events(db_path)
    assert len(events) == 1
    assert events[0].source_type == "mx"
    assert events[0].source_id == "evt-1"
    assert events[0].code == "600519"
    assert "贵州茅台" in events[0].summary


def test_quality_blocks_when_required_history_missing(tmp_path: Path):
    db_path = tmp_path / "advisor.sqlite"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE market_daily (code TEXT, trade_date TEXT);
        CREATE TABLE ledger_transactions (transaction_id TEXT);
        """
    )
    assert has_blocking_failure(
        connection,
        required_codes=["600519"],
        as_of="2026-07-11T08:30:00+08:00",
    )
