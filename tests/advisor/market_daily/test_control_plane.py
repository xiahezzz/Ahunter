from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from advisor.market_daily.control import MarketDailyControlPlane, MarketRunError, RunSecurity


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 7, 20, 0, tzinfo=SHANGHAI)


def securities() -> tuple[RunSecurity, ...]:
    return (
        RunSecurity("000001", "平安银行", "SZ", date(1991, 4, 3), None, "active"),
        RunSecurity("600519", "贵州茅台", "SH", date(2001, 8, 27), None, "active"),
    )


def test_submit_cold_start_is_idempotent(tmp_path):
    control = MarketDailyControlPlane(tmp_path / "advisor.sqlite")

    first = control.submit_cold_start(date(2026, 8, 7), date(2021, 8, 9), NOW)
    second = control.submit_cold_start(date(2026, 8, 7), date(2021, 8, 9), NOW + timedelta(seconds=1))

    assert first.request_id == second.request_id
    assert first.status == "pending"
    assert control.pending_request_count() == 1


def test_service_lease_is_exclusive_until_expiry(tmp_path):
    control = MarketDailyControlPlane(tmp_path / "advisor.sqlite")

    assert control.acquire_lease("one", NOW, lease_seconds=30) is True
    assert control.acquire_lease("two", NOW + timedelta(seconds=10), lease_seconds=30) is False
    assert control.acquire_lease("two", NOW + timedelta(seconds=31), lease_seconds=30) is True


def test_claim_create_run_and_compute_complete_progress(tmp_path):
    control = MarketDailyControlPlane(tmp_path / "advisor.sqlite")
    request = control.submit_cold_start(date(2026, 8, 7), date(2021, 8, 9), NOW)

    claimed = control.claim_next_request("service-one", NOW)
    assert claimed is not None
    assert claimed.request_id == request.request_id
    run = control.create_run(
        claimed.request_id,
        universe_hash="a" * 64,
        securities=securities(),
        now=NOW,
    )
    control.start_run(run.run_id, NOW)
    control.mark_item(run.run_id, "000001", "completed", NOW, selected_source="eastmoney")
    control.mark_item(run.run_id, "600519", "skipped", NOW, selected_source="listing_interval")

    final = control.finalize_run(run.run_id, NOW)

    assert final.status == "complete"
    assert final.total_items == 2
    assert final.completed_items == 2
    assert final.failed_items == 0
    assert control.request(request.request_id).status == "completed"


def test_source_missing_leaves_run_partial_and_restart_requeues_only_running_items(tmp_path):
    control = MarketDailyControlPlane(tmp_path / "advisor.sqlite")
    request = control.submit_cold_start(date(2026, 8, 7), date(2021, 8, 9), NOW)
    claimed = control.claim_next_request("service-one", NOW)
    assert claimed is not None
    run = control.create_run(claimed.request_id, "b" * 64, securities(), NOW)
    control.start_run(run.run_id, NOW)
    control.mark_item(run.run_id, "000001", "running", NOW)
    control.mark_item(run.run_id, "600519", "source_missing", NOW, error="provider unavailable")

    control.recover_expired_work(NOW + timedelta(minutes=2), lease_seconds=30)
    recovered = control.run(run.run_id)

    assert recovered.status == "pending"
    assert control.item(run.run_id, "000001").status == "pending"
    assert control.item(run.run_id, "600519").status == "source_missing"

    control.start_run(run.run_id, NOW + timedelta(minutes=2))
    control.mark_item(run.run_id, "000001", "completed", NOW + timedelta(minutes=2), selected_source="eastmoney")
    final = control.finalize_run(run.run_id, NOW + timedelta(minutes=2))
    assert final.status == "partial"
    assert final.failed_items == 1


def test_control_plane_rejects_illegal_transitions_and_unknown_security(tmp_path):
    control = MarketDailyControlPlane(tmp_path / "advisor.sqlite")
    request = control.submit_cold_start(date(2026, 8, 7), date(2021, 8, 9), NOW)
    claimed = control.claim_next_request("service-one", NOW)
    assert claimed is not None
    run = control.create_run(claimed.request_id, "c" * 64, securities(), NOW)

    with pytest.raises(MarketRunError):
        control.finalize_run(run.run_id, NOW)
    with pytest.raises(MarketRunError):
        control.mark_item(run.run_id, "300750", "completed", NOW)


def test_persisted_item_error_is_bounded_and_redacts_response_details(tmp_path):
    control = MarketDailyControlPlane(tmp_path / "advisor.sqlite")
    request = control.submit_cold_start(date(2026, 8, 7), date(2021, 8, 9), NOW)
    claimed = control.claim_next_request("service-one", NOW)
    assert claimed is not None
    run = control.create_run(claimed.request_id, "d" * 64, securities(), NOW)
    control.start_run(run.run_id, NOW)

    raw_error = "HTTP 502 debug_id=opaque-123 response_body=<html>" + ("x" * 600)
    item = control.mark_item(run.run_id, "000001", "source_missing", NOW, error=raw_error)

    assert item.last_error is not None
    assert len(item.last_error) <= 280
    assert "opaque-123" not in item.last_error
    assert "<html>" not in item.last_error


def test_concurrent_submission_and_item_claim_keep_one_durable_owner(tmp_path):
    control = MarketDailyControlPlane(tmp_path / "advisor.sqlite")

    with ThreadPoolExecutor(max_workers=2) as pool:
        submitted = list(
            pool.map(
                lambda _unused: control.submit_cold_start(date(2026, 8, 7), date(2021, 8, 9), NOW),
                range(2),
            )
        )

    assert {request.request_id for request in submitted} == {submitted[0].request_id}
    assert control.pending_request_count() == 1
    claimed = control.claim_next_request("service-one", NOW)
    assert claimed is not None
    run = control.create_run(claimed.request_id, "e" * 64, securities(), NOW)
    assert control.create_run(claimed.request_id, "e" * 64, securities(), NOW).run_id == run.run_id
    control.start_run(run.run_id, NOW)

    assert control.claim_item(run.run_id, "000001", "worker-one", NOW) is True
    assert control.claim_item(run.run_id, "000001", "worker-two", NOW + timedelta(seconds=1)) is False
    control.mark_item(run.run_id, "000001", "completed", NOW, selected_source="fixture")
    with pytest.raises(MarketRunError):
        control.mark_item(run.run_id, "000001", "source_missing", NOW, error="cannot regress")


def test_item_claim_heartbeat_extends_only_the_current_owner_lease(tmp_path):
    control = MarketDailyControlPlane(tmp_path / "advisor.sqlite")
    request = control.submit_cold_start(date(2026, 8, 7), date(2021, 8, 9), NOW)
    claimed = control.claim_next_request("service-one", NOW)
    assert claimed is not None
    run = control.create_run(claimed.request_id, "f" * 64, securities(), NOW)
    control.start_run(run.run_id, NOW)

    assert control.claim_item(run.run_id, "000001", "worker-one", NOW, lease_seconds=10) is True
    assert control.renew_item_claim(
        run.run_id, "000001", "worker-one", NOW + timedelta(seconds=5), lease_seconds=10
    ) is True
    assert control.renew_item_claim(
        run.run_id, "000001", "worker-two", NOW + timedelta(seconds=6), lease_seconds=10
    ) is False
    assert control.claim_item(run.run_id, "000001", "worker-two", NOW + timedelta(seconds=11)) is False
    assert control.claim_item(run.run_id, "000001", "worker-two", NOW + timedelta(seconds=16)) is True


def test_live_progress_tracks_commits_and_preserves_completed_items_on_resume(tmp_path):
    control = MarketDailyControlPlane(tmp_path / "advisor.sqlite")
    request = control.submit_catch_up(NOW.date(), NOW.date(), NOW)
    control.claim_next_request("service-one", NOW)
    run = control.create_run(request.request_id, "a" * 64, securities(), NOW)
    control.start_run(run.run_id, NOW)
    control.mark_item(run.run_id, "000001", "completed", NOW)
    # Reading through a new instance mirrors the independently running API.
    reader = MarketDailyControlPlane(control.database_path)
    assert reader.run(run.run_id).completed_items == 1
    assert reader.latest_run().completed_items == 1
    control.mark_item(run.run_id, "000001", "completed", NOW)
    assert reader.run(run.run_id).completed_items == 1
    control.mark_item(run.run_id, "600519", "source_missing", NOW, error="租约已失效")
    assert reader.run(run.run_id).failed_items == 1
    control.finalize_run(run.run_id, NOW)

    resumed = control.resume_partial_run(run.run_id, NOW + timedelta(minutes=1))
    assert resumed.completed_items == 1
    assert resumed.failed_items == 0
    control.mark_item(run.run_id, "600519", "completed", NOW + timedelta(minutes=1))
    assert reader.run(run.run_id).completed_items == 2
    assert control.finalize_run(run.run_id, NOW + timedelta(minutes=1)).status == "complete"


def test_resume_progress_retains_conflicts(tmp_path):
    control = MarketDailyControlPlane(tmp_path / "advisor.sqlite")
    request = control.submit_catch_up(NOW.date(), NOW.date(), NOW)
    control.claim_next_request("service-one", NOW)
    run = control.create_run(request.request_id, "b" * 64, securities(), NOW)
    control.start_run(run.run_id, NOW)
    control.mark_item(run.run_id, "000001", "conflicted", NOW)
    control.mark_item(run.run_id, "600519", "source_missing", NOW)
    control.finalize_run(run.run_id, NOW)

    resumed = control.resume_partial_run(run.run_id, NOW + timedelta(minutes=1))
    assert resumed.completed_items == 0
    assert resumed.failed_items == 1
    assert control.item(run.run_id, "000001").status == "conflicted"
