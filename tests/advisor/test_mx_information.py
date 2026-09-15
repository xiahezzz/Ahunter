from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from advisor.web.api import create_app


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _fixture(tmp_path: Path) -> tuple[TestClient, bytes]:
    root = tmp_path / "repo"
    root.mkdir()
    allowed = root / "allowed-rids.yaml"
    allowed.write_text("allowed_rids: [20025]\n", encoding="utf-8")
    database = root / "events.sqlite"
    now = datetime.now(tz=_SHANGHAI)
    now_ms = int(now.timestamp() * 1000)
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE events (
          event_id TEXT PRIMARY KEY, rid INTEGER, received_at INTEGER,
          source_created_at INTEGER, decoded_text TEXT, content_hash TEXT
        );
        CREATE TABLE media (
          event_id TEXT, url_hash TEXT, content_hash TEXT, content_type TEXT,
          local_path TEXT, downloaded_at INTEGER
        );
        CREATE TABLE media_jobs (event_id TEXT, status TEXT);
        CREATE VIRTUAL TABLE mx_event_search USING fts5(event_id UNINDEXED, decoded_text);
        """
    )
    rows = [
        ("event-a", 20025, now_ms - 3_000, now_ms - 4_000, "Alpha 规范化内容 https://source.invalid/private", "a" * 64),
        ("event-b", 23200, now_ms - 2_000, now_ms - 3_000, "", "b" * 64),
        ("event-c", 20025, now_ms - 1_000, None, "Current latest", "c" * 64),
    ]
    connection.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)", rows)
    connection.executemany(
        "INSERT INTO mx_event_search(rowid, event_id, decoded_text) VALUES (?, ?, ?)",
        [(1, "event-a", "Alpha 规范化内容"), (2, "event-b", ""), (3, "event-c", "Current latest")],
    )
    image = b"\xff\xd8\xff\xd9"
    image_path = root / "data" / "media" / "2026-08-08" / f"{'d' * 64}.jpg"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(image)
    connection.execute(
        "INSERT INTO media VALUES (?, ?, ?, ?, ?, ?)",
        ("event-b", "e" * 64, "d" * 64, "image/jpeg", "data/media/2026-08-08/" + "d" * 64 + ".jpg", now_ms - 1_500),
    )
    connection.commit()
    connection.close()
    app = create_app(
        state_dir=root / "state",
        db_path=root / "advisor.sqlite",
        allowed_rids_path=allowed,
        mx_events_db_path=database,
        mx_media_root=root,
    )
    return TestClient(app), image


def test_information_list_filters_searches_and_binds_keyset_cursor(tmp_path: Path):
    client, _image = _fixture(tmp_path)
    first = client.get("/api/mx/events", params={"authorization": "current", "limit": 1})
    assert first.status_code == 200
    first_item = first.json()["events"][0]
    assert first_item["authorization"] == "current"
    assert "https://source.invalid" not in first.text
    assert "private" not in first.text
    assert "raw_payload" not in first.text

    second = client.get(
        "/api/mx/events",
        params={"authorization": "current", "limit": 1, "cursor": first.json()["next_cursor"]},
    )
    assert second.status_code == 200
    assert second.json()["events"][0]["event_id"] != first_item["event_id"]

    stale = client.get(
        "/api/mx/events",
        params={"authorization": "revoked", "cursor": first.json()["next_cursor"]},
    )
    assert stale.status_code == 409
    assert "event-a" not in stale.text

    searched = client.get("/api/mx/events", params={"q": "Alpha", "rid": "20025"})
    assert searched.status_code == 200
    assert len(searched.json()["events"]) == 1
    assert searched.json()["events"][0]["rid"] == 20025

    revoked = client.get("/api/mx/events", params={"authorization": "revoked"})
    assert revoked.status_code == 200
    assert [item["rid"] for item in revoked.json()["events"]] == [23200]


def test_information_detail_and_media_are_opaque_and_safe(tmp_path: Path):
    client, image = _fixture(tmp_path)
    listing = client.get("/api/mx/events", params={"authorization": "revoked"}).json()
    event_id = listing["events"][0]["event_id"]
    detail = client.get(f"/api/mx/events/{event_id}")
    assert detail.status_code == 200
    payload = detail.json()
    media = next(block for block in payload["blocks"] if block["type"] == "media")
    assert "local_path" not in detail.text
    assert "source_url" not in detail.text
    assert "event-b" not in event_id
    response = client.get(media["href"])
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.content == image

    foreign = client.get(f"/api/mx/events/{event_id}/media/not-a-real-token")
    assert foreign.status_code == 404
    assert "data/media" not in foreign.text


def test_information_rejects_invalid_filter_and_does_not_create_missing_database(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    allowed = root / "allowed-rids.yaml"
    allowed.write_text("allowed_rids: []\n", encoding="utf-8")
    missing = root / "missing.sqlite"
    client = TestClient(
        create_app(
            state_dir=root / "state",
            db_path=root / "advisor.sqlite",
            allowed_rids_path=allowed,
            mx_events_db_path=missing,
            mx_media_root=root,
        )
    )
    bad = client.get("/api/mx/events", params={"rid": "0"})
    assert bad.status_code == 400
    unavailable = client.get("/api/mx/events")
    assert unavailable.status_code == 503
    assert not missing.exists()
