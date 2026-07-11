import asyncio
import inspect
import json
import os
import sqlite3
import tomllib
from datetime import date, datetime, timedelta
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
    assert payload["report_list"]["status"] == "ok"
    assert payload["profiles"] == []
    assert payload["charts"] == []


def test_report_routes_list_and_serve_only_verified_archives(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    state_dir = tmp_path / "state"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    monkeypatch.setattr(web_api, "_shanghai_today", lambda: date(2026, 7, 12))
    write_premarket_report(
        "2026-07-11",
        [],
        reports_root,
        quality_results=[QualityResult("market_data", "blocking", True, "current")],
    )
    db_path = state_dir / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) VALUES (?, ?, ?, ?, ?)",
        ("current-run", "premarket", "2026-07-12", "passed", "2026-07-12T08:30:00+08:00"),
    )
    connection.execute(
        "INSERT INTO data_quality_checks (check_id, run_id, check_name, severity, status, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("current-check", "current-run", "market", "blocking", "passed", "{}", "2026-07-12T08:31:00+08:00"),
    )
    connection.commit()
    connection.close()
    client = TestClient(create_app(state_dir))

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


def test_report_cursor_cannot_be_replayed_after_app_restart(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    for report_date in ("2026-07-10", "2026-07-11"):
        write_premarket_report(
            report_date,
            [],
            reports_root,
            quality_results=[QualityResult("market_data", "blocking", True, "current")],
        )
    first = TestClient(create_app(tmp_path)).get(
        "/api/reports?start_date=2026-07-10&end_date=2026-07-11&limit=1"
    ).json()

    response = TestClient(create_app(tmp_path)).get(
        "/api/reports",
        params={
            "start_date": "2026-07-10",
            "end_date": "2026-07-11",
            "limit": 1,
            "cursor": first["next_cursor"],
        },
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "report listing unavailable"}


def test_report_cursor_returns_explicit_stale_error_after_archive_addition(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    for report_date in ("2026-07-09", "2026-07-11"):
        write_premarket_report(
            report_date,
            [],
            reports_root,
            quality_results=[QualityResult("market_data", "blocking", True, "current")],
        )
    client = TestClient(create_app(tmp_path))
    first = client.get(
        "/api/reports?start_date=2026-07-09&end_date=2026-07-11&limit=1"
    ).json()
    write_premarket_report(
        "2026-07-10",
        [],
        reports_root,
        quality_results=[QualityResult("market_data", "blocking", True, "current")],
    )

    response = client.get(
        "/api/reports",
        params={
            "start_date": "2026-07-09",
            "end_date": "2026-07-11",
            "limit": 1,
            "cursor": first["next_cursor"],
        },
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "report cursor stale"}


def test_report_api_listing_contains_no_failure_links(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    paths = write_premarket_report(
        "2026-07-11",
        [],
        reports_root,
        quality_results=[QualityResult("market_data", "blocking", True, "current")],
    )
    marker_path = paths.json_path.with_name("premarket.complete.json")
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    paths.markdown_path.replace(paths.markdown_path.with_name("failure.md"))
    paths.json_path.replace(paths.json_path.with_name("failure.json"))
    marker["report_time"] = "22:30"
    marker["report_type"] = "failure"
    marker["files"]["markdown"]["name"] = "failure.md"
    marker["files"]["json"]["name"] = "failure.json"
    marker_path.unlink()
    marker_path.with_name("failure.complete.json").write_text(json.dumps(marker), encoding="utf-8")

    payload = TestClient(create_app(tmp_path)).get(
        "/api/reports?start_date=2026-07-11&end_date=2026-07-11"
    ).json()

    assert payload["items"] == []
    assert payload["reports"] == []


def test_current_state_uses_bounded_report_page_and_degrades_on_overflow(tmp_path, monkeypatch):
    calls = []

    def overflowing_page(*args, **kwargs):
        calls.append(kwargs)
        raise ValueError("archive candidate limit exceeded")

    monkeypatch.setattr(web_api, "page_verified_archives", overflowing_page)

    payload = TestClient(create_app(tmp_path)).get("/api/current-state").json()

    assert len(calls) == 1
    assert calls[0]["limit"] <= 20
    assert calls[0]["start_date"] is not None
    assert calls[0]["end_date"] is not None
    assert len(calls[0]["cursor_secret"]) >= 32
    assert payload["reports"] == []
    assert payload["report_list"] == {"status": "degraded", "truncated": True}


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


@pytest.mark.parametrize(
    ("payload", "constant", "bound"),
    [
        ({"value": "x" * 32}, "_MAX_PROFILE_JSON_BYTES", 16),
        ({"a": {"b": {"c": 1}}}, "_MAX_PROFILE_JSON_DEPTH", 2),
        ({"a": 1, "b": 2, "c": 3}, "_MAX_PROFILE_JSON_ITEMS", 2),
        ({"value": "too-long"}, "_MAX_PROFILE_STRING_LENGTH", 4),
    ],
)
def test_profile_json_fields_enforce_bytes_depth_items_and_string_bounds(monkeypatch, payload, constant, bound):
    monkeypatch.setattr(web_api, constant, bound, raising=False)

    with pytest.raises(ValueError, match="invalid stored json"):
        web_api._load_json_field(json.dumps(payload), dict)


@pytest.mark.parametrize("stored", ["[" * 2000 + "]" * 2000, '"\ud800"'])
def test_profile_json_fields_reject_parser_depth_and_encoding_failures(stored):
    with pytest.raises(ValueError, match="invalid stored json"):
        web_api._load_json_field(stored, list if stored.startswith("[") else str)


@pytest.mark.parametrize(
    ("table", "field", "value"),
    [
        ("securities", "name", "n" * 257),
        ("securities", "industry", "i" * 257),
        ("stock_profiles", "updated_at", "2026-07-11T08:30:00" + "x" * 64),
    ],
)
def test_profile_routes_exclude_unbounded_db_scalars(tmp_path, table, field, value):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO securities (code, name, exchange, industry, concepts_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("600519", "Moutai", "SSE", "Beverages", "[]", "2026-07-11T08:00:00+08:00", "2026-07-11T08:00:00+08:00"),
    )
    connection.execute(
        "INSERT INTO stock_profiles (code, updated_at) VALUES (?, ?)",
        ("600519", "2026-07-11T08:30:00+08:00"),
    )
    connection.execute(f"UPDATE {table} SET {field} = ? WHERE code = ?", (value, "600519"))
    connection.commit()
    connection.close()
    client = TestClient(create_app(tmp_path))

    assert client.get("/api/profiles").json()["profiles"] == []
    assert client.get("/api/profiles/600519").status_code == 404


def test_profile_json_rejects_huge_integer_without_overflow():
    with pytest.raises(ValueError, match="invalid stored json"):
        web_api._load_json_field('{"value":' + "9" * 100 + "}", dict)


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("code", "x" * 200),
        ("chart_type", "x" * 65),
        ("as_of", "2026-07-11T08:30:00" + "x" * 64),
    ],
)
def test_chart_listing_excludes_unbounded_db_metadata(tmp_path, field, value):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    chart_path = tmp_path / "charts" / "bounded.png"
    chart_path.parent.mkdir()
    chart_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    row = {
        "asset_id": "chart-1",
        "code": "600519",
        "chart_type": "kline",
        "as_of": "2026-07-11",
    }
    row[field] = value
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO chart_assets (asset_id, code, chart_type, as_of, path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (row["asset_id"], row["code"], row["chart_type"], row["as_of"], str(chart_path), "2026-07-11T08:30:00+08:00"),
    )
    connection.commit()
    connection.close()

    assert TestClient(create_app(tmp_path)).get("/api/charts").json()["charts"] == []


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


@pytest.mark.parametrize("transaction_id", ["../escape", "x" * 129])
def test_ledger_transaction_ids_are_bounded_and_path_neutral(tmp_path, transaction_id):
    response = TestClient(create_app(tmp_path)).post(
        "/api/ledger/transactions",
        json={
            "transaction_id": transaction_id,
            "trade_date": "2026-07-11",
            "transaction_type": "cash_deposit",
            "quantity": 0,
            "price": 0,
            "amount": 100,
            "fees": 0,
        },
    )

    assert response.status_code == 422
    assert not (tmp_path / "advisor.sqlite").exists()


def test_ledger_read_excludes_unbounded_stored_account_id(tmp_path):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    account_id = "a" * 65
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO ledger_accounts (account_id, name, created_at) VALUES (?, ?, ?)",
        (account_id, account_id, "2026-07-11T08:00:00+08:00"),
    )
    connection.execute(
        "INSERT INTO ledger_transactions (transaction_id, account_id, trade_date, transaction_type, quantity, price, amount, fees, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("cash-1", account_id, "2026-07-11", "cash_deposit", 0, 0, 100, 0, "manual", "2026-07-11T08:30:00+08:00"),
    )
    connection.commit()
    connection.close()

    payload = TestClient(create_app(tmp_path)).get("/api/ledger/transactions").json()

    assert payload["transactions"] == []
    assert payload["ledger"] == web_api._empty_ledger_state()


def test_ledger_writes_shanghai_aware_created_at(tmp_path):
    response = TestClient(create_app(tmp_path)).post(
        "/api/ledger/transactions",
        json={
            "transaction_id": "cash-1",
            "trade_date": "2026-07-11",
            "transaction_type": "cash_deposit",
            "quantity": 0,
            "price": 0,
            "amount": 100,
            "fees": 0,
        },
    )
    connection = sqlite3.connect(tmp_path / "advisor.sqlite")
    account_created_at = connection.execute("SELECT created_at FROM ledger_accounts").fetchone()[0]
    transaction_created_at = connection.execute("SELECT created_at FROM ledger_transactions").fetchone()[0]
    connection.close()

    assert response.status_code == 201
    for stored in (account_created_at, transaction_created_at):
        parsed = datetime.fromisoformat(stored)
        assert parsed.utcoffset() == timedelta(hours=8)


def test_ledger_rejects_huge_json_integer_without_overflow(tmp_path):
    response = TestClient(create_app(tmp_path)).post(
        "/api/ledger/transactions",
        json={
            "transaction_id": "cash-1",
            "trade_date": "2026-07-11",
            "transaction_type": "cash_deposit",
            "quantity": 0,
            "price": 0,
            "amount": 10**100,
            "fees": 0,
        },
    )

    assert response.status_code == 422


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
    assert client.get("/api/reports?cursor=not-a-valid-cursor").status_code == 503


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


def test_ledger_capacity_crossing_rejects_before_commit(tmp_path, monkeypatch):
    monkeypatch.setattr(web_api, "_MAX_LEDGER_REPLAY_ROWS", 1)
    client = TestClient(create_app(tmp_path))
    first = {"transaction_id": "cash-1", "trade_date": "2026-07-10", "transaction_type": "cash_deposit", "quantity": 0, "price": 0, "amount": 1, "fees": 0}
    second = {"transaction_id": "cash-2", "trade_date": "2026-07-11", "transaction_type": "cash_deposit", "quantity": 0, "price": 0, "amount": 1, "fees": 0}

    assert client.post("/api/ledger/transactions", json=first).status_code == 201
    rejected = client.post("/api/ledger/transactions", json=second)
    database = sqlite3.connect(tmp_path / "advisor.sqlite")
    count = database.execute("SELECT COUNT(*) FROM ledger_transactions").fetchone()[0]
    database.close()

    assert rejected.status_code == 503
    assert rejected.json()["detail"] == "ledger history exceeds replay limit"
    assert count == 1


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
    assert payload["review"] == {"status": "blocked", "items": []}
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
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) VALUES (?, ?, ?, ?, ?)",
        ("today-run", "premarket", today, "passed", f"{today}T08:30:00+08:00"),
    )
    connection.execute(
        "INSERT INTO data_quality_checks (check_id, run_id, check_name, severity, status, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("today-check", "today-run", "market", "blocking", "passed", "{}", f"{today}T08:31:00+08:00"),
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


def test_newer_timestamped_current_day_failure_run_blocks_passed_archive(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    today = date.today().isoformat()
    advice = [AdviceItem("advice-1", "600519", "watch", 0.7, "fixture", ["evidence-1"])]
    write_premarket_report(today, advice, reports_root, quality_results=[QualityResult("market", "blocking", True, "current")])
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute("INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) VALUES (?, ?, ?, ?, ?)", ("passed-run", "premarket", f"{today}T08:30:00", "passed", f"{today}T08:30:00"))
    connection.execute("INSERT INTO data_quality_checks (check_id, run_id, check_name, severity, status, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", ("passed-check", "passed-run", "market", "blocking", "passed", "{}", f"{today}T08:30:00"))
    connection.execute("INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) VALUES (?, ?, ?, ?, ?)", ("failure-run", "failure", f"{today}T09:00:00", "failed", f"{today}T09:00:00"))
    connection.commit()
    connection.close()

    payload = TestClient(create_app(tmp_path)).get("/api/current-state").json()

    assert payload["advice"] == []
    assert payload["advice_status"] == "blocked"


def test_current_state_fails_closed_for_malformed_report_quality(tmp_path, monkeypatch):
    malformed_report = {"json": {"quality_status": "not-valid", "advice": [{"advice_id": "advice-1"}]}}
    monkeypatch.setattr(web_api, "_read_report_links", lambda _today, _secret: ([], {"status": "ok", "truncated": False}))
    monkeypatch.setattr(web_api, "_read_today_report", lambda _today, report_type: malformed_report if report_type == "premarket" else None)

    payload = TestClient(create_app(tmp_path)).get("/api/current-state").json()

    assert payload["advice"] == []
    assert payload["advice_status"] == "blocked"


def test_current_quality_resolver_applies_utc_rollover_blocking_check(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    monkeypatch.setattr(web_api, "_shanghai_today", lambda: date(2026, 7, 12))
    advice = [AdviceItem("advice-1", "600519", "watch", 0.7, "must stay private", ["evidence-1"])]
    write_premarket_report(
        "2026-07-12",
        advice,
        reports_root,
        quality_results=[QualityResult("market", "blocking", True, "current")],
    )
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) VALUES (?, ?, ?, ?, ?)",
        ("utc-run", "premarket", "2026-07-11T16:30:00Z", "passed", "2026-07-11T16:30:00Z"),
    )
    connection.execute(
        "INSERT INTO data_quality_checks (check_id, run_id, check_name, severity, status, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("utc-check", "utc-run", "market_stale", "blocking", "failed", "{}", "2026-07-11T16:31:00Z"),
    )
    connection.commit()
    connection.close()

    payload = TestClient(create_app(tmp_path)).get("/api/current-state").json()

    assert payload["advice"] == []
    assert payload["advice_status"] == "blocked"
    assert [check["check_name"] for check in payload["blocking_quality_checks"]] == ["market_stale"]


def test_current_quality_resolver_orders_conflicting_offsets_by_shanghai_instant(tmp_path, monkeypatch):
    monkeypatch.setattr(web_api, "_shanghai_today", lambda: date(2026, 7, 12))
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) VALUES (?, ?, ?, ?, ?)",
        ("passed-local", "premarket", "2026-07-12T09:30:00+08:00", "passed", "2026-07-12T09:30:00+08:00"),
    )
    connection.execute(
        "INSERT INTO data_quality_checks (check_id, run_id, check_name, severity, status, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("passed-check", "passed-local", "market", "blocking", "passed", "{}", "2026-07-12T09:31:00+08:00"),
    )
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) VALUES (?, ?, ?, ?, ?)",
        ("failure-z", "failure", "2026-07-12T02:00:00Z", "failed", "2026-07-12T02:00:00Z"),
    )
    connection.commit()
    connection.close()

    payload = TestClient(create_app(tmp_path)).get("/api/current-state").json()

    assert payload["advice_status"] == "blocked"


@pytest.mark.parametrize("field", ["as_of", "started_at"])
def test_current_quality_resolver_fails_closed_for_malformed_competing_run(tmp_path, monkeypatch, field):
    monkeypatch.setattr(web_api, "_shanghai_today", lambda: date(2026, 7, 12))
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    values = {
        "as_of": "2026-07-12T09:00:00+08:00",
        "started_at": "2026-07-12T09:00:00+08:00",
    }
    values[field] = "2026-07-12garbage"
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) VALUES (?, ?, ?, ?, ?)",
        ("malformed-run", "premarket", values["as_of"], "passed", values["started_at"]),
    )
    connection.commit()
    connection.close()

    payload = TestClient(create_app(tmp_path)).get("/api/current-state").json()

    assert payload["advice_status"] == "blocked"


def test_current_quality_resolver_discovers_malformed_as_of_by_current_started_at(tmp_path, monkeypatch):
    monkeypatch.setattr(web_api, "_shanghai_today", lambda: date(2026, 7, 12))
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) VALUES (?, ?, ?, ?, ?)",
        ("malformed-as-of", "premarket", "not-a-date", "passed", "2026-07-12T09:00:00+08:00"),
    )
    connection.commit()
    connection.close()

    payload = TestClient(create_app(tmp_path)).get("/api/current-state").json()

    assert payload["advice"] == []
    assert payload["advice_status"] == "blocked"


def test_current_quality_resolver_fails_closed_on_candidate_overflow(tmp_path, monkeypatch):
    monkeypatch.setattr(web_api, "_shanghai_today", lambda: date(2026, 7, 12))
    monkeypatch.setattr(web_api, "_MAX_CURRENT_RUN_CANDIDATES", 1)
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    for index in range(2):
        connection.execute(
            "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) VALUES (?, ?, ?, ?, ?)",
            (f"run-{index}", "premarket", "2026-07-12", "passed", f"2026-07-12T0{index + 8}:00:00+08:00"),
        )
    connection.commit()
    connection.close()

    payload = TestClient(create_app(tmp_path)).get("/api/current-state").json()

    assert payload["advice_status"] == "blocked"


def test_current_quality_resolver_rejects_malformed_check_details(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    monkeypatch.setattr(web_api, "_shanghai_today", lambda: date(2026, 7, 12))
    advice = [AdviceItem("advice-1", "600519", "watch", 0.7, "must stay private", ["evidence-1"])]
    write_premarket_report(
        "2026-07-12",
        advice,
        reports_root,
        quality_results=[QualityResult("market", "blocking", True, "current")],
    )
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) VALUES (?, ?, ?, ?, ?)",
        ("passed-run", "premarket", "2026-07-12", "passed", "2026-07-12T08:30:00+08:00"),
    )
    connection.execute(
        "INSERT INTO data_quality_checks (check_id, run_id, check_name, severity, status, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("bad-check", "passed-run", "market", "blocking", "passed", "not-json", "2026-07-12T08:31:00+08:00"),
    )
    connection.commit()
    connection.close()

    payload = TestClient(create_app(tmp_path)).get("/api/current-state").json()

    assert payload["advice"] == []
    assert payload["advice_status"] == "blocked"


def test_current_report_content_is_withheld_when_active_quality_is_unsafe(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    monkeypatch.setattr(web_api, "_shanghai_today", lambda: date(2026, 7, 12))
    advice = [AdviceItem("advice-1", "600519", "watch", 0.7, "must stay private", ["evidence-1"])]
    write_premarket_report(
        "2026-07-12",
        advice,
        reports_root,
        quality_results=[QualityResult("market", "blocking", True, "current")],
    )
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) VALUES (?, ?, ?, ?, ?)",
        ("blocked-run", "premarket", "2026-07-12T08:30:00+08:00", "blocked", "2026-07-12T08:30:00+08:00"),
    )
    connection.commit()
    connection.close()

    response = TestClient(create_app(tmp_path)).get("/api/reports/2026-07-12/premarket?run_id=initial")

    assert response.status_code == 503
    assert response.json() == {"detail": "current report quality unavailable"}
    assert "must stay private" not in response.text


def test_present_unreadable_database_fails_closed_for_current_report_content(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    monkeypatch.setattr(web_api, "_shanghai_today", lambda: date(2026, 7, 12))
    advice = [AdviceItem("advice-1", "600519", "watch", 0.7, "must stay private", ["evidence-1"])]
    write_premarket_report(
        "2026-07-12",
        advice,
        reports_root,
        quality_results=[QualityResult("market", "blocking", True, "current")],
    )
    outside = tmp_path / "outside.sqlite"
    outside.write_bytes(b"not a database")
    (tmp_path / "advisor.sqlite").symlink_to(outside)
    client = TestClient(create_app(tmp_path))

    state = client.get("/api/current-state")
    report = client.get("/api/reports/2026-07-12/premarket?run_id=initial")

    assert state.json()["advice"] == []
    assert state.json()["advice_status"] == "blocked"
    assert report.status_code == 503
    assert "must stay private" not in report.text


def test_absent_database_cannot_authorize_verified_current_report(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    monkeypatch.setattr(web_api, "_shanghai_today", lambda: date(2026, 7, 12))
    advice = [AdviceItem("advice-1", "600519", "watch", 0.7, "verified archive", ["evidence-1"])]
    write_premarket_report(
        "2026-07-12",
        advice,
        reports_root,
        quality_results=[QualityResult("market", "blocking", True, "current")],
    )
    client = TestClient(create_app(tmp_path))

    state = client.get("/api/current-state")
    report = client.get("/api/reports/2026-07-12/premarket?run_id=initial")

    assert state.json()["advice"] == []
    assert state.json()["advice_status"] == "blocked"
    assert report.status_code == 503
    assert "verified archive" not in report.text


def test_current_failure_blocks_historical_report_content(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    monkeypatch.setattr(web_api, "_shanghai_today", lambda: date(2026, 7, 12))
    advice = [AdviceItem("advice-1", "600519", "watch", 0.7, "historical conclusion", ["evidence-1"])]
    write_premarket_report(
        "2026-07-11",
        advice,
        reports_root,
        quality_results=[QualityResult("market", "blocking", True, "historical")],
    )
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) VALUES (?, ?, ?, ?, ?)",
        ("current-failure", "failure", "2026-07-12", "failed", "2026-07-12T09:00:00+08:00"),
    )
    connection.commit()
    connection.close()

    response = TestClient(create_app(tmp_path)).get("/api/reports/2026-07-11/premarket?run_id=initial")

    assert response.status_code == 503
    assert response.json() == {"detail": "current report quality unavailable"}
    assert "historical conclusion" not in response.text


def test_report_content_route_rejects_failure_archives(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)

    response = TestClient(create_app(tmp_path)).get("/api/reports/2026-07-11/failure?run_id=initial")

    assert response.status_code == 404
