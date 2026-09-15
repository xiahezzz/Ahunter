import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from advisor.market_daily.control import MarketDailyControlPlane, RunSecurity
from advisor.web.api import create_app


NOW = datetime(2026, 8, 7, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _partial_run(database):
    control = MarketDailyControlPlane(database)
    request = control.submit_cold_start(date(2026, 8, 7), date(2021, 8, 9), NOW)
    claimed = control.claim_next_request("fixture-service", NOW)
    assert claimed is not None
    run = control.create_run(
        claimed.request_id,
        "a" * 64,
        (
            RunSecurity("600519", "贵州茅台", "SH", date(2001, 8, 27), None, "active"),
            RunSecurity("000001", "平安银行", "SZ", date(1991, 4, 3), None, "active"),
        ),
        NOW,
    )
    control.start_run(run.run_id, NOW)
    control.mark_item(run.run_id, "600519", "completed", NOW, selected_source="eastmoney")
    control.mark_item(run.run_id, "000001", "source_missing", NOW, error="来源未证明日线或停牌")
    return control.finalize_run(run.run_id, NOW)


def test_market_daily_status_api_reports_partial_progress_and_bounded_failures(tmp_path):
    database = tmp_path / "advisor.sqlite"
    run = _partial_run(database)
    client = TestClient(create_app(tmp_path, db_path=database))

    status = client.get("/api/market-daily/status")
    listing = client.get("/api/market-daily/runs?limit=1")
    detail = client.get(f"/api/market-daily/runs/{run.run_id}")
    failures = client.get(f"/api/market-daily/runs/{run.run_id}/failures?limit=1")

    assert status.status_code == 200
    assert status.json()["state"] == "partial"
    assert status.json()["progress"] == 1.0
    assert listing.json()["runs"][0]["run_id"] == run.run_id
    assert detail.json()["item_status_counts"] == {"completed": 1, "source_missing": 1}
    assert failures.json()["total"] == 1
    assert failures.json()["items"] == [{
        "code": "000001", "status": "source_missing", "attempts": 0,
        "selected_source": None, "error": "来源未证明日线或停牌", "updated_at": NOW.isoformat(),
    }]


def test_market_daily_status_returns_clear_503_when_database_is_unavailable(tmp_path):
    response = TestClient(create_app(tmp_path, db_path=tmp_path / "missing.sqlite")).get("/api/market-daily/status")

    assert response.status_code == 503
    assert "数据库不可用" in response.json()["detail"]


def test_market_daily_control_api_submits_one_idempotent_cold_start_intent(tmp_path):
    database = tmp_path / "advisor.sqlite"
    client = TestClient(create_app(tmp_path, db_path=database))

    first = client.post("/api/market-daily/cold-start")
    second = client.post("/api/market-daily/cold-start")
    status = client.get("/api/market-daily/status")

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json() == second.json()
    assert first.json() == {
        "request_id": "mdreq-c2c28608551dc2c7684c7cee",
        "request_status": "pending",
        "message": "冷启动请求已在本地队列中；Market Daily 服务会在 21:00 后执行",
    }
    assert MarketDailyControlPlane(database).pending_request_count() == 1
    assert status.json()["state"] == "waiting_for_cold_start"
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM market_daily_runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM market_daily").fetchone()[0] == 0
    finally:
        connection.close()


def test_market_daily_control_api_returns_a_bounded_failure_when_queue_is_unavailable(tmp_path):
    database = tmp_path / "not-a-database"
    database.mkdir()

    response = TestClient(create_app(tmp_path, db_path=database)).post("/api/market-daily/cold-start")

    assert response.status_code == 503
    assert response.json() == {"detail": "Market Daily 冷启动请求不可提交"}
