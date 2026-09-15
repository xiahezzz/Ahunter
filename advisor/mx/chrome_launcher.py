"""Explicit local launcher for the operator-owned dedicated MX Chrome."""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener


_CHROME_EXECUTABLE = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
_DEBUG_HOST = "127.0.0.1"
_DEBUG_PORT = 9333
_MAX_VERSION_BYTES = 64 * 1024
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class ChromeLaunchError(RuntimeError):
    """A bounded failure whose code is safe for control-flow and tests."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ChromeLaunchResult:
    changed: bool
    ready: bool


class ChromeLauncher(Protocol):
    def start(self) -> ChromeLaunchResult: ...


class _EndpointState(Enum):
    ABSENT = "absent"
    READY = "ready"
    OCCUPIED = "occupied"


class DedicatedChromeLauncher:
    """Start one fixed isolated Chrome without navigating or handling login."""

    def __init__(
        self,
        *,
        chrome_executable: Path = _CHROME_EXECUTABLE,
        profile_directory: Path | None = None,
        endpoint_probe: Callable[[], _EndpointState] | None = None,
        process_launcher: Callable[[tuple[str, ...]], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        startup_timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 0.2,
    ) -> None:
        if startup_timeout_seconds < 0 or poll_interval_seconds <= 0:
            raise ValueError("invalid Chrome launcher timing")
        self._chrome_executable = Path(chrome_executable)
        self._profile_directory = (
            Path(profile_directory)
            if profile_directory is not None
            else Path.home() / ".chrome-mx-debug-profile"
        )
        self._endpoint_probe = endpoint_probe or _probe_endpoint
        self._process_launcher = process_launcher or _spawn_chrome
        self._monotonic = monotonic
        self._sleep = sleep
        self._startup_timeout_seconds = startup_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._lock = threading.Lock()

    def start(self) -> ChromeLaunchResult:
        with self._lock:
            initial = self._safe_probe()
            if initial is _EndpointState.READY:
                return ChromeLaunchResult(changed=False, ready=True)
            if initial is _EndpointState.OCCUPIED:
                raise ChromeLaunchError("port_in_use")

            self._validate_executable()
            self._prepare_profile_directory()
            arguments = (
                str(self._chrome_executable),
                f"--remote-debugging-address={_DEBUG_HOST}",
                f"--remote-debugging-port={_DEBUG_PORT}",
                f"--user-data-dir={self._profile_directory}",
                "--no-first-run",
                "--no-default-browser-check",
            )
            try:
                self._process_launcher(arguments)
            except OSError as error:
                raise ChromeLaunchError("unavailable") from error

            deadline = self._monotonic() + self._startup_timeout_seconds
            while True:
                if self._safe_probe() is _EndpointState.READY:
                    return ChromeLaunchResult(changed=True, ready=True)
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    return ChromeLaunchResult(changed=True, ready=False)
                self._sleep(min(self._poll_interval_seconds, remaining))

    def _safe_probe(self) -> _EndpointState:
        try:
            state = self._endpoint_probe()
        except (OSError, ValueError):
            return _EndpointState.OCCUPIED
        if not isinstance(state, _EndpointState):
            raise ChromeLaunchError("probe_invalid")
        return state

    def _validate_executable(self) -> None:
        try:
            metadata = self._chrome_executable.lstat()
        except OSError as error:
            raise ChromeLaunchError("unavailable") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ChromeLaunchError("unavailable")
        if not os.access(self._chrome_executable, os.X_OK):
            raise ChromeLaunchError("unavailable")

    def _prepare_profile_directory(self) -> None:
        try:
            os.mkdir(self._profile_directory, mode=0o700)
        except FileExistsError:
            pass
        except OSError as error:
            raise ChromeLaunchError("profile_unavailable") from error

        flags = os.O_RDONLY
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._profile_directory, flags)
        except OSError as error:
            raise ChromeLaunchError("profile_unavailable") from error
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise ChromeLaunchError("profile_unavailable")
            os.fchmod(descriptor, 0o700)
        except OSError as error:
            raise ChromeLaunchError("profile_unavailable") from error
        finally:
            os.close(descriptor)


def _spawn_chrome(arguments: tuple[str, ...]) -> None:
    subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


def _probe_endpoint() -> _EndpointState:
    try:
        connection = socket.create_connection((_DEBUG_HOST, _DEBUG_PORT), timeout=0.25)
    except OSError:
        return _EndpointState.ABSENT
    else:
        connection.close()

    opener = build_opener(ProxyHandler({}))
    request = Request(
        f"http://{_DEBUG_HOST}:{_DEBUG_PORT}/json/version",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with opener.open(request, timeout=0.5) as response:
            if response.status != 200:
                return _EndpointState.OCCUPIED
            body = response.read(_MAX_VERSION_BYTES + 1)
    except OSError:
        return _EndpointState.OCCUPIED
    if len(body) > _MAX_VERSION_BYTES:
        return _EndpointState.OCCUPIED
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return _EndpointState.OCCUPIED
    if not isinstance(payload, dict):
        return _EndpointState.OCCUPIED
    browser = payload.get("Browser")
    websocket_url = payload.get("webSocketDebuggerUrl")
    if not isinstance(browser, str) or not browser.startswith("Chrome/"):
        return _EndpointState.OCCUPIED
    if not isinstance(websocket_url, str):
        return _EndpointState.OCCUPIED
    try:
        parsed = urlparse(websocket_url)
        endpoint_is_expected = (
            parsed.scheme == "ws"
            and parsed.hostname in _LOOPBACK_HOSTS
            and parsed.port == _DEBUG_PORT
            and parsed.path.startswith("/devtools/browser/")
        )
    except ValueError:
        return _EndpointState.OCCUPIED
    return _EndpointState.READY if endpoint_is_expected else _EndpointState.OCCUPIED
