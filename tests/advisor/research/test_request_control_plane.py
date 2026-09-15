from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from advisor.research.contracts import ResearchBoundary, ResearchSubject
from advisor.research.repository import ResearchRepository


NOW = datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc)
HASH = "a" * 64


def _repo(tmp_path: Path) -> ResearchRepository:
    return ResearchRepository.open(tmp_path / "advisor.sqlite")


def _submit(repo: ResearchRepository, identity: str, *, origin: str = "web", at: datetime = NOW):
    return repo.submit_request(
        team_ref="normal@1",
        subject=ResearchSubject(code="600519"),
        origin=origin,
        submission_identity=identity,
        requested_at=at,
        accepted_at=at,
    )


def test_submission_identity_is_idempotent_but_independent_clicks_are_not(tmp_path: Path):
    repo = _repo(tmp_path)
    with repo.transaction():
        first = _submit(repo, "transport-1")
        retry = _submit(repo, "transport-1")
        second = _submit(repo, "transport-2")

    assert first.request_id == retry.request_id
    assert first.request_id != second.request_id
    assert {item.request_id for item in repo.current_requests()} == {first.request_id, second.request_id}


def test_claim_cancel_rerun_and_progress_are_durable_and_fail_closed(tmp_path: Path):
    repo = _repo(tmp_path)
    with repo.transaction():
        queued = _submit(repo, "queued")
        running = _submit(repo, "running", at=NOW + timedelta(seconds=1))
        cancelled = repo.request_cancel(queued.request_id, updated_at=NOW + timedelta(seconds=2))
        claimed = repo.claim_next_request("service-a", claimed_at=NOW + timedelta(seconds=3))
        assert claimed is not None and claimed.request_id == running.request_id
        repo.update_request_progress(claimed.request_id, phase="agents", agents_completed=1, agents_total=2, updated_at=NOW + timedelta(seconds=4))
        terminal = repo.complete_request(
            claimed.request_id,
            status="partial",
            phase="complete",
            reason_code="agent_timeout",
            report_json_hash=HASH,
            report_markdown_hash="b" * 64,
            updated_at=NOW + timedelta(seconds=5),
        )
        rerun = repo.rerun_request(terminal.request_id, submission_identity="rerun-1", requested_at=NOW + timedelta(seconds=6))

    assert cancelled.status == "cancelled"
    assert repo.record_for_request(cancelled.request_id) is not None
    assert terminal.status == "partial"
    assert repo.record_for_request(terminal.request_id).report_json_hash == HASH  # type: ignore[union-attr]
    assert rerun.rerun_of == terminal.request_id
    assert rerun.boundary is None
    with pytest.raises(ValueError, match="terminal"):
        repo.request_cancel(terminal.request_id)
    with pytest.raises(ValueError, match="only terminal"):
        repo.rerun_request(rerun.request_id, submission_identity="rerun-2")


def test_cancellation_wins_when_it_arrives_between_completion_read_and_terminal_write(tmp_path: Path):
    database = tmp_path / "advisor.sqlite"
    repository = ResearchRepository.open(database)
    with repository.transaction():
        request = _submit(repository, "completion-race")
        claimed = repository.claim_next_request("service-a", claimed_at=NOW + timedelta(seconds=1))
        assert claimed is not None and claimed.request_id == request.request_id

    cancelling_peer = ResearchRepository.open(database)

    class CompletionRaceRepository(ResearchRepository):
        def __init__(self, connection, peer: ResearchRepository) -> None:
            super().__init__(connection)
            self.peer = peer
            self.race_request_id: str | None = None
            self.raced = False

        def get_request(self, request_id: str):
            current = super().get_request(request_id)
            if request_id == self.race_request_id and not self.raced:
                self.raced = True
                with self.peer.transaction():
                    self.peer.request_cancel(request_id, updated_at=NOW + timedelta(seconds=2))
            return current

    racing_connection = ResearchRepository.open(database)
    racing = CompletionRaceRepository(racing_connection.connection, cancelling_peer)
    racing.race_request_id = request.request_id
    with racing.transaction():
        terminal = racing.complete_request(
            request.request_id,
            status="passed",
            cycle_id="published-cycle",
            report_json_hash=HASH,
            report_markdown_hash="b" * 64,
            updated_at=NOW + timedelta(seconds=3),
        )

    assert terminal.status == "cancelled"
    assert terminal.reason_code == "cancelled"
    assert terminal.cycle_id == "published-cycle"
    assert terminal.report_json_hash is None
    assert terminal.report_markdown_hash is None
    assert repository.get_request(request.request_id).status == "cancelled"
    record = repository.record_for_request(request.request_id)
    assert record is not None and record.status == "cancelled"


def test_manual_fifo_has_priority_over_scheduled_and_records_paginate_past_one_hundred(tmp_path: Path):
    repo = _repo(tmp_path)
    with repo.transaction():
        scheduled = _submit(repo, "scheduled", origin="scheduled", at=NOW)
        manual = _submit(repo, "manual", origin="web", at=NOW + timedelta(seconds=1))
        claimed = repo.claim_next_request("service-a", claimed_at=NOW + timedelta(seconds=2))
        assert claimed is not None and claimed.request_id == manual.request_id
        repo.complete_request(claimed.request_id, status="blocked", reason_code="quality_blocked", updated_at=NOW + timedelta(seconds=3))
        claimed_scheduled = repo.claim_next_request("service-a", claimed_at=NOW + timedelta(seconds=4))
        assert claimed_scheduled is not None and claimed_scheduled.request_id == scheduled.request_id
        repo.complete_request(claimed_scheduled.request_id, status="blocked", reason_code="quality_blocked", updated_at=NOW + timedelta(seconds=5))
        for index in range(151):
            request = _submit(repo, f"page-{index}", at=NOW + timedelta(minutes=index + 1))
            claimed = repo.claim_next_request("service-a", claimed_at=NOW + timedelta(minutes=index + 1, seconds=1))
            assert claimed is not None and claimed.request_id == request.request_id
            repo.complete_request(claimed.request_id, status="blocked", reason_code="quality_blocked", updated_at=NOW + timedelta(minutes=index + 1, seconds=2))

    records = [*repo.list_records(statuses=("blocked",), limit=100, offset=0), *repo.list_records(statuses=("blocked",), limit=100, offset=100)]
    assert len(records) == 153
    assert len({item.record_id for item in records}) == 153


def test_scope_and_time_mismatches_do_not_leave_partial_control_plane_state(tmp_path: Path):
    repo = _repo(tmp_path)
    with pytest.raises(ValueError, match="earlier"):
        repo.submit_request(
            team_ref="normal@1",
            subject=ResearchSubject(code="600519"),
            origin="web",
            submission_identity="bad-time",
            requested_at=NOW,
            accepted_at=NOW - timedelta(seconds=1),
        )
    with pytest.raises(ValueError, match="Security Subject"):
        ResearchSubject(scope="security")
    assert repo.current_requests() == ()


def test_boundary_persistence_is_monotonic_across_different_utc_offsets(tmp_path: Path):
    repo = _repo(tmp_path)
    china = timezone(timedelta(hours=8))
    requested = datetime(2026, 9, 6, 8, 30, tzinfo=china)
    later_utc = datetime(2026, 9, 6, 3, 25, tzinfo=timezone.utc)

    with repo.transaction():
        queued = _submit(repo, "mixed-offset-boundary", at=requested)
        claimed = repo.claim_next_request("service-a", claimed_at=later_utc)
        assert claimed is not None and claimed.request_id == queued.request_id
        persisted = repo.set_request_boundary(
            claimed.request_id,
            ResearchBoundary(as_of=later_utc),
            expected_claimed_by="service-a",
            updated_at=later_utc,
        )

    assert persisted.boundary is not None
    assert persisted.boundary.as_of == later_utc
    raw = repo.connection.execute(
        "SELECT boundary_at FROM research_requests WHERE request_id = ?",
        (queued.request_id,),
    ).fetchone()[0]
    assert raw == "2026-09-06T11:25:00+08:00"


def test_request_fifo_orders_different_offsets_by_instant(tmp_path: Path):
    repo = _repo(tmp_path)
    china = timezone(timedelta(hours=8))
    earlier = datetime(2026, 9, 6, 8, 0, tzinfo=china)  # 00:00 UTC
    later = datetime(2026, 9, 6, 1, 0, tzinfo=timezone.utc)

    with repo.transaction():
        late_request = _submit(repo, "offset-late", at=later)
        early_request = _submit(repo, "offset-early", at=earlier)
        assert [item.request_id for item in repo.current_requests()] == [
            early_request.request_id,
            late_request.request_id,
        ]
        claimed = repo.claim_next_request("service-a", claimed_at=later + timedelta(hours=1))

    assert claimed is not None
    assert claimed.request_id == early_request.request_id


def test_legacy_cycle_index_is_idempotent_and_never_fabricates_a_report(tmp_path: Path):
    repo = _repo(tmp_path)
    with repo.transaction():
        repo.create_cycle(
            cycle_id="legacy-cycle", subject_code="600519", subject_name=None,
            as_of=NOW.isoformat(), fingerprint="f" * 64,
        )
        repo.create_run(run_id="legacy-cycle:normal@1", cycle_id="legacy-cycle", team_ref="normal@1")
        repo.set_status("research_cycles", "cycle_id", "legacy-cycle", "running")
        repo.set_status("research_cycles", "cycle_id", "legacy-cycle", "passed", finished=True)
        repo.set_status("research_runs", "research_run_id", "legacy-cycle:normal@1", "running")
        repo.set_status("research_runs", "research_run_id", "legacy-cycle:normal@1", "passed", finished=True)
        first = repo.index_legacy_cycles()
        second = repo.index_legacy_cycles()

    assert first == 1 and second == 0
    records = repo.list_records(statuses=("blocked",), limit=10)
    assert len(records) == 1
    assert records[0].reason_code == "publication_failed"
    assert records[0].report_json_hash is None
