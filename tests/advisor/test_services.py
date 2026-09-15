import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from advisor.market_daily.control import MarketDailyControlPlane
from advisor.scheduler.launchd import render_launchd_template, validate_launchd_template
from advisor.services.manager import ServiceSetManager, statuses_as_dict


def test_market_daily_launchagent_template_is_keepalive_with_no_calendar_trigger():
    template = Path("config/launchd/com.ahunter.market-daily.plist.template")

    assert validate_launchd_template(template)
    rendered = render_launchd_template(template, repo_root=Path("/repo"), python=Path("/repo/.venv-runtime/bin/python"))
    assert "StartCalendarInterval" not in rendered
    assert "/repo/.venv-runtime/bin/python" in rendered
    assert "advisor.market_daily.cli</string>" in rendered
    assert "service</string>" in rendered


def test_service_set_reports_mx_control_plane_without_probing_cdp_or_returning_debug_url(tmp_path):
    root = tmp_path / "repo"
    template_dir = root / "config" / "launchd"
    template_dir.mkdir(parents=True)
    template_dir.joinpath("com.ahunter.market-daily.plist.template").write_text(
        Path("config/launchd/com.ahunter.market-daily.plist.template").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    template_dir.joinpath("com.ahunter.mx-listener.plist.template").write_text(
        Path("config/launchd/com.ahunter.mx-listener.plist.template").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    runtime = root / ".venv-runtime" / "bin"
    runtime.mkdir(parents=True)
    runtime.joinpath("python").write_text("#!/bin/sh\n", encoding="utf-8")
    database = root / "data" / "advisor" / "advisor.sqlite"
    MarketDailyControlPlane(database)
    events_database = root / "data" / "state" / "events.sqlite"
    events_database.parent.mkdir(parents=True)
    now = datetime(2026, 8, 8, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    now_ms = int(now.timestamp() * 1000)
    connection = sqlite3.connect(events_database)
    connection.executescript(
        """
        CREATE TABLE listener_service_lease (
          singleton INTEGER PRIMARY KEY, instance_id TEXT, started_at INTEGER,
          heartbeat_at INTEGER, expires_at INTEGER
        );
        CREATE TABLE listener_service_status (
          singleton INTEGER PRIMARY KEY, readiness TEXT, health TEXT, reason_code TEXT,
          updated_at INTEGER, connected_at INTEGER, last_frame_at INTEGER,
          last_accepted_event_at INTEGER
        );
        """
    )
    connection.execute(
        "INSERT INTO listener_service_lease VALUES (1, 'listener-local', ?, ?, ?)",
        (now_ms - 1_000, now_ms - 100, now_ms + 30_000),
    )
    connection.execute(
        "INSERT INTO listener_service_status VALUES (1, 'listening', 'healthy', 'network_enabled', ?, ?, ?, ?)",
        (now_ms, now_ms - 1_000, now_ms - 100, now_ms - 50),
    )
    connection.commit()
    connection.close()
    allowed = root / "config" / "allowed-rids.yaml"
    allowed.parent.mkdir(exist_ok=True)
    allowed.write_text("allowed_rids: []\n", encoding="utf-8")
    commands = []

    def runner(command):
        commands.append(command)
        if command[:2] == ["launchctl", "print"] and command[-1].endswith("com.ahunter.mx-listener"):
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(returncode=1)

    manager = ServiceSetManager(
        root=root,
        database_path=database,
        events_database_path=events_database,
        allowed_rids_path=allowed,
        launch_agents_dir=tmp_path / "LaunchAgents",
        runner=runner,
    )
    payload = statuses_as_dict(manager.statuses(now))

    mx = next(item for item in payload["服务"] if item["编号"] == "mx-listener")
    assert mx["状态"] == "运行中"
    assert mx["liveness"] == "live"
    assert mx["readiness"] == "listening"
    assert mx["health"] == "healthy"
    assert mx["collection_enabled"] is False
    assert "127.0.0.1" not in str(payload)
    assert not any(command[0] in {"pgrep", "curl"} for command in commands)


def test_market_daily_install_is_idempotent_and_uses_runtime_python_module(tmp_path):
    root = tmp_path / "repo"
    template_dir = root / "config" / "launchd"
    template_dir.mkdir(parents=True)
    template_dir.joinpath("com.ahunter.market-daily.plist.template").write_text(
        Path("config/launchd/com.ahunter.market-daily.plist.template").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    binary = root / ".venv-runtime" / "bin"
    binary.mkdir(parents=True)
    binary.joinpath("python").write_text("#!/bin/sh\n", encoding="utf-8")
    database = root / "data" / "advisor" / "advisor.sqlite"
    manager = ServiceSetManager(
        root=root,
        database_path=database,
        launch_agents_dir=tmp_path / "LaunchAgents",
        runner=lambda _command: SimpleNamespace(returncode=1),
        cdp_opener=lambda _path: (_ for _ in ()).throw(OSError()),
    )

    first = manager.install_market_daily()
    second = manager.install_market_daily()

    assert first == second
    rendered = first.read_text(encoding="utf-8")
    assert ".venv-runtime/bin/python" in rendered
    assert "advisor.market_daily.cli" in rendered


def test_market_daily_stop_uses_a_launchctl_service_target(tmp_path):
    calls = []
    root = tmp_path / "repo"
    database = root / "data" / "advisor" / "advisor.sqlite"

    def runner(command):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    manager = ServiceSetManager(
        root=root,
        database_path=database,
        launch_agents_dir=tmp_path / "LaunchAgents",
        runner=runner,
        cdp_opener=lambda _path: (_ for _ in ()).throw(OSError()),
    )

    assert manager.stop_market_daily() is True
    assert calls[-1] == ["launchctl", "bootout", f"gui/{os.getuid()}/com.ahunter.market-daily"]


def test_mx_listener_lifecycle_targets_only_its_exact_launchagent(tmp_path):
    calls = []
    root = tmp_path / "repo"
    templates = root / "config" / "launchd"
    templates.mkdir(parents=True)
    templates.joinpath("com.ahunter.mx-listener.plist.template").write_text(
        Path("config/launchd/com.ahunter.mx-listener.plist.template").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    def runner(command):
        calls.append(command)
        return SimpleNamespace(returncode=1 if command[:2] == ["launchctl", "print"] else 0)

    manager = ServiceSetManager(
        root=root,
        database_path=root / "data" / "advisor" / "advisor.sqlite",
        launch_agents_dir=tmp_path / "LaunchAgents",
        runner=runner,
    )
    assert manager.start_mx_listener() is True
    rendered = (tmp_path / "LaunchAgents" / "com.ahunter.mx-listener.plist").read_text(encoding="utf-8")
    assert "run-mx-listener-service.mjs" in rendered
    assert "http://127.0.0.1:9333" in rendered
    assert ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.ahunter.mx-listener"] in calls
    assert all("market-daily" not in " ".join(command) for command in calls)
