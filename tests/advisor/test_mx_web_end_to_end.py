from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from advisor.mx.rid_authorization import RidAuthorizationStore
from advisor.services.contracts import ServiceStatus
from advisor.web.api import create_app


_SHANGHAI = ZoneInfo("Asia/Shanghai")


class _FixtureMxManager:
    def __init__(self, allowed_rids: Path) -> None:
        self.allowed_rids = allowed_rids
        self.loaded = False
        self.calls: list[str] = []

    def mx_listener_status(self, _now: datetime) -> ServiceStatus:
        rids = RidAuthorizationStore(self.allowed_rids).read().rids
        return ServiceStatus(
            "mx-listener",
            "运行中" if self.loaded else "未加载",
            "等待用户自行恢复 Chrome 授权" if self.loaded else "服务未加载",
            ("/fixture/private-log.out",),
            {
                "launchagent_loaded": self.loaded,
                "liveness": "live" if self.loaded else "offline",
                "readiness": "waiting_for_authorization" if self.loaded else "stopping",
                "health": "healthy" if self.loaded else "failed",
                "reason_code": "authorization_required" if self.loaded else "not_started",
                "connected_at": None,
                "last_frame_at": None,
                "last_accepted_event_at": None,
                "lease_expires_at": None,
                "rid_count": len(rids),
                "collection_enabled": bool(rids),
                "rid_config_valid": True,
            },
        )

    def start_mx_listener(self) -> bool:
        self.calls.append("start")
        changed = not self.loaded
        self.loaded = True
        return changed

    def stop_mx_listener(self) -> bool:
        self.calls.append("stop")
        changed = self.loaded
        self.loaded = False
        return changed


def _fixture(tmp_path: Path) -> tuple[TestClient, _FixtureMxManager]:
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    allowed = root / "config" / "allowed-rids.yaml"
    allowed.write_text("allowed_rids: [111]\n", encoding="utf-8")
    database = root / "events.sqlite"
    now_ms = int(datetime.now(tz=_SHANGHAI).timestamp() * 1000)
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
    connection.executemany(
        "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("fixture-111", 111, now_ms - 2_000, now_ms - 3_000, "RID 111 临时文本", "1" * 64),
            (
                "fixture-222",
                222,
                now_ms - 1_000,
                now_ms - 2_000,
                "RID 222 图片资讯 https://source.invalid/session-only",
                "2" * 64,
            ),
        ],
    )
    connection.executemany(
        "INSERT INTO mx_event_search(rowid, event_id, decoded_text) VALUES (?, ?, ?)",
        [(1, "fixture-111", "RID 111 临时文本"), (2, "fixture-222", "RID 222 图片资讯")],
    )
    image = root / "data" / "media" / "2026-08-08" / "fixture.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\xff\xd8\xff\xd9")
    connection.execute(
        "INSERT INTO media VALUES (?, ?, ?, ?, ?, ?)",
        ("fixture-222", "a" * 64, "b" * 64, "image/jpeg", "data/media/2026-08-08/fixture.jpg", now_ms - 500),
    )
    connection.commit()
    connection.close()
    manager = _FixtureMxManager(allowed)
    app = create_app(
        state_dir=root / "state",
        db_path=root / "advisor.sqlite",
        research_root=root,
        allowed_rids_path=allowed,
        mx_events_db_path=database,
        mx_media_root=root,
        service_manager_factory=lambda **_kwargs: manager,
    )
    return TestClient(app), manager


def test_temporary_rid_lifecycle_and_history_are_safe_without_touching_live_state(tmp_path: Path):
    client, manager = _fixture(tmp_path)

    initial = client.get("/api/mx/rids")
    assert initial.status_code == 200
    assert initial.json()["rids"] == [111]

    added = client.put(
        "/api/mx/rids",
        json={"rids": [111, 222], "version": initial.json()["version"]},
    )
    started = client.post("/api/mx/listener/start", json={})
    current = client.get("/api/mx/events", params={"authorization": "current", "has_media": "true"})

    assert added.status_code == 200
    assert added.json()["rids"] == [111, 222]
    assert started.status_code == 200
    assert started.json()["service"]["liveness"] == "live"
    assert started.json()["service"]["readiness"] == "waiting_for_authorization"
    assert started.json()["service"]["rid_count"] == 2
    assert "/fixture/" not in started.text
    assert current.status_code == 200
    event = current.json()["events"][0]
    assert event["rid"] == 222
    assert "fixture-222" not in event["event_id"]
    assert "source.invalid" not in current.text
    assert "session-only" not in current.text
    assert "local_path" not in current.text
    assert "raw_payload" not in current.text

    detail = client.get(f"/api/mx/events/{event['event_id']}")
    assert detail.status_code == 200
    media = next(block for block in detail.json()["blocks"] if block["type"] == "media")
    image = client.get(media["href"])
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/jpeg")
    assert image.content == b"\xff\xd8\xff\xd9"

    after_add = client.get("/api/mx/rids")
    removed = client.put(
        "/api/mx/rids",
        json={"rids": [111], "version": after_add.json()["version"]},
    )
    revoked = client.get("/api/mx/events", params={"authorization": "revoked"})
    stopped = client.post("/api/mx/listener/stop", json={})

    assert removed.status_code == 200
    assert removed.json()["rids"] == [111]
    assert revoked.status_code == 200
    assert [item["rid"] for item in revoked.json()["events"]] == [222]
    assert event["event_id"] == revoked.json()["events"][0]["event_id"]
    assert "fixture-222" not in revoked.text
    assert "source.invalid" not in revoked.text
    assert stopped.status_code == 200
    assert manager.calls == ["start", "stop"]
