import asyncio
import inspect
import os
import sqlite3
import tomllib
from datetime import date, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from fastapi.responses import StreamingResponse
import pytest

from advisor import paths as advisor_paths
from advisor.db.migrate import migrate_database
from advisor.quality import QualityResult
from advisor.reporting.contracts import AdviceItem, ReviewItem
from advisor.reporting.premarket import write_premarket_report
from advisor.reporting.review import write_review_report
from advisor.web.api import create_app
import advisor.web.api as web_api


def test_dev_dependencies_declare_starlette_testclient_transport():
    project = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8"))

    assert any(requirement.startswith("httpx2>=") and "<3" in requirement for requirement in project["project"]["optional-dependencies"]["dev"])


def test_health_endpoint_reports_service_and_known_component_statuses(tmp_path):
    client = TestClient(create_app(tmp_path))

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "advisor-api",
        "collector": "unknown",
        "market_updater": "unknown",
        "advisor_scheduler": "unknown",
        "frontend": "unknown",
        "api": "ok",
    }


def test_current_state_degrades_explicitly_when_local_state_is_missing(tmp_path):
    client = TestClient(create_app(tmp_path))

    response = client.get("/api/current-state")

    assert response.status_code == 200
    payload = response.json()
    assert {"today", "advice", "review", "ledger", "flows", "blocking_quality_checks", "reports", "profiles", "charts", "health"} <= payload.keys()
    assert payload["advice"] == []
    assert payload["review"] == {"status": "missing", "items": []}
    assert payload["ledger"] == {
        "cash": 0.0,
        "positions": [],
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "accounts": [],
    }
    assert payload["flows"] == {
        "information": {"status": "unknown", "count": 0},
        "capital": {"status": "unknown", "count": 0},
        "analyst": {"status": "unknown", "count": 0},
    }
    assert payload["blocking_quality_checks"] == []
    assert payload["reports"] == []
    assert payload["profiles"] == []
    assert payload["charts"] == []


def test_report_routes_list_and_serve_only_verified_archives(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    write_premarket_report(
        "2026-07-11",
        [],
        reports_root,
        quality_results=[QualityResult("market_data", "blocking", True, "current")],
    )
    client = TestClient(create_app(tmp_path / "state"))

    listing = client.get("/api/reports")
    report = client.get("/api/reports/2026-07-11/premarket?run_id=initial")

    assert listing.status_code == 200
    assert listing.json()["reports"] == [
        {
            "report_date": "2026-07-11",
            "report_type": "premarket",
            "run_id": "initial",
            "quality_status": "passed",
            "href": "/api/reports/2026-07-11/premarket?run_id=initial",
        }
    ]
    assert report.status_code == 200
    assert report.json()["json"]["report_type"] == "premarket"
    assert "08:30 Premarket Advice" in report.json()["markdown"]
    assert client.get("/api/reports/../premarket").status_code == 404
    assert client.get("/api/reports/2026-07-11/unknown").status_code == 404


def test_profile_routes_read_structured_profiles_and_reject_malformed_data(tmp_path):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    now = datetime.now().isoformat()
    connection.execute(
        "INSERT INTO securities (code, name, exchange, industry, concepts_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("600519", "Moutai", "SSE", "Beverages", "[]", now, now),
    )
    connection.execute(
        """
        INSERT INTO stock_profiles (
          code, thesis_json, information_flow_json, capital_flow_json, fundamentals_json,
          analyst_flow_json, ledger_exposure_json, assets_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("600519", '{"thesis":"durable demand"}', '["news"]', '["flow"]', '{}', '["analyst"]', '{}', '[]', now),
    )
    connection.commit()
    connection.close()
    client = TestClient(create_app(tmp_path))

    listing = client.get("/api/profiles")
    profile = client.get("/api/profiles/600519")

    assert listing.status_code == 200
    assert listing.json()["profiles"] == [{"code": "600519", "name": "Moutai", "href": "/api/profiles/600519"}]
    assert profile.status_code == 200
    assert profile.json()["thesis"] == {"thesis": "durable demand"}
    assert profile.json()["information_flow"] == ["news"]
    connection = sqlite3.connect(db_path)
    connection.execute("UPDATE stock_profiles SET thesis_json = 'not-json' WHERE code = '600519'")
    connection.commit()
    connection.close()
    assert client.get("/api/profiles/600519").status_code == 404
    assert client.get("/api/profiles").json()["profiles"] == []
    assert client.get("/api/profiles/../../etc").status_code == 404


def test_chart_routes_only_list_and_serve_contained_regular_png_assets(tmp_path):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    chart_path = tmp_path / "charts" / "600519-kline.png"
    chart_path.parent.mkdir()
    chart_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO chart_assets (asset_id, code, chart_type, as_of, path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("chart-1", "600519", "kline", "2026-07-11", str(chart_path), "2026-07-11T08:30:00"),
    )
    connection.commit()
    connection.close()
    client = TestClient(create_app(tmp_path))

    listing = client.get("/api/charts")
    asset = client.get("/api/charts/chart-1")

    assert listing.status_code == 200
    assert listing.json()["charts"] == [
        {"asset_id": "chart-1", "code": "600519", "chart_type": "kline", "as_of": "2026-07-11", "href": "/api/charts/chart-1"}
    ]
    assert asset.status_code == 200
    assert asset.headers["content-type"] == "image/png"
    chart_path.unlink()
    chart_path.symlink_to(tmp_path / "outside.png")
    assert client.get("/api/charts/chart-1").status_code == 404
    assert client.get("/api/charts/../../outside").status_code == 404


def test_chart_route_streams_descriptor_pinned_bytes_during_replacement_race(tmp_path, monkeypatch):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    chart_path = tmp_path / "charts" / "race.png"
    chart_path.parent.mkdir()
    original = b"\x89PNG\r\n\x1a\noriginal"
    chart_path.write_bytes(original)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO chart_assets (asset_id, code, chart_type, as_of, path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("chart-race", "600519", "kline", "2026-07-11", str(chart_path), "2026-07-11T08:30:00"),
    )
    connection.commit()
    connection.close()
    called = []

    def replace_after_open(event: str, **_context):
        if event == "fd_opened":
            replacement = chart_path.with_name("replacement.png")
            replacement.write_bytes(b"\x89PNG\r\n\x1a\nreplacement")
            replacement.replace(chart_path)
            called.append(event)

    monkeypatch.setattr("advisor.web.api._chart_hook", replace_after_open, raising=False)
    response = TestClient(create_app(tmp_path)).get("/api/charts/chart-race")

    assert called == ["fd_opened"]
    assert response.content == original


def test_chart_stream_closes_descriptor_when_asgi_send_fails(tmp_path):
    path = tmp_path / "chart.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\npayload")
    descriptor = os.open(path, os.O_RDONLY)
    stream = web_api._stream_descriptor(descriptor)

    async def run_response():
        response = StreamingResponse(stream, media_type="image/png")

        async def receive():
            await asyncio.sleep(60)

        async def send(message):
            if message["type"] == "http.response.body":
                raise RuntimeError("send failed")

        try:
            await response({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http", "path": "/", "raw_path": b"/", "query_string": b"", "headers": [], "client": ("test", 1), "server": ("test", 80)}, receive, send)
        except RuntimeError:
            pass

    assert inspect.isasyncgen(stream)
    asyncio.run(run_response())
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_ledger_routes_store_valid_local_transactions_and_reject_oversells(tmp_path):
    client = TestClient(create_app(tmp_path))
    deposit = {
        "transaction_id": "cash-1",
        "trade_date": "2026-07-10",
        "transaction_type": "cash_deposit",
        "quantity": 0,
        "price": 0,
        "amount": 100000,
        "fees": 0,
    }
    buy = {
        "transaction_id": "buy-1",
        "trade_date": "2026-07-10",
        "transaction_type": "buy",
        "code": "600519",
        "quantity": 100,
        "price": 100,
        "amount": -10000,
        "fees": 5,
    }
    oversell = {**buy, "transaction_id": "sell-1", "transaction_type": "sell", "amount": 11000, "price": 110, "quantity": 101}

    assert client.get("/api/ledger/transactions").json()["transactions"] == []
    assert client.post("/api/ledger/transactions", json=deposit).status_code == 201
    created = client.post("/api/ledger/transactions", json=buy)

    assert created.status_code == 201
    assert created.json()["ledger"]["cash"] == 89995.0
    assert created.json()["ledger"]["positions"] == [
        {"code": "600519", "quantity": 100, "cost_basis": 10005.0, "market_price": None, "market_value": None, "unrealized_pnl": 0.0}
    ]
    assert client.post("/api/ledger/transactions", json=buy).status_code == 409
    assert client.post("/api/ledger/transactions", json=oversell).status_code == 422


def test_ledger_import_is_atomic_for_bounded_validated_json_lists(tmp_path):
    client = TestClient(create_app(tmp_path))
    transactions = [
        {"transaction_id": "cash-1", "trade_date": "2026-07-10", "transaction_type": "cash_deposit", "quantity": 0, "price": 0, "amount": 1000, "fees": 0},
        {"transaction_id": "buy-1", "trade_date": "2026-07-10", "transaction_type": "buy", "code": "600519", "quantity": 10, "price": 100, "amount": -1000, "fees": 0},
    ]

    imported = client.post("/api/ledger/import", json=transactions)
    rejected = client.post(
        "/api/ledger/import",
        json=[
            {"transaction_id": "cash-2", "trade_date": "2026-07-11", "transaction_type": "cash_deposit", "quantity": 0, "price": 0, "amount": 100, "fees": 0},
            {"transaction_id": "sell-1", "trade_date": "2026-07-11", "transaction_type": "sell", "code": "600519", "quantity": 11, "price": 100, "amount": 1100, "fees": 0},
        ],
    )
    current = client.get("/api/ledger/transactions")

    assert imported.status_code == 201
    assert imported.json()["ledger"]["positions"][0]["quantity"] == 10
    assert rejected.status_code == 422
    assert [row["transaction_id"] for row in current.json()["transactions"]] == ["buy-1", "cash-1"]


def test_ledger_rejects_backdated_sell_using_canonical_replay_order(tmp_path):
    client = TestClient(create_app(tmp_path))
    assert client.post(
        "/api/ledger/import",
        json=[
            {"transaction_id": "cash-1", "trade_date": "2026-07-10", "transaction_type": "cash_deposit", "quantity": 0, "price": 0, "amount": 1000, "fees": 0},
            {"transaction_id": "buy-1", "trade_date": "2026-07-11", "transaction_type": "buy", "code": "600519", "quantity": 10, "price": 100, "amount": -1000, "fees": 0},
        ],
    ).status_code == 201

    rejected = client.post(
        "/api/ledger/transactions",
        json={"transaction_id": "sell-backdated", "trade_date": "2026-07-09", "transaction_type": "sell", "code": "600519", "quantity": 10, "price": 100, "amount": 1000, "fees": 0},
    )

    assert rejected.status_code == 422
    assert [row["transaction_id"] for row in client.get("/api/ledger/transactions").json()["transactions"]] == ["cash-1", "buy-1"]


def test_ledger_rejects_invalid_signed_cash_and_stock_codes(tmp_path):
    client = TestClient(create_app(tmp_path))
    invalid_deposit = {
        "transaction_id": "cash-1",
        "trade_date": "2026-07-10",
        "transaction_type": "cash_deposit",
        "quantity": 0,
        "price": 0,
        "amount": -1,
        "fees": 0,
    }
    invalid_trade = {
        "transaction_id": "buy-1",
        "trade_date": "2026-07-10",
        "transaction_type": "buy",
        "code": "not-a-code",
        "quantity": 1,
        "price": 10,
        "amount": -10,
        "fees": 0,
    }

    assert client.post("/api/ledger/transactions", json=invalid_deposit).status_code == 422
    assert client.post("/api/ledger/transactions", json=invalid_trade).status_code == 422
    assert client.get("/api/ledger/transactions").json()["transactions"] == []


def test_report_and_ledger_lists_enforce_bounded_limit_and_offset(tmp_path):
    client = TestClient(create_app(tmp_path))
    assert client.post(
        "/api/ledger/import",
        json=[
            {"transaction_id": "cash-1", "trade_date": "2026-07-10", "transaction_type": "cash_deposit", "quantity": 0, "price": 0, "amount": 1000, "fees": 0},
            {"transaction_id": "cash-2", "trade_date": "2026-07-11", "transaction_type": "cash_deposit", "quantity": 0, "price": 0, "amount": 1000, "fees": 0},
        ],
    ).status_code == 201

    paged = client.get("/api/ledger/transactions?limit=1&offset=1")

    assert paged.status_code == 200
    assert [row["transaction_id"] for row in paged.json()["transactions"]] == ["cash-2"]
    assert client.get("/api/ledger/transactions?limit=101").status_code == 422
    assert client.get("/api/reports?offset=1001").status_code == 422


def test_ledger_replay_cap_returns_degraded_error_without_unbounded_history(tmp_path, monkeypatch):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute("INSERT INTO ledger_accounts (account_id, name, created_at) VALUES (?, ?, ?)", ("default", "default", "2026-07-10T08:30:00"))
    for transaction_id, trade_date in (("cash-1", "2026-07-10"), ("cash-2", "2026-07-11")):
        connection.execute(
            "INSERT INTO ledger_transactions (transaction_id, account_id, trade_date, transaction_type, quantity, price, amount, fees, source, created_at) VALUES (?, ?, ?, 'cash_deposit', 0, 0, 1, 0, 'manual', ?)",
            (transaction_id, "default", trade_date, f"{trade_date}T08:30:00"),
        )
    connection.commit()
    connection.close()
    monkeypatch.setattr(web_api, "_MAX_LEDGER_REPLAY_ROWS", 1, raising=False)

    response = TestClient(create_app(tmp_path)).get("/api/ledger/transactions")

    assert response.status_code == 503
    assert response.json()["detail"] == "ledger history exceeds replay limit"


def test_current_state_reads_verified_reports_and_local_dashboard_fixtures(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    today = date.today().isoformat()
    advice = [AdviceItem("advice-1", "600519", "watch", 0.7, "fixture rationale", ["evidence-1"])]
    write_premarket_report(today, advice, reports_root, quality_results=[QualityResult("market", "blocking", True, "current")])
    write_review_report(
        today,
        advice,
        [ReviewItem("review-1", "advice-1", "valid", "fixture review")],
        reports_root,
        quality_results=[QualityResult("market", "blocking", True, "current")],
    )
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    chart_path = tmp_path / "charts" / "fixture.png"
    chart_path.parent.mkdir()
    chart_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    now = datetime.now().isoformat()
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) VALUES (?, ?, ?, ?, ?)",
        ("run-1", "premarket", today, "blocked", now),
    )
    connection.execute(
        "INSERT INTO securities (code, name, exchange, concepts_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("600519", "Moutai", "SSE", "[]", now, now),
    )
    connection.execute(
        "INSERT INTO stock_profiles (code, updated_at) VALUES (?, ?)",
        ("600519", now),
    )
    connection.execute(
        "INSERT INTO events_normalized (evidence_source_id, source_type, source_id, as_of, summary, raw_ref_json) VALUES (?, ?, ?, ?, ?, ?)",
        ("event-1", "mx", "source-1", now, "fixture", "{}"),
    )
    connection.execute(
        "INSERT INTO analyst_outputs (output_id, run_id, role, as_of, summary, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
        ("output-1", "run-1", "market", now, "fixture", "{}"),
    )
    connection.execute(
        "INSERT INTO data_quality_checks (check_id, run_id, check_name, severity, status, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("quality-1", "run-1", "market_stale", "blocking", "failed", "{}", now),
    )
    connection.execute(
        "INSERT INTO chart_assets (asset_id, code, chart_type, as_of, path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("chart-1", "600519", "kline", today, str(chart_path), now),
    )
    connection.commit()
    connection.close()
    (tmp_path / "health.json").write_text('{"components":{"collector":"running","token":"secret"}}', encoding="utf-8")
    client = TestClient(create_app(tmp_path))
    client.post(
        "/api/ledger/transactions",
        json={"transaction_id": "cash-1", "trade_date": today, "transaction_type": "cash_deposit", "quantity": 0, "price": 0, "amount": 1000, "fees": 0},
    )

    payload = client.get("/api/current-state").json()

    assert payload["advice"] == []
    assert payload["advice_status"] == "blocked"
    assert payload["review"] == {"status": "passed", "items": [ReviewItem("review-1", "advice-1", "valid", "fixture review").to_dict()]}
    assert payload["ledger"]["cash"] == 1000.0
    assert payload["flows"] == {
        "information": {"status": "ok", "count": 1},
        "capital": {"status": "ok", "count": 0},
        "analyst": {"status": "ok", "count": 1},
    }
    assert payload["blocking_quality_checks"] == [{"check_name": "market_stale", "severity": "blocking", "status": "failed", "created_at": now}]
    assert payload["reports"][0]["report_date"] == today
    assert payload["profiles"] == [{"code": "600519", "name": "Moutai", "href": "/api/profiles/600519"}]
    assert payload["charts"][0]["asset_id"] == "chart-1"
    assert payload["health"]["collector"] == "running"
    assert "token" not in payload["health"]


def test_current_state_ignores_yesterday_quality_failure_for_today_passed_report(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    today = date.today().isoformat()
    advice = [AdviceItem("advice-1", "600519", "watch", 0.7, "fixture", ["evidence-1"])]
    write_premarket_report(today, advice, reports_root, quality_results=[QualityResult("market", "blocking", True, "current")])
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    yesterday = "2026-01-01" if today != "2026-01-01" else "2026-01-02"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) VALUES (?, ?, ?, ?, ?)",
        ("yesterday-run", "premarket", yesterday, "failed", "2026-01-01T08:30:00"),
    )
    connection.execute(
        "INSERT INTO data_quality_checks (check_id, run_id, check_name, severity, status, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("old-check", "yesterday-run", "old_failure", "blocking", "failed", "{}", "2026-01-01T08:30:00"),
    )
    connection.commit()
    connection.close()

    payload = TestClient(create_app(tmp_path)).get("/api/current-state").json()

    assert payload["advice"] == [advice[0].to_dict()]
    assert payload["advice_status"] == "passed"
    assert payload["blocking_quality_checks"] == []


def test_current_blocked_run_without_checks_fails_closed(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    today = date.today().isoformat()
    advice = [AdviceItem("advice-1", "600519", "watch", 0.7, "fixture", ["evidence-1"])]
    write_premarket_report(today, advice, reports_root, quality_results=[QualityResult("market", "blocking", True, "current")])
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) VALUES (?, ?, ?, ?, ?)",
        ("blocked-run", "premarket", today, "blocked", "2026-07-11T08:30:00"),
    )
    connection.commit()
    connection.close()

    payload = TestClient(create_app(tmp_path)).get("/api/current-state").json()

    assert payload["advice"] == []
    assert payload["advice_status"] == "blocked"


def test_current_state_fails_closed_for_malformed_report_quality(tmp_path, monkeypatch):
    malformed_report = {"json": {"quality_status": "not-valid", "advice": [{"advice_id": "advice-1"}]}}
    monkeypatch.setattr(web_api, "_read_report_links", lambda: [])
    monkeypatch.setattr(web_api, "_latest_report", lambda _today, report_type, _links: malformed_report if report_type == "premarket" else None)

    payload = TestClient(create_app(tmp_path)).get("/api/current-state").json()

    assert payload["advice"] == []
    assert payload["advice_status"] == "blocked"
