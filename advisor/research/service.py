"""Singleton, durable executor for Research Requests.

The Service owns queue claiming and lifecycle projection.  It intentionally
does not expose a second queue or run a Cycle from an HTTP handler; callers
provide one idempotent request executor and retain the persisted checkpoints
owned by the Research Repository.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
import sqlite3
import threading
from typing import Callable
from uuid import uuid4

from advisor.research.contracts import ResearchBoundary, ResearchScope
from advisor.research.repository import ResearchRepository, ResearchRequest, ResearchRequestOwnershipLost
from advisor.research.work_queue import ResearchWork, next_work, read_work, recover_experiment_work
from advisor.research.experiments.records import Fenced, TERMINAL


class ResearchServiceBlocked(RuntimeError):
    def __init__(self, reason_code: str = "quality_blocked") -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class ResearchServiceCancelled(RuntimeError):
    pass


class ResearchServiceLeaseLost(RuntimeError):
    """The singleton lease moved while this worker was executing.

    Never terminally mutate the Request after this point: the new owner owns
    recovery and may already have returned it to the queue.
    """


class _CancellationSignal:
    """Callable compatibility surface plus an Event for Codex cancellation."""

    def __init__(self, service: "ResearchService", request_id: str, event: threading.Event) -> None:
        self.service = service
        self.request_id = request_id
        self.event = event

    def __call__(self) -> bool:
        return self.service._cancelled(self.request_id)


@dataclass(frozen=True)
class ServiceExecutionResult:
    status: str
    reason_code: str | None = None
    cycle_id: str | None = None
    report_json_hash: str | None = None
    report_markdown_hash: str | None = None
    published_at: datetime | None = None


@dataclass(frozen=True)
class ResearchServiceStatus:
    state: str
    active_request_id: str | None
    queued_count: int
    heartbeat_at: str | None
    reason_code: str | None = None
    active_test_id: str | None = None


ProgressCallback = Callable[[str, int, int, str | None], None]
CancelCheck = Callable[[], bool]
RequestExecutor = Callable[[ResearchRequest, ResearchBoundary, ProgressCallback, CancelCheck], ServiceExecutionResult]


class ResearchService:
    """Claims at most one Request and runs one Cycle at a time."""

    def __init__(
        self,
        repository: ResearchRepository,
        executor: RequestExecutor,
        *,
        owner_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = 30,
        heartbeat_interval_seconds: float | None = None,
        experiment_queue=None,
        experiment_runner_factory=None,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        interval = min(10.0, lease_seconds / 3)
        self.heartbeat_interval_seconds = interval if heartbeat_interval_seconds is None else heartbeat_interval_seconds
        if self.heartbeat_interval_seconds <= 0 or self.heartbeat_interval_seconds >= lease_seconds:
            raise ValueError("heartbeat interval must be positive and shorter than the lease")
        self.repository = repository
        self.executor = executor
        if (experiment_queue is None) != (experiment_runner_factory is None):
            raise ValueError("experiment queue and runner factory must be configured together")
        if experiment_queue is not None and experiment_queue.db is not repository.connection:
            raise ValueError("experiment queue must share the Research Repository connection")
        self.experiment_queue, self.experiment_runner_factory = experiment_queue, experiment_runner_factory
        self._tick_lock = threading.Lock()
        self._active_test_lease = None
        self._active_test_settings = None
        self.owner_id = owner_id or f"research-service-{os.getpid()}-{uuid4().hex[:12]}"
        # Both durable identities are per-process-instance fencing tokens.
        # A caller may reuse a human-readable owner_id after restart, but it
        # must not let two live instances renew the same lease or write the
        # same Request.
        self._lease_owner_id = f"research-lease-{uuid4().hex}"
        self._claim_owner_id = f"research-claim-{uuid4().hex}"
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lease_seconds = lease_seconds
        self._started = False
        self._stopping = False
        self._active_request_id: str | None = None
        self._active_cancel_event: threading.Event | None = None
        self._lease_lost = threading.Event()

    def start(self) -> bool:
        now = self.clock()
        with self.repository.transaction():
            acquired = self.repository.acquire_service_lease(
                self._lease_owner_id, now=now, ttl_seconds=self.lease_seconds
            )
            if not acquired:
                return False
            # A long-lived process can lose the lease, let a successor claim
            # work, and later reacquire after that successor expires. Recover
            # on every successful lease acquisition, not just this process's
            # first start, so that successor's Request is never stranded.
            self.repository.recover_interrupted_requests(self._claim_owner_id, updated_at=now)
            recover_experiment_work(self.repository.connection, service_owner_id=self._lease_owner_id,
                                    claimed_by=self._claim_owner_id, now=now)
        self._started = True
        self._stopping = False
        return True

    def stop(self) -> None:
        """Stop claiming work; an active Request cooperatively observes cancellation."""
        self._stopping = True
        if self._active_cancel_event is not None:
            self._active_cancel_event.set()

    def tick(self) -> ResearchRequest | ResearchWork | None:
        if not self._tick_lock.acquire(blocking=False):
            return None
        try:
            return self._tick()
        finally:
            self._tick_lock.release()

    def _tick(self) -> ResearchRequest | ResearchWork | None:
        if self._stopping:
            return None
        if not self.start():
            return None
        now = self.clock()
        with self.repository.transaction():
            if not self.repository.heartbeat_service_lease(
                self._lease_owner_id, now=now, ttl_seconds=self.lease_seconds
            ):
                return None
            work = next_work(self.repository.connection)
            request = (self.repository.claim_next_request(self._claim_owner_id, claimed_at=now)
                       if work is None or work.kind == "request" else None)
            if request is not None:
                self.repository.connection.execute("UPDATE research_work_queue SET service_owner_id=? WHERE work_id=?",
                                                   (self._lease_owner_id, request.request_id))
        if work is not None and work.kind == "experiment":
            if self.experiment_queue is None:
                return None  # No formal experiment executor is fabricated.
            return self._run_experiment(work)
        if request is None:
            return None
        self._active_request_id = request.request_id
        self._lease_lost.clear()
        cancel_event = threading.Event()
        self._active_cancel_event = cancel_event
        heartbeat_stop, heartbeat_thread = self._start_lease_heartbeat(request.request_id, cancel_event)
        try:
            self._run_claimed(request)
        finally:
            heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=max(1.0, self.heartbeat_interval_seconds * 2))
            self._active_request_id = None
            self._active_cancel_event = None
            if not self._lease_lost.is_set():
                with self.repository.transaction():
                    self.repository.heartbeat_service_lease(
                        self._lease_owner_id, now=self.clock(), ttl_seconds=self.lease_seconds
                    )
        return self.repository.get_request(request.request_id)

    def status(self) -> ResearchServiceStatus:
        lease = self.repository.service_lease_status(now=self.clock())
        current = self.repository.current_requests()
        queued = self.repository.connection.execute("SELECT COUNT(*) FROM research_work_queue WHERE state='queued'").fetchone()[0]
        running = next((item for item in current if item.status == "running"), None)
        active_test = self.repository.connection.execute("SELECT work_id FROM research_work_queue WHERE kind='experiment' AND state='running' LIMIT 1").fetchone()
        if self._stopping:
            state = "stopping"
        elif not lease["online"]:
            state = "offline"
        elif running is not None or active_test is not None:
            state = "running"
        else:
            state = "idle"
        return ResearchServiceStatus(
            state=state,
            active_request_id=running.request_id if running is not None else None,
            queued_count=queued,
            heartbeat_at=lease["heartbeat_at"] if isinstance(lease["heartbeat_at"], str) else None,
            reason_code=None,
            active_test_id=active_test[0] if active_test else None,
        )

    def _run_experiment(self, work):
        from advisor.research.experiments.contracts import ResolvedSpecification
        from advisor.research.experiments.episode_candidates import EpisodeCandidates
        from advisor.research.experiments.resolution import verify_specification
        records = self.experiment_queue.records
        test = records._test(work.work_id)
        definition = records.read(test["value"]["definition_id"])
        settings = verify_specification(ResolvedSpecification.model_validate(definition["value"]["specification"])).runtime
        lease = self.experiment_queue.claim(work.work_id, service_owner_id=self._lease_owner_id,
            claimed_by=self._claim_owner_id, lease_seconds=settings.lease_seconds)
        self._active_test_lease, self._active_test_settings = lease, settings
        self._lease_lost.clear()
        self._active_cancel_event = threading.Event()
        stop, thread = self._start_lease_heartbeat(work.work_id, self._active_cancel_event)
        controller = None
        def cancelled():
            records._assert_lease(lease)
            if self._lease_lost.is_set():
                raise Fenced("shared service heartbeat ownership lost")
            return self._stopping or read_work(records.db, work.work_id).cancel_requested
        try:
            if records.status(work.work_id) in {"queued", "evaluating"} and read_work(records.db, work.work_id).cancel_requested:
                records.transition(work.work_id, "cancelled", action_id="service-cancel-before-start", lease=lease, reason="cancelled")
                return read_work(records.db, work.work_id)
            if records.status(work.work_id) == "queued":
                records.transition(work.work_id, "running", action_id="service-start", lease=lease)
            if records.status(work.work_id) == "evaluating":
                self.experiment_queue.park_evaluation(lease)
                return read_work(records.db, work.work_id)
            controller = self.experiment_runner_factory(lease, self.experiment_queue.descriptor(work))
            if not isinstance(controller, EpisodeCandidates) or controller.records is not records or controller.lease != lease:
                raise ValueError("experiment runner must bind the claimed Test and shared records")
            while True:
                outcome = controller.step(cancelled=cancelled)["kind"]
                if outcome == "progress":
                    continue
                if outcome in {"cleanup_required", "worker_recovery_required"}:
                    break
                if outcome == "ready_for_evaluation":
                    records.transition(work.work_id, "evaluating", action_id="service-evaluating", lease=lease)
                    self.experiment_queue.park_evaluation(lease)
                elif outcome in {"cancelled", "blocked", "failed"}:
                    records.transition(work.work_id, outcome, action_id="service-terminal:" + outcome, lease=lease,
                                       reason=controller.replay._state()["value"]["stop_reason"])
                else:
                    raise ValueError("unsupported experiment runner outcome")
                break
        except Fenced:
            self._lease_lost.set()
        except Exception as error:
            # Preserve uncertain dispatches; a following tick reconstructs the
            # same controller and drains the persisted stop before terminality.
            if records.status(work.work_id) not in TERMINAL:
                if controller is not None:
                    state = controller.replay._state()["value"]
                    if state["status"] == "running" and not state["stop_reason"]:
                        controller.replay.request_stop("platform_failure")
                else:
                    records.commit(lease, phase_id="service", action_id="factory-failure:" + str(lease.generation), attempt=0,
                        kind="service_factory_failure", payload={"error_type": type(error).__name__}, simulated_at=None)
                    pending = False
                    for name in ("candidate_processes", "model_invocations"):
                        projection = records.projection(work.work_id, name)
                        pending |= bool(projection and any(not c["quiescent"] or c["status"] == "prepared" for c in projection["value"]["calls"].values()))
                    if not pending:
                        records.transition(work.work_id, "failed", action_id="service-factory-failed", lease=lease,
                                           reason="executor_construction_failed")
        finally:
            stop.set()
            if thread is not None:
                thread.join(timeout=max(1.0, min(self.heartbeat_interval_seconds, settings.heartbeat_seconds) * 2))
            self._active_test_lease = self._active_test_settings = None
            self._active_cancel_event = None
        return read_work(records.db, work.work_id)

    def _run_claimed(self, request: ResearchRequest) -> None:
        try:
            boundary = self._ensure_boundary(request)
            self._progress(request.request_id, "preflight", 0, 0, None)
            self._raise_if_cancelled(request.request_id)
            cancel_event = self._active_cancel_event or threading.Event()
            result = self.executor(
                self.repository.get_request(request.request_id),
                boundary,
                lambda phase, completed, total, stage=None: self._progress(
                    request.request_id, phase, completed, total, stage
                ),
                _CancellationSignal(self, request.request_id, cancel_event),
            )
            self._raise_if_lease_lost()
            self._raise_if_cancelled(request.request_id)
            if result.status not in {"passed", "partial", "blocked", "failed"}:
                raise RuntimeError("invalid service execution status")
            self._complete_claimed(
                request.request_id,
                status=result.status,
                phase="complete",
                reason_code=result.reason_code,
                cycle_id=result.cycle_id,
                report_json_hash=result.report_json_hash,
                report_markdown_hash=result.report_markdown_hash,
                published_at=result.published_at,
            )
        except (ResearchServiceLeaseLost, ResearchRequestOwnershipLost):
            return
        except ResearchServiceCancelled:
            if self._lease_lost.is_set():
                return
            self._complete_claimed(
                request.request_id,
                status="cancelled",
                phase="cancelled",
                reason_code="cancelled",
            )
        except ResearchServiceBlocked as error:
            if self._lease_lost.is_set():
                return
            if self._cancelled(request.request_id):
                self._complete_claimed(
                    request.request_id,
                    status="cancelled",
                    phase="cancelled",
                    reason_code="cancelled",
                )
                return
            self._complete_claimed(
                request.request_id,
                status="blocked",
                phase="complete",
                reason_code=error.reason_code,
            )
        except Exception:
            if self._lease_lost.is_set():
                return
            if self._cancelled(request.request_id):
                self._complete_claimed(
                    request.request_id,
                    status="cancelled",
                    phase="cancelled",
                    reason_code="cancelled",
                )
                return
            self._complete_claimed(
                request.request_id,
                status="failed",
                phase="complete",
                reason_code="persistence_failed",
            )

    def _complete_claimed(
        self,
        request_id: str,
        *,
        status: str,
        phase: str,
        reason_code: str | None,
        cycle_id: str | None = None,
        report_json_hash: str | None = None,
        report_markdown_hash: str | None = None,
        published_at: datetime | None = None,
    ) -> bool:
        """Complete only the Request row claimed by this executor instance."""
        try:
            with self.repository.transaction():
                completed = self.repository.complete_request(
                    request_id,
                    status=status,
                    phase=phase,
                    reason_code=reason_code,
                    cycle_id=cycle_id,
                    report_json_hash=report_json_hash,
                    report_markdown_hash=report_markdown_hash,
                    published_at=published_at,
                    expected_claimed_by=self._claim_owner_id,
                    updated_at=self.clock(),
                )
        except ResearchRequestOwnershipLost:
            self._lease_lost.set()
            return False
        if completed.claimed_by != self._claim_owner_id:
            self._lease_lost.set()
            return False
        return True

    def _ensure_boundary(self, request: ResearchRequest) -> ResearchBoundary:
        self.repository.require_request_claim(request.request_id, self._claim_owner_id)
        if request.boundary is not None:
            return request.boundary
        # Manual Security requests are fixed at acceptance.  A Market Request
        # is intentionally fixed only at the execution/snapshot boundary.
        instant = request.accepted_at if request.scope == ResearchScope.security else self.clock()
        with self.repository.transaction():
            return self.repository.set_request_boundary(
                request.request_id,
                ResearchBoundary(as_of=instant),
                expected_claimed_by=self._claim_owner_id,
                updated_at=self.clock(),
            ).boundary  # type: ignore[return-value]

    def _progress(self, request_id: str, phase: str, completed: int, total: int, stage: str | None) -> None:
        self._raise_if_lease_lost()
        self._raise_if_cancelled(request_id)
        with self.repository.transaction():
            self.repository.update_request_progress(
                request_id,
                phase=phase,
                agents_completed=completed,
                agents_total=total,
                decision_stage=stage,
                expected_claimed_by=self._claim_owner_id,
                updated_at=self.clock(),
            )

    def _cancelled(self, request_id: str) -> bool:
        event = self._active_cancel_event
        lease = self.repository.service_lease_status(now=self.clock())
        if lease["owner_id"] != self._lease_owner_id:
            self._lease_lost.set()
            if event is not None:
                event.set()
            return True
        if self._stopping or (event is not None and event.is_set()):
            return True
        current = self.repository.get_request(request_id)
        if current.status != "running" or current.claimed_by != self._claim_owner_id:
            self._lease_lost.set()
            if event is not None:
                event.set()
            return True
        cancelled = current.cancel_requested
        if cancelled and event is not None:
            event.set()
        return cancelled

    def _raise_if_cancelled(self, request_id: str) -> None:
        if self._cancelled(request_id):
            raise ResearchServiceCancelled()

    def _raise_if_lease_lost(self) -> None:
        if self._lease_lost.is_set():
            raise ResearchServiceLeaseLost()

    def _start_lease_heartbeat(
        self,
        request_id: str,
        cancel_event: threading.Event,
    ) -> tuple[threading.Event, threading.Thread | None]:
        """Renew through a peer connection while a bounded Codex call runs."""
        stop = threading.Event()
        try:
            peer = self.repository.open_peer()
        except (OSError, sqlite3.Error):
            peer = None
        if peer is None:
            return stop, None
        test_lease, test_settings = self._active_test_lease, self._active_test_settings
        interval = min(self.heartbeat_interval_seconds, test_settings.heartbeat_seconds) if test_settings else self.heartbeat_interval_seconds

        def maintain() -> None:
            try:
                while not stop.wait(interval):
                    try:
                        with peer.transaction():
                            if not peer.heartbeat_service_lease(
                                self._lease_owner_id, now=self.clock(), ttl_seconds=self.lease_seconds
                            ):
                                self._lease_lost.set()
                                cancel_event.set()
                                return
                            if test_lease is not None:
                                item = read_work(peer.connection, request_id)
                                if item.state in {"finished", "waiting"}:
                                    return
                                self.experiment_queue.heartbeat(peer.connection, test_lease, service_owner_id=self._lease_owner_id,
                                    now=self.clock(), lease_seconds=test_settings.lease_seconds)
                                cancelled = item.cancel_requested
                            else:
                                cancelled = peer.get_request(request_id).cancel_requested
                            if cancelled:
                                cancel_event.set()
                    except Fenced:
                        self._lease_lost.set()
                        cancel_event.set()
                        return
                    except (sqlite3.Error, ValueError):
                        # A short database lock should not turn a healthy
                        # Request into a false failure. The next bounded pulse
                        # retries; a confirmed ownership change is handled
                        # above without terminally mutating the Request.
                        continue
            finally:
                peer.close()

        thread = threading.Thread(
            target=maintain,
            name=f"a-hunter-research-lease-{request_id[-8:]}",
            daemon=True,
        )
        thread.start()
        return stop, thread
