"""One narrow status and lifecycle surface for the independent service set."""

from __future__ import annotations

import os
import plistlib
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from advisor.market_daily.control import MarketDailyControlPlane
from advisor.mx.listener_control import ListenerControlUnavailable, read_listener_control_snapshot
from advisor.mx.rid_authorization import (
    RidAuthorizationStore,
    RidAuthorizationUnavailable,
    RidAuthorizationValidationError,
)
from advisor.scheduler.launchd import render_launchd_template, validate_launchd_template
from advisor.services.contracts import ServiceDescriptor, ServiceStatus


_SHANGHAI = ZoneInfo("Asia/Shanghai")
MARKET_DAILY = ServiceDescriptor(
    "market-daily",
    "com.ahunter.market-daily",
    "Market Daily 行情服务",
    ("logs/market-daily.out.log", "logs/market-daily.err.log"),
    True,
)
MX_LISTENER = ServiceDescriptor(
    "mx-listener",
    "com.ahunter.mx-listener",
    "MX Listener 监听服务",
    ("logs/mx-listener.out.log", "logs/mx-listener.err.log"),
    True,
)
RESEARCH = ServiceDescriptor(
    "research",
    "com.ahunter.research",
    "Research 研究执行服务",
    ("logs/research.out.log", "logs/research.err.log"),
    True,
)


class ServiceSetManager:
    """Manage launchd only; MX truth always comes from its event control plane."""

    def __init__(
        self,
        *,
        root: Path,
        database_path: Path,
        events_database_path: Path | None = None,
        allowed_rids_path: Path | None = None,
        launch_agents_dir: Path | None = None,
        runner: Callable[[list[str]], object] | None = None,
        cdp_opener: Callable[[str], object] | None = None,
        cdp_port: int = 9333,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.database_path = database_path.expanduser().resolve()
        # Keep the final paths un-resolved so their readers can fail closed on
        # a symlink rather than silently treating its target as authoritative.
        self.events_database_path = Path(events_database_path or self.root / "data/state/events.sqlite").expanduser()
        self.allowed_rids_path = Path(allowed_rids_path or self.root / "config/allowed-rids.yaml").expanduser()
        self.launch_agents_dir = (launch_agents_dir or Path.home() / "Library" / "LaunchAgents").expanduser()
        self._runner = runner or _run
        self._sleep = sleep
        self._monotonic = monotonic
        # Kept as ignored compatibility inputs for existing callers.  Service
        # status must never probe CDP or infer browser readiness itself.
        self._cdp_opener = cdp_opener
        self._cdp_port = cdp_port

    @property
    def descriptors(self) -> tuple[ServiceDescriptor, ...]:
        return (MARKET_DAILY, MX_LISTENER, RESEARCH)

    def statuses(self, now: datetime | None = None) -> tuple[ServiceStatus, ...]:
        current = (now or datetime.now(tz=_SHANGHAI)).astimezone(_SHANGHAI)
        return (self.market_daily_status(current), self.mx_listener_status(current), self.research_status(current))

    def market_daily_status(self, now: datetime) -> ServiceStatus:
        if self.database_path.is_symlink() or not self.database_path.is_file():
            return ServiceStatus(
                MARKET_DAILY.service_id,
                "不可用",
                "本地 Market Daily 数据库不可读取",
                self._absolute_logs(MARKET_DAILY),
                {"launchagent_loaded": self._is_loaded(MARKET_DAILY.label or "")},
            )
        try:
            control = MarketDailyControlPlane(self.database_path, ensure_schema=False)
            lease = control.lease()
            latest = control.latest_run()
        except Exception:
            return ServiceStatus(
                MARKET_DAILY.service_id,
                "不可用",
                "本地 Market Daily 状态无法读取",
                self._absolute_logs(MARKET_DAILY),
                {"launchagent_loaded": self._is_loaded(MARKET_DAILY.label or "")},
            )
        loaded = self._is_loaded(MARKET_DAILY.label or "")
        lease_active = lease is not None and lease[2] > now
        status = "运行中" if loaded and lease_active else "待命" if loaded else "未加载"
        details = "服务已持有有效租约" if lease_active else "未观察到有效运行租约"
        return ServiceStatus(
            MARKET_DAILY.service_id,
            status,
            details,
            self._absolute_logs(MARKET_DAILY),
            {
                "launchagent_loaded": loaded,
                "lease_owner": lease[0] if lease_active and lease else None,
                "lease_expires_at": lease[2].isoformat() if lease else None,
                "latest_run_id": latest.run_id if latest else None,
                "latest_run_status": latest.status if latest else None,
                "target_session": latest.target_session.isoformat() if latest else None,
            },
        )

    def mx_listener_status(self, now: datetime) -> ServiceStatus:
        loaded = self._is_loaded(MX_LISTENER.label or "")
        try:
            control = read_listener_control_snapshot(self.events_database_path, now=now)
        except (ListenerControlUnavailable, OSError, ValueError):
            control = None
        try:
            authorization = RidAuthorizationStore(self.allowed_rids_path).read()
            rid_count = len(authorization.rids)
            collection_enabled = authorization.collection_enabled
            rid_config_valid = True
        except (RidAuthorizationUnavailable, RidAuthorizationValidationError):
            rid_count = 0
            collection_enabled = False
            rid_config_valid = False

        if control is None:
            status, details = ("待命", "MX Listener 控制面暂不可读取") if loaded else ("未加载", "MX Listener 未加载")
            extra = {
                "liveness": "offline",
                "readiness": "stopping",
                "health": "failed",
                "reason_code": "control_unavailable",
                "connected_at": None,
                "last_frame_at": None,
                "last_accepted_event_at": None,
                "lease_expires_at": None,
            }
        else:
            status = "运行中" if loaded and control.liveness == "live" else "待命" if loaded else "未加载"
            details = _mx_status_details(control.liveness, control.readiness, control.health)
            extra = {
                "liveness": control.liveness,
                "readiness": control.readiness,
                "health": control.health,
                "reason_code": control.reason_code,
                "connected_at": control.connected_at,
                "last_frame_at": control.last_frame_at,
                "last_accepted_event_at": control.last_accepted_event_at,
                "lease_expires_at": control.lease_expires_at,
            }
        return ServiceStatus(
            MX_LISTENER.service_id,
            status,
            details,
            self._absolute_logs(MX_LISTENER),
            {
                "launchagent_loaded": loaded,
                "rid_count": rid_count,
                "collection_enabled": collection_enabled,
                "rid_config_valid": rid_config_valid,
                **extra,
            },
        )

    def research_status(self, now: datetime) -> ServiceStatus:
        """Read the durable Research lease without starting or migrating it."""
        loaded = self._is_loaded(RESEARCH.label or "")
        if self.database_path.is_symlink() or not self.database_path.is_file():
            return ServiceStatus(
                RESEARCH.service_id,
                "不可用",
                "本地 Research 数据库不可读取",
                self._absolute_logs(RESEARCH),
                {"launchagent_loaded": loaded, "state": "offline", "queued_count": 0, "active_request_id": None},
            )
        try:
            connection = sqlite3.connect(f"file:{self.database_path}?mode=ro", uri=True)
            try:
                lease = connection.execute(
                    "SELECT heartbeat_at, expires_at FROM research_service_leases WHERE lease_name = 'research-service'"
                ).fetchone()
                running = connection.execute(
                    "SELECT request_id FROM research_requests WHERE status = 'running' ORDER BY claimed_at, request_id LIMIT 1"
                ).fetchone()
                queued = connection.execute(
                    "SELECT COUNT(*) FROM research_requests WHERE status = 'queued'"
                ).fetchone()
            finally:
                connection.close()
            expires_at = datetime.fromisoformat(lease[1]) if lease else None
            online = bool(expires_at and expires_at > now)
            heartbeat = lease[0] if online and lease else None
            active = str(running[0]) if online and running else None
            queued_count = int(queued[0]) if queued else 0
        except (sqlite3.Error, TypeError, ValueError):
            return ServiceStatus(
                RESEARCH.service_id,
                "不可用",
                "Research 控制面暂不可读取",
                self._absolute_logs(RESEARCH),
                {"launchagent_loaded": loaded, "state": "degraded", "queued_count": 0, "active_request_id": None},
            )
        if online and active:
            status, details, state = "运行中", "Research Service 正在执行唯一的已认领请求", "running"
        elif online:
            status, details, state = "待命", "Research Service 在线，正在等待队列", "idle"
        elif loaded:
            status, details, state = "待命", "LaunchAgent 已加载，但未观察到有效 Research 租约", "offline"
        else:
            status, details, state = "未加载", "Research Service 未加载", "offline"
        return ServiceStatus(
            RESEARCH.service_id,
            status,
            details,
            self._absolute_logs(RESEARCH),
            {
                "launchagent_loaded": loaded,
                "state": state,
                "queued_count": queued_count,
                "active_request_id": active,
                "heartbeat_at": heartbeat,
            },
        )

    def install_market_daily(self) -> Path:
        template = self.root / "config" / "launchd" / "com.ahunter.market-daily.plist.template"
        if not validate_launchd_template(template):
            raise ValueError("Market Daily LaunchAgent 模板无效")
        runtime_python = self.root / ".venv-runtime" / "bin" / "python"
        if not runtime_python.is_file() or runtime_python.is_symlink():
            raise ValueError("Market Daily 运行环境 Python 不可用")
        return self._install_template(MARKET_DAILY, template, runtime_python)

    def install_mx_listener(self) -> Path:
        template = self.root / "config" / "launchd" / "com.ahunter.mx-listener.plist.template"
        if not validate_launchd_template(template):
            raise ValueError("MX Listener LaunchAgent 模板无效")
        # The Node template does not interpolate PYTHON; pass a stable path to
        # keep one renderer for all project-owned templates without requiring a
        # Python runtime to install this independent service.
        return self._install_template(MX_LISTENER, template, self.root / ".venv-runtime/bin/python")

    def install_research(self) -> Path:
        template = self.root / "config" / "launchd" / "com.ahunter.research.plist.template"
        if not validate_launchd_template(template):
            raise ValueError("Research LaunchAgent 模板无效")
        runtime_python = self.root / ".venv-runtime" / "bin" / "python"
        if not runtime_python.is_file() or runtime_python.is_symlink():
            raise ValueError("Research 运行环境 Python 不可用")
        return self._install_template(RESEARCH, template, runtime_python)

    def load_market_daily(self) -> bool:
        return self._load(MARKET_DAILY, self.install_market_daily)

    def load_mx_listener(self) -> bool:
        return self._load(MX_LISTENER, self.install_mx_listener)

    def load_research(self) -> bool:
        return self._load(RESEARCH, self.install_research)

    def unload_market_daily(self) -> bool:
        return self._unload(MARKET_DAILY)

    def unload_mx_listener(self) -> bool:
        return self._unload(MX_LISTENER)

    def unload_research(self) -> bool:
        return self._unload(RESEARCH)

    def start_market_daily(self) -> bool:
        return self._start(MARKET_DAILY, self.load_market_daily)

    def start_mx_listener(self) -> bool:
        return self._start(MX_LISTENER, self.load_mx_listener)

    def start_research(self) -> bool:
        return self._start(RESEARCH, self.load_research)

    def stop_market_daily(self) -> bool:
        return self.unload_market_daily()

    def stop_mx_listener(self) -> bool:
        changed = self.unload_mx_listener()
        self._wait_for_mx_lease_release()
        return changed

    def stop_research(self) -> bool:
        return self.unload_research()

    def _install_template(self, descriptor: ServiceDescriptor, template: Path, python: Path) -> Path:
        rendered = render_launchd_template(template, repo_root=self.root, python=python)
        payload = plistlib.loads(rendered.encode("utf-8"))
        target = self.launch_agents_dir / f"{descriptor.label}.plist"
        self.launch_agents_dir.mkdir(parents=True, exist_ok=True)
        for key in ("StandardOutPath", "StandardErrorPath"):
            Path(payload[key]).parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.read_text(encoding="utf-8") != rendered:
            target.write_text(rendered, encoding="utf-8")
        return target

    def _load(self, descriptor: ServiceDescriptor, installer: Callable[[], Path]) -> bool:
        plist = installer()
        if self._is_loaded(descriptor.label or ""):
            return False
        _require_success(self._runner(["launchctl", "bootstrap", _domain(), str(plist)]))
        return True

    def _unload(self, descriptor: ServiceDescriptor) -> bool:
        label = descriptor.label or ""
        if not self._is_loaded(label):
            return False
        _require_success(self._runner(["launchctl", "bootout", f"{_domain()}/{label}"]))
        return True

    def _start(self, descriptor: ServiceDescriptor, loader: Callable[[], bool]) -> bool:
        loaded_now = loader()
        _require_success(self._runner(["launchctl", "kickstart", "-k", f"{_domain()}/{descriptor.label}"]))
        return loaded_now

    def _wait_for_mx_lease_release(self, timeout_seconds: float = 30.0) -> None:
        deadline = self._monotonic() + timeout_seconds
        while True:
            try:
                snapshot = read_listener_control_snapshot(
                    self.events_database_path, now=datetime.now(tz=_SHANGHAI)
                )
            except ListenerControlUnavailable:
                return
            if snapshot.liveness != "live":
                return
            if self._monotonic() >= deadline:
                raise RuntimeError("MX Listener 租约未在停止期限内释放")
            self._sleep(0.1)

    def _is_loaded(self, label: str) -> bool:
        try:
            return _return_code(self._runner(["launchctl", "print", f"{_domain()}/{label}"])) == 0
        except (OSError, ValueError):
            return False

    def _absolute_logs(self, descriptor: ServiceDescriptor) -> tuple[str, ...]:
        return tuple(str(self.root / item) for item in descriptor.log_paths)


def statuses_as_dict(statuses: tuple[ServiceStatus, ...]) -> dict[str, object]:
    return {
        "服务": [
            {
                "编号": item.service_id,
                "状态": item.status,
                "说明": item.details,
                "日志": list(item.log_paths),
                **item.extra,
            }
            for item in statuses
        ]
    }


def _mx_status_details(liveness: str, readiness: str, health: str) -> str:
    if liveness != "live":
        return "未观察到有效 Listener 租约"
    if readiness == "waiting_for_chrome":
        return "正在等待用户提供已开启调试的 Chrome"
    if readiness == "waiting_for_authorization":
        return "正在等待用户已登录并打开的 MX 页面"
    if readiness == "listening" and health == "healthy":
        return "正在被动监听已授权 MX 页面"
    if health == "failed":
        return "Listener 已记录故障，等待恢复"
    if health == "degraded":
        return "Listener 正在降级运行"
    return "Listener 正在连接或停止"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, text=True, capture_output=True)


def _return_code(result: object) -> int:
    return int(getattr(result, "returncode", 1))


def _require_success(result: object) -> None:
    if _return_code(result) != 0:
        raise RuntimeError("launchctl 操作失败")
