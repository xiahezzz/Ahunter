"""One process-wide request-start limiter for every default Sina adapter."""

from __future__ import annotations

import math
import threading
import time
from typing import Callable


class SinaRateLimiter:
    """Thread-safe fixed-spacing limiter expressed as requests per second."""

    def __init__(
        self,
        *,
        requests_per_second: float = 2.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if (
            isinstance(requests_per_second, bool)
            or not isinstance(requests_per_second, (int, float))
            or not math.isfinite(float(requests_per_second))
            or requests_per_second <= 0
        ):
            raise ValueError("requests_per_second must be a positive finite number")
        self.requests_per_second = float(requests_per_second)
        self._interval = 1.0 / self.requests_per_second
        self._monotonic = monotonic
        self._sleep = sleep
        self._next_allowed = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self._monotonic()
            wait = max(0.0, self._next_allowed - now)
            if wait:
                self._sleep(wait)
                now = self._monotonic()
            self._next_allowed = max(now, self._next_allowed) + self._interval


PROCESS_SINA_LIMITER = SinaRateLimiter()


__all__ = ["PROCESS_SINA_LIMITER", "SinaRateLimiter"]
