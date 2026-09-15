"""Long-running owner of Market Daily requests and the 21:00 schedule."""

from __future__ import annotations

import logging
import signal
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from advisor.market_daily.catch_up import CatchUpError, CatchUpWorkflow
from advisor.market_daily.cold_start import ColdStartError, ColdStartWorkflow
from advisor.market_daily.control import (
    MarketDailyControlPlane,
    MarketDailyRequest,
    MarketDailyRun,
    bounded_error_message,
)
from advisor.market_daily.sessions import SessionObservationError
from advisor.market_daily.universe import UniverseError


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DUE_AT = clock_time(21, 0)
_MAX_DEGRADED_POLL_SECONDS = 300.0


class _ColdStartWorkflow(Protocol):
    def execute_claimed(self, request: MarketDailyRequest, owner_id: str, now: datetime) -> MarketDailyRun: ...

    def resume_partial(self, owner_id: str, now: datetime) -> MarketDailyRun | None: ...


class _CatchUpWorkflow(Protocol):
    def submit_due(self, now: datetime) -> MarketDailyRequest | None: ...

    def execute_claimed(self, request: MarketDailyRequest, owner_id: str, now: datetime) -> MarketDailyRun | None: ...

    def resume_partial(self, owner_id: str, now: datetime) -> MarketDailyRun | None: ...


@dataclass(frozen=True)
class ServiceTick:
    status: str
    action: str
    request_id: str | None = None
    run_id: str | None = None
    message: str | None = None
    recovered_runs: int = 0


class MarketDailyService:
    """One process owns the queue and internal clock; it never opens a port."""

    def __init__(
        self,
        control: MarketDailyControlPlane,
        cold_start: ColdStartWorkflow | _ColdStartWorkflow,
        catch_up: CatchUpWorkflow | _CatchUpWorkflow,
        *,
        owner_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
        poll_seconds: float = 15.0,
        lease_seconds: int = 120,
    ) -> None:
        if not isinstance(poll_seconds, (int, float)) or poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive integer")
        self._control = control
        self._cold_start = cold_start
        self._catch_up = catch_up
        self.owner_id = owner_id or f"market-daily-{uuid.uuid4().hex[:12]}"
        self._clock = clock or (lambda: datetime.now(tz=_SHANGHAI))
        self._sleep = sleep
        self._logger = logger or logging.getLogger("advisor.market_daily.service")
        self.poll_seconds = float(poll_seconds)
        self.lease_seconds = lease_seconds
        self._stopping = False
        self._automatic_due_day: date | None = None
        # A partial Run already contains bounded per-provider attempts.  A
        # long-running service may try that Run once on startup and then at
        # most once on each later service day; it must not turn a persistent
        # source gap into a request storm on every poll tick.
        self._partial_attempt_day: dict[str, date] = {}

    def request_stop(self) -> None:
        """Stop claiming work after the current short transaction returns."""

        self._stopping = True

    def tick(self) -> ServiceTick:
        now = _shanghai_now(self._clock())
        if self._stopping:
            return ServiceTick("stopping", "not_claiming")
        if not self._control.acquire_lease(self.owner_id, now, lease_seconds=self.lease_seconds):
            return ServiceTick("standby", "lease_held_elsewhere")
        recovered = self._control.recover_expired_work(now, lease_seconds=self.lease_seconds)

        # A run that already crossed its scheduling boundary must survive a
        # restart across midnight.  Recovery changes an abandoned ``running``
        # run back to ``pending``; resume that durable work before applying the
        # 21:00 gate to *new* requests and automatic planning.
        incomplete = self._control.latest_run(statuses=("pending", "partial"))
        if incomplete is not None:
            if incomplete.status == "pending" or self._partial_retry_allowed(incomplete.run_type, now):
                if incomplete.run_type == "cold_start":
                    resumed = self._resume_partial_cold_start(now, recovered)
                else:
                    resumed = self._resume_partial_catch_up(now, recovered)
                if resumed is not None:
                    return resumed
        recovered_request = self._control.claim_recovered_request(self.owner_id, now)
        if recovered_request is not None:
            return self._execute_claimed(recovered_request, now, recovered)
        if now.time() < _DUE_AT:
            return ServiceTick("waiting", "before_21_00", recovered_runs=recovered)

        if self._partial_retry_allowed("cold_start", now):
            partial_cold = self._resume_partial_cold_start(now, recovered)
            if partial_cold is not None:
                return partial_cold

        claimed = self._control.claim_next_request(self.owner_id, now)
        if claimed is not None:
            return self._execute_claimed(claimed, now, recovered)

        if self._partial_retry_allowed("catch_up", now):
            partial_catch_up = self._resume_partial_catch_up(now, recovered)
            if partial_catch_up is not None:
                return partial_catch_up

        # A service may poll every few seconds after 21:00.  Automatic
        # planning is a once-per-day action; work submitted explicitly still
        # passes through the request queue above on every tick.  On process
        # restart the durable Catch-up idempotency key remains the second
        # guard against duplicate ingestion.
        if self._automatic_due_day == now.date():
            return ServiceTick("idle", "up_to_date", recovered_runs=recovered)

        try:
            request = self._catch_up.submit_due(now)
        except CatchUpError as error:
            # No complete cold baseline is an expected idle state until the
            # operator submits a cold-start intent.
            self._automatic_due_day = now.date()
            return ServiceTick("waiting_for_cold_start", "no_complete_baseline", message=_message(error), recovered_runs=recovered)
        except _RECOVERABLE_ERRORS as error:
            return ServiceTick("degraded", "schedule_unavailable", message=_message(error), recovered_runs=recovered)
        self._automatic_due_day = now.date()
        if request is None:
            return ServiceTick("idle", "up_to_date", recovered_runs=recovered)
        claimed = self._control.claim_next_request(self.owner_id, now)
        if claimed is None:
            return ServiceTick("idle", "request_claimed_elsewhere", recovered_runs=recovered)
        return self._execute_claimed(claimed, now, recovered)

    def run_forever(self, *, max_ticks: int | None = None) -> None:
        """Poll cheaply while idle; graceful signals release only this lease."""

        previous_handlers = _install_stop_handlers(self.request_stop)
        ticks = 0
        next_sleep = self.poll_seconds
        try:
            while not self._stopping and (max_ticks is None or ticks < max_ticks):
                result = self.tick()
                self._log(result)
                ticks += 1
                if not self._stopping and (max_ticks is None or ticks < max_ticks):
                    self._sleep(next_sleep)
                    next_sleep = (
                        min(next_sleep * 2, _MAX_DEGRADED_POLL_SECONDS)
                        if result.status == "degraded"
                        else self.poll_seconds
                    )
        finally:
            _restore_stop_handlers(previous_handlers)
            self._control.release_lease(self.owner_id)

    def status(self) -> dict[str, object]:
        now = _shanghai_now(self._clock())
        lease = self._control.lease()
        latest = self._control.latest_run()
        return {
            "service_status": "running" if lease and lease[2] > now else "offline",
            "lease_owner": lease[0] if lease and lease[2] > now else None,
            "lease_expires_at": lease[2].isoformat() if lease else None,
            "latest_run_id": latest.run_id if latest else None,
            "latest_run_status": latest.status if latest else None,
            "next_scheduled_at": _next_due(now).isoformat(),
        }

    def _resume_partial_cold_start(self, now: datetime, recovered: int) -> ServiceTick | None:
        self._partial_attempt_day["cold_start"] = now.date()
        try:
            run = self._cold_start.resume_partial(self.owner_id, now)
        except _RECOVERABLE_ERRORS as error:
            return ServiceTick("degraded", "resume_cold_start_failed", message=_message(error), recovered_runs=recovered)
        if run is None:
            return None
        status = "running" if run.status in {"pending", "running"} else run.status
        return ServiceTick(status, "resumed_cold_start", run_id=run.run_id, recovered_runs=recovered)

    def _resume_partial_catch_up(self, now: datetime, recovered: int) -> ServiceTick | None:
        self._partial_attempt_day["catch_up"] = now.date()
        try:
            run = self._catch_up.resume_partial(self.owner_id, now)
        except _RECOVERABLE_ERRORS as error:
            return ServiceTick("degraded", "resume_catch_up_failed", message=_message(error), recovered_runs=recovered)
        if run is None:
            return None
        status = "running" if run.status in {"pending", "running"} else run.status
        return ServiceTick(status, "resumed_catch_up", run_id=run.run_id, recovered_runs=recovered)

    def _execute_claimed(self, request: MarketDailyRequest, now: datetime, recovered: int) -> ServiceTick:
        try:
            if request.request_type == "cold_start":
                run = self._cold_start.execute_claimed(request, self.owner_id, now)
            else:
                run = self._catch_up.execute_claimed(request, self.owner_id, now)
        except _RECOVERABLE_ERRORS as error:
            try:
                self._control.release_request_claim(
                    request.request_id, self.owner_id, now, message=_message(error)
                )
            except Exception:
                # The workflow may already have created a durable Run.  That
                # Run is recovered by the next lease holder; do not turn a
                # contained provider error into a service crash here.
                pass
            return ServiceTick("degraded", "request_deferred", request.request_id, message=_message(error), recovered_runs=recovered)
        if run is None:
            return ServiceTick("idle", "no_op", request.request_id, recovered_runs=recovered)
        if run.status == "partial":
            self._partial_attempt_day[run.run_type] = now.date()
        status = "running" if run.status in {"pending", "running"} else run.status
        return ServiceTick(status, f"executed_{request.request_type}", request.request_id, run.run_id, recovered_runs=recovered)

    def _partial_retry_allowed(self, run_type: str, now: datetime) -> bool:
        return self._partial_attempt_day.get(run_type) != now.date()

    def _log(self, result: ServiceTick) -> None:
        self._logger.info(
            "market_daily status=%s action=%s request=%s run=%s recovered=%d message=%s",
            result.status,
            result.action,
            result.request_id or "-",
            result.run_id or "-",
            result.recovered_runs,
            (result.message or "-")[:320],
        )


_RECOVERABLE_ERRORS = (ColdStartError, CatchUpError, SessionObservationError, UniverseError, OSError, ValueError)


def _shanghai_now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Market Daily service clock must be timezone-aware")
    return value.astimezone(_SHANGHAI)


def _next_due(now: datetime) -> datetime:
    candidate = now.replace(hour=21, minute=0, second=0, microsecond=0)
    return candidate if now < candidate else candidate + timedelta(days=1)


def _message(error: Exception) -> str:
    return bounded_error_message(str(error), fallback=f"{type(error).__name__}：服务暂缓")


def _install_stop_handlers(callback: Callable[[], None]) -> dict[int, object]:
    previous: dict[int, object] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda _signum, _frame: callback())
        except (ValueError, OSError):
            # Signal handlers cannot be installed outside the main thread.
            continue
    return previous


def _restore_stop_handlers(previous: dict[int, object]) -> None:
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (ValueError, OSError):
            continue
