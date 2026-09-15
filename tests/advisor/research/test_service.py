from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading

import pytest

from advisor.research.contracts import ResearchSubject
from advisor.research.repository import ResearchRepository, ResearchRequestOwnershipLost
from advisor.research.service import ResearchService, ServiceExecutionResult


NOW = datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc)


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _repo(path: Path) -> ResearchRepository:
    return ResearchRepository.open(path)


def _submit(repo: ResearchRepository, *, identity: str, scope: str = "security", origin: str = "web", at: datetime = NOW):
    subject = ResearchSubject(code="600519") if scope == "security" else ResearchSubject(scope="market")
    return repo.submit_request(
        team_ref="normal@1" if scope == "security" else "market_overview@1",
        subject=subject,
        origin=origin,
        submission_identity=identity,
        requested_at=at,
        accepted_at=at,
    )


def test_only_one_service_holds_the_lease_and_offline_requests_stay_queued(tmp_path: Path):
    database = tmp_path / "advisor.sqlite"
    first_repo, second_repo = _repo(database), _repo(database)
    with first_repo.transaction():
        request = _submit(first_repo, identity="offline")
    clock = Clock(NOW)
    first = ResearchService(first_repo, lambda *_args: ServiceExecutionResult(status="blocked", reason_code="quality_blocked"), owner_id="one", clock=clock)
    second = ResearchService(second_repo, lambda *_args: ServiceExecutionResult(status="blocked", reason_code="quality_blocked"), owner_id="two", clock=clock)

    assert first.start() is True
    assert second.start() is False
    assert first_repo.get_request(request.request_id).status == "queued"
    assert second.status().state == "idle"


def test_reused_configured_owner_name_does_not_share_a_live_service_lease(tmp_path: Path):
    database = tmp_path / "advisor.sqlite"
    first_repo, second_repo = _repo(database), _repo(database)
    clock = Clock(NOW)
    first = ResearchService(
        first_repo,
        lambda *_args: ServiceExecutionResult(status="blocked", reason_code="quality_blocked"),
        owner_id="configured-owner",
        clock=clock,
    )
    second = ResearchService(
        second_repo,
        lambda *_args: ServiceExecutionResult(status="blocked", reason_code="quality_blocked"),
        owner_id="configured-owner",
        clock=clock,
    )

    assert first.start() is True
    assert second.start() is False


def test_two_connections_cannot_acquire_a_fresh_service_lease_at_the_same_time(tmp_path: Path):
    database = tmp_path / "advisor.sqlite"
    first_repo, second_repo = _repo(database), _repo(database)
    barrier = threading.Barrier(2)
    outcomes: list[bool] = []

    def acquire(repository: ResearchRepository, owner: str) -> None:
        barrier.wait(timeout=2)
        with repository.transaction():
            outcomes.append(repository.acquire_service_lease(owner, now=NOW, ttl_seconds=30))

    first = threading.Thread(target=acquire, args=(first_repo, "one"))
    second = threading.Thread(target=acquire, args=(second_repo, "two"))
    first.start()
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive() and not second.is_alive()
    assert sorted(outcomes) == [False, True]


def test_service_keeps_manual_fifo_priority_and_fixes_scope_specific_boundaries(tmp_path: Path):
    repo = _repo(tmp_path / "advisor.sqlite")
    with repo.transaction():
        security = _submit(repo, identity="security", at=NOW)
        market = _submit(repo, identity="market", scope="market", at=NOW + timedelta(seconds=1))
    clock = Clock(NOW + timedelta(seconds=10))
    seen: list[tuple[str, datetime]] = []

    def execute(request, boundary, progress, cancelled):
        progress("snapshot", 0, 0)
        seen.append((request.request_id, boundary.as_of))
        return ServiceExecutionResult(status="blocked", reason_code="quality_blocked")

    service = ResearchService(repo, execute, owner_id="one", clock=clock)
    assert service.tick() is not None
    clock.value = NOW + timedelta(seconds=20)
    assert service.tick() is not None

    assert [item[0] for item in seen] == [security.request_id, market.request_id]
    assert seen[0][1] == security.accepted_at
    assert seen[1][1] == NOW + timedelta(seconds=20)


def test_running_cancel_and_restart_recovery_preserve_durable_request_history(tmp_path: Path):
    database = tmp_path / "advisor.sqlite"
    repo = _repo(database)
    with repo.transaction():
        request = _submit(repo, identity="cancel")
    clock = Clock(NOW)

    def execute(current, _boundary, _progress, _cancelled):
        with repo.transaction():
            repo.request_cancel(current.request_id, updated_at=clock.value)
        return ServiceExecutionResult(status="blocked", reason_code="quality_blocked")

    service = ResearchService(repo, execute, owner_id="first", clock=clock)
    result = service.tick()
    assert result is not None and result.status == "cancelled"
    assert repo.record_for_request(request.request_id) is not None

    with repo.transaction():
        interrupted = _submit(repo, identity="interrupted", at=NOW + timedelta(seconds=1))
        claimed = repo.claim_next_request("abandoned", claimed_at=NOW + timedelta(seconds=1))
        assert claimed is not None
    clock.value = NOW + timedelta(seconds=40)
    resumed_repo = _repo(database)
    resumed = ResearchService(
        resumed_repo,
        lambda *_args: ServiceExecutionResult(status="blocked", reason_code="quality_blocked"),
        owner_id="second",
        clock=clock,
    )
    assert resumed.start() is True
    assert resumed_repo.get_request(interrupted.request_id).status == "queued"
    assert resumed.tick() is not None
    assert resumed_repo.get_request(interrupted.request_id).status == "blocked"


def test_lease_loss_signals_the_running_executor_and_only_the_new_owner_recovers_it(tmp_path: Path):
    database = tmp_path / "advisor.sqlite"
    first_repo, second_repo = _repo(database), _repo(database)
    with first_repo.transaction():
        request = _submit(first_repo, identity="lease-loss")
    first_clock = Clock(NOW)
    second_clock = Clock(NOW + timedelta(seconds=2))
    executor_started = threading.Event()
    executor_cancelled = threading.Event()

    def execute(_request, _boundary, _progress, cancelled):
        executor_started.set()
        assert cancelled.event.wait(timeout=2)
        executor_cancelled.set()
        return ServiceExecutionResult(status="blocked", reason_code="quality_blocked")

    first = ResearchService(
        first_repo,
        execute,
        owner_id="first",
        clock=first_clock,
        lease_seconds=1,
        heartbeat_interval_seconds=0.01,
    )
    worker = threading.Thread(target=first.tick)
    worker.start()
    assert executor_started.wait(timeout=2)

    recovered = ResearchService(
        second_repo,
        lambda *_args: ServiceExecutionResult(status="blocked", reason_code="quality_blocked"),
        owner_id="second",
        clock=second_clock,
    )

    with second_repo.transaction():
        assert second_repo.acquire_service_lease(recovered._lease_owner_id, now=second_clock.value, ttl_seconds=30)

    assert executor_cancelled.wait(timeout=2)
    worker.join(timeout=2)
    assert not worker.is_alive()
    # The former owner must leave the durable Request untouched once it knows
    # the singleton lease has moved.
    assert first_repo.get_request(request.request_id).status == "running"

    assert recovered.start() is True
    assert second_repo.get_request(request.request_id).status == "queued"
    assert recovered.tick() is not None
    assert second_repo.get_request(request.request_id).status == "blocked"


def test_reclaimed_request_is_fenced_from_an_old_executor_before_its_next_heartbeat(tmp_path: Path, monkeypatch):
    database = tmp_path / "advisor.sqlite"
    first_repo, second_repo = _repo(database), _repo(database)
    with first_repo.transaction():
        request = _submit(first_repo, identity="reclaim-fence")
    first_clock = Clock(NOW)
    second_clock = Clock(NOW + timedelta(seconds=2))
    executor_started = threading.Event()
    allow_old_executor_to_return = threading.Event()

    def execute(_request, _boundary, _progress, _cancelled):
        executor_started.set()
        assert allow_old_executor_to_return.wait(timeout=2)
        return ServiceExecutionResult(status="blocked", reason_code="quality_blocked")

    first = ResearchService(
        first_repo,
        execute,
        owner_id="first",
        clock=first_clock,
        lease_seconds=1,
        heartbeat_interval_seconds=0.5,
    )
    # Model the narrow window before the old process's next background pulse:
    # recovery/reclaim happens first, then its executor returns.
    monkeypatch.setattr(
        first,
        "_start_lease_heartbeat",
        lambda _request_id, _cancel_event: (threading.Event(), None),
    )
    old_worker = threading.Thread(target=first.tick)
    old_worker.start()
    assert executor_started.wait(timeout=2)
    old_claim = first_repo.get_request(request.request_id).claimed_by
    assert old_claim == first._claim_owner_id

    new_owner = ResearchService(
        second_repo,
        lambda *_args: ServiceExecutionResult(status="blocked", reason_code="quality_blocked"),
        owner_id="second",
        clock=second_clock,
        lease_seconds=1,
        heartbeat_interval_seconds=0.5,
    )
    assert new_owner.start() is True
    with second_repo.transaction():
        reclaimed = second_repo.claim_next_request(new_owner._claim_owner_id, claimed_at=second_clock.value)
        assert reclaimed is not None and reclaimed.request_id == request.request_id
    assert reclaimed.claimed_by == new_owner._claim_owner_id
    assert reclaimed.boundary is not None

    with pytest.raises(ResearchRequestOwnershipLost):
        with first_repo.transaction():
            first_repo.set_request_boundary(
                request.request_id,
                reclaimed.boundary,
                expected_claimed_by=old_claim,
                updated_at=second_clock.value,
            )
    with pytest.raises(ResearchRequestOwnershipLost):
        with first_repo.transaction():
            first_repo.update_request_progress(
                request.request_id,
                phase="agents",
                agents_completed=1,
                agents_total=3,
                expected_claimed_by=old_claim,
                updated_at=second_clock.value,
            )
    with pytest.raises(ResearchRequestOwnershipLost):
        with first_repo.transaction():
            first_repo.complete_request(
                request.request_id,
                status="blocked",
                reason_code="quality_blocked",
                expected_claimed_by=old_claim,
                updated_at=second_clock.value,
            )

    allow_old_executor_to_return.set()
    old_worker.join(timeout=2)
    assert not old_worker.is_alive()
    still_running = second_repo.get_request(request.request_id)
    assert still_running.status == "running"
    assert still_running.claimed_by == new_owner._claim_owner_id

    with second_repo.transaction():
        completed = second_repo.complete_request(
            request.request_id,
            status="blocked",
            reason_code="quality_blocked",
            expected_claimed_by=new_owner._claim_owner_id,
            updated_at=second_clock.value,
        )
    assert completed.status == "blocked"


def test_reacquired_service_lease_recovers_work_claimed_by_an_expired_successor(tmp_path: Path):
    database = tmp_path / "advisor.sqlite"
    first_repo, second_repo = _repo(database), _repo(database)
    with first_repo.transaction():
        request = _submit(first_repo, identity="lease-epoch-recovery")
    first_clock = Clock(NOW)
    second_clock = Clock(NOW + timedelta(seconds=2))
    first = ResearchService(
        first_repo,
        lambda *_args: ServiceExecutionResult(status="blocked", reason_code="quality_blocked"),
        owner_id="first",
        clock=first_clock,
        lease_seconds=1,
        heartbeat_interval_seconds=0.5,
    )
    second = ResearchService(
        second_repo,
        lambda *_args: ServiceExecutionResult(status="blocked", reason_code="quality_blocked"),
        owner_id="second",
        clock=second_clock,
        lease_seconds=1,
        heartbeat_interval_seconds=0.5,
    )

    assert first.start() is True
    with first_repo.transaction():
        first_claim = first_repo.claim_next_request(first._claim_owner_id, claimed_at=first_clock.value)
        assert first_claim is not None
    assert second.start() is True
    with second_repo.transaction():
        successor_claim = second_repo.claim_next_request(second._claim_owner_id, claimed_at=second_clock.value)
        assert successor_claim is not None
    assert second_repo.get_request(request.request_id).claimed_by == second._claim_owner_id

    first_clock.value = NOW + timedelta(seconds=4)
    assert first.start() is True
    recovered = first_repo.get_request(request.request_id)
    assert recovered.status == "queued"
    assert recovered.claimed_by is None
    assert recovered.reason_code == "service_recovery"


def test_start_recovers_an_interrupted_running_request_without_a_claim_identity(tmp_path: Path):
    repo = _repo(tmp_path / "advisor.sqlite")
    with repo.transaction():
        request = _submit(repo, identity="claimless-interruption")
        repo.connection.execute(
            """
            UPDATE research_requests
            SET status = 'running', phase = 'preflight', claimed_by = NULL, claimed_at = NULL
            WHERE request_id = ?
            """,
            (request.request_id,),
        )

    service = ResearchService(
        repo,
        lambda *_args: ServiceExecutionResult(status="blocked", reason_code="quality_blocked"),
        owner_id="recovery-owner",
        clock=Clock(NOW),
    )

    assert service.start() is True
    recovered = repo.get_request(request.request_id)
    assert recovered.status == "queued"
    assert recovered.claimed_by is None
    assert recovered.reason_code == "service_recovery"
