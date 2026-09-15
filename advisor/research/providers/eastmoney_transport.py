"""Bounded transport for Eastmoney's public list JSON endpoint.

The local Python HTTP stack is rejected intermittently by this endpoint while
the repository-pinned Node runtime can reach it reliably.  Keep that exception
small: callers may only use the one public endpoint, requests are bounded and
credential-free, and the helper returns ordinary response-like objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Callable, Iterable, Mapping

import requests


# Static taxonomy does not require tick-level quotes and uses Eastmoney's
# public delayed quote host, which is materially more stable for unattended
# collection.  Research never routes real-time quotes through this transport.
EASTMONEY_CLIST_ENDPOINT = "https://push2delay.eastmoney.com/api/qt/clist/get"
_ALLOWED_ENDPOINTS = frozenset({EASTMONEY_CLIST_ENDPOINT})
_DEFAULT_NODE = Path("/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node")
_DEFAULT_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "eastmoney-public-json.mjs"
_MAX_BATCH_SIZE = 200
_MAX_STDOUT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class _NodeResponse:
    _payload: object
    status_code: int = 200

    @property
    def headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json; charset=utf-8"}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class EastmoneyNodeSession:
    """A requests-like, fixed-host Node subprocess transport.

    ``get_many`` is the preferred path for taxonomy membership because one
    subprocess can apply a fixed interval across the whole batch.  ``get`` is
    retained for the paged whole-market fallback adapter and also spaces
    subprocess invocations so separate calls cannot bypass the source limit.
    """

    def __init__(
        self,
        *,
        node_path: Path | str = _DEFAULT_NODE,
        script_path: Path | str = _DEFAULT_SCRIPT,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        minimum_interval_seconds: float = 0.5,
        max_attempts: int = 3,
    ) -> None:
        if (
            not isinstance(minimum_interval_seconds, (int, float))
            or isinstance(minimum_interval_seconds, bool)
            or not math.isfinite(float(minimum_interval_seconds))
            or minimum_interval_seconds < 0.5
            or not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or not 1 <= max_attempts <= 3
        ):
            raise ValueError("Eastmoney Node transport configuration is invalid")
        self.node_path = Path(node_path)
        self.script_path = Path(script_path)
        self._runner = runner
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._sleep = sleep
        self._minimum_interval_seconds = float(minimum_interval_seconds)
        self._max_attempts = max_attempts
        self._lock = threading.Lock()
        self._last_invocation_at = 0.0

    def get(
        self,
        endpoint: str,
        *,
        params: Mapping[str, object],
        timeout: float,
        headers: Mapping[str, str] | None = None,
    ) -> _NodeResponse:
        return self.get_many(endpoint, (params,), timeout=timeout, headers=headers)[0]

    def get_many(
        self,
        endpoint: str,
        params: Iterable[Mapping[str, object]],
        *,
        timeout: float,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[_NodeResponse, ...]:
        if endpoint not in _ALLOWED_ENDPOINTS:
            raise ValueError("Eastmoney Node transport only accepts an allowlisted public list endpoint")
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(float(timeout))
            or not 0 < float(timeout) <= 30
        ):
            raise ValueError("Eastmoney Node transport timeout is invalid")
        requests_payload = tuple(_bounded_params(item) for item in params)
        if not 1 <= len(requests_payload) <= _MAX_BATCH_SIZE:
            raise ValueError("Eastmoney Node transport batch size is invalid")
        # Caller headers are deliberately not forwarded.  This prevents a
        # generic session-shaped API from becoming a credential exfiltration
        # path; the Node helper owns a fixed public browser header set.
        if headers is not None and not isinstance(headers, Mapping):
            raise ValueError("Eastmoney Node transport headers are invalid")
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Eastmoney Node transport clock must be timezone-aware")
        payload = {
            "endpoint": endpoint,
            "requests": [{"params": item} for item in requests_payload],
            "timeout_ms": int(float(timeout) * 1000),
            "max_attempts": self._max_attempts,
            "minimum_interval_ms": int(self._minimum_interval_seconds * 1000),
        }
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > 512 * 1024:
            raise ValueError("Eastmoney Node transport input is too large")
        command = [str(self.node_path), str(self.script_path)]
        process_timeout = min(
            600.0,
            max(
                30.0,
                len(requests_payload) * self._minimum_interval_seconds
                + float(timeout) * self._max_attempts
                + 30.0,
            ),
        )
        try:
            with self._lock:
                elapsed = self._monotonic() - self._last_invocation_at
                if elapsed < self._minimum_interval_seconds:
                    self._sleep(self._minimum_interval_seconds - elapsed)
                self._last_invocation_at = self._monotonic()
                completed = self._runner(
                    command,
                    input=serialized,
                    text=True,
                    capture_output=True,
                    timeout=process_timeout,
                    check=False,
                )
        except (OSError, subprocess.SubprocessError) as error:
            raise requests.ConnectionError("Eastmoney public JSON transport failed") from error
        stdout = completed.stdout if isinstance(completed.stdout, str) else ""
        if completed.returncode != 0:
            raise requests.ConnectionError("Eastmoney public JSON transport was unavailable")
        if not stdout or len(stdout.encode("utf-8")) > _MAX_STDOUT_BYTES:
            raise requests.ConnectionError("Eastmoney public JSON transport output is invalid")
        try:
            decoded = json.loads(stdout)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise requests.ConnectionError("Eastmoney public JSON transport output is not JSON") from error
        responses = decoded.get("responses") if isinstance(decoded, dict) else None
        if not isinstance(responses, list) or len(responses) != len(requests_payload):
            raise requests.ConnectionError("Eastmoney public JSON transport response count is invalid")
        return tuple(_NodeResponse(item) for item in responses)


def _bounded_params(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping) or not 1 <= len(value) <= 30:
        raise ValueError("Eastmoney public JSON params are invalid")
    result: dict[str, object] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not raw_key or len(raw_key) > 80:
            raise ValueError("Eastmoney public JSON param key is invalid")
        if isinstance(raw_value, bool):
            normalized: object = raw_value
        elif isinstance(raw_value, int):
            normalized = raw_value
        elif isinstance(raw_value, float):
            if not math.isfinite(raw_value):
                raise ValueError("Eastmoney public JSON param is not finite")
            normalized = raw_value
        elif isinstance(raw_value, str) and len(raw_value) <= 2_000:
            normalized = raw_value
        else:
            raise ValueError("Eastmoney public JSON param value is invalid")
        result[raw_key] = normalized
    return result


__all__ = ["EASTMONEY_CLIST_ENDPOINT", "EastmoneyNodeSession"]
