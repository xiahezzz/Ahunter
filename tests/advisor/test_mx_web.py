from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from advisor.mx.chrome_launcher import ChromeLaunchResult
from advisor.services.contracts import ServiceStatus
from advisor.web.api import create_app


class _FakeMxManager:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.loaded = False

    def mx_listener_status(self, _now: datetime) -> ServiceStatus:
        return ServiceStatus(
            "mx-listener",
            "运行中" if self.loaded else "未加载",
            "正在等待用户提供已开启调试的 Chrome" if self.loaded else "MX Listener 未加载",
            ("/fixture/log.out",),
            {
                "launchagent_loaded": self.loaded,
                "liveness": "live" if self.loaded else "offline",
                "readiness": "waiting_for_chrome" if self.loaded else "stopping",
                "health": "healthy" if self.loaded else "failed",
                "reason_code": "waiting_for_chrome" if self.loaded else "not_started",
                "connected_at": None,
                "last_frame_at": None,
                "last_accepted_event_at": None,
                "lease_expires_at": None,
                "rid_count": 1,
                "collection_enabled": True,
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


class _FakeChromeLauncher:
    def __init__(self) -> None:
        self.calls = 0
        self.result = ChromeLaunchResult(changed=True, ready=True)

    def start(self) -> ChromeLaunchResult:
        self.calls += 1
        return self.result


def _client(tmp_path: Path) -> tuple[TestClient, _FakeMxManager, _FakeChromeLauncher]:
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    allowed = root / "config" / "allowed-rids.yaml"
    allowed.write_text("allowed_rids: [111]\n", encoding="utf-8")
    manager = _FakeMxManager()
    chrome_launcher = _FakeChromeLauncher()
    app = create_app(
        state_dir=tmp_path / "state",
        db_path=tmp_path / "advisor.sqlite",
        research_root=root,
        allowed_rids_path=allowed,
        service_manager_factory=lambda **_kwargs: manager,
        chrome_launcher_factory=lambda: chrome_launcher,
    )
    return TestClient(app), manager, chrome_launcher


def test_mx_listener_status_and_lifecycle_routes_only_use_the_service_set_manager(tmp_path: Path):
    client, manager, chrome_launcher = _client(tmp_path)

    initial = client.get("/api/mx/listener/status")
    started = client.post("/api/mx/listener/start", json={})
    stopped = client.post("/api/mx/listener/stop", json={})

    assert initial.status_code == 200
    assert initial.json()["liveness"] == "offline"
    assert initial.json()["readiness"] == "stopping"
    assert initial.json()["health"] == "failed"
    assert initial.json()["rid_count"] == 1
    assert started.status_code == 200
    assert started.json()["changed"] is True
    assert started.json()["service"]["readiness"] == "waiting_for_chrome"
    assert "不会打开、登录或操作 Chrome 页面" in started.json()["message"]
    assert stopped.status_code == 200
    assert stopped.json()["changed"] is True
    assert manager.calls == ["start", "stop"]
    assert chrome_launcher.calls == 0
    assert "fixture/log" not in str(initial.json())
    assert "127.0.0.1" not in str(started.json())


def test_mx_listener_lifecycle_routes_require_empty_local_json_requests(tmp_path: Path):
    client, manager, chrome_launcher = _client(tmp_path)

    bad_shape = client.post("/api/mx/listener/start", json={"unexpected": True})
    cross_origin = client.post(
        "/api/mx/listener/start",
        json={},
        headers={"Origin": "https://example.invalid"},
    )
    bad_type = client.post("/api/mx/listener/stop", content=b"{}", headers={"Content-Type": "text/plain"})

    assert [response.status_code for response in (bad_shape, cross_origin, bad_type)] == [400, 400, 400]
    assert manager.calls == []
    assert chrome_launcher.calls == 0
    assert all(len(response.json()["detail"]) <= 80 for response in (bad_shape, cross_origin, bad_type))


def test_mx_chrome_start_is_explicit_idempotent_and_returns_only_safe_aggregate_state(tmp_path: Path):
    client, manager, chrome_launcher = _client(tmp_path)

    started = client.post("/api/mx/chrome/start", json={})
    chrome_launcher.result = ChromeLaunchResult(changed=False, ready=True)
    repeated = client.post("/api/mx/chrome/start", json={})

    assert started.status_code == 200
    assert started.json() == {
        "changed": True,
        "ready": True,
        "message": "专用 Chrome 已启动；请在其中自行登录并打开 MX 页面",
    }
    assert repeated.status_code == 200
    assert repeated.json()["changed"] is False
    assert repeated.json()["ready"] is True
    assert chrome_launcher.calls == 2
    assert manager.calls == []
    assert all(
        sensitive not in started.text + repeated.text
        for sensitive in ("9333", "127.0.0.1", "chrome-mx-debug-profile", "devtools/browser")
    )


def test_mx_chrome_start_requires_an_empty_local_json_request(tmp_path: Path):
    client, manager, chrome_launcher = _client(tmp_path)

    bad_shape = client.post("/api/mx/chrome/start", json={"url": "https://example.invalid"})
    cross_origin = client.post(
        "/api/mx/chrome/start",
        json={},
        headers={"Origin": "https://example.invalid"},
    )
    bad_type = client.post(
        "/api/mx/chrome/start",
        content=b"{}",
        headers={"Content-Type": "text/plain"},
    )

    assert [response.status_code for response in (bad_shape, cross_origin, bad_type)] == [400, 400, 400]
    assert chrome_launcher.calls == 0
    assert manager.calls == []
    assert all(len(response.json()["detail"]) <= 80 for response in (bad_shape, cross_origin, bad_type))
