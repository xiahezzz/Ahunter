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


def run_streamed_request(app, path: str, chunks: list[bytes]) -> tuple[int, dict, int]:
    messages = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]
    sent: list[dict] = []
    receive_count = 0

    async def receive():
        nonlocal receive_count
        receive_count += 1
        if messages:
            return messages.pop(0)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }

    asyncio.run(app(scope, receive, send))
    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )
    return status, json.loads(body), receive_count


def insert_report_archive(
    connection: sqlite3.Connection,
    *,
    database_run_id: str,
    report_type: str,
    report_date: str,
    markdown_path: Path,
    json_path: Path,
) -> None:
    connection.execute(
        """
        INSERT INTO report_archive (
          report_id, run_id, report_type, report_date, markdown_path, json_path, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"report-{database_run_id}-{report_type}",
            database_run_id,
            report_type,
            report_date,
            str(markdown_path),
            str(json_path),
            datetime.now().isoformat(),
        ),
    )


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
    assert {"today", "last_successful_data_update", "advice", "review", "ledger", "flows", "blocking_quality_checks", "reports", "report_list", "profiles", "profile_list", "charts", "chart_list", "health"} <= payload.keys()
    assert payload["last_successful_data_update"] is None
    assert payload["advice"] == []
    assert payload["review"] == {"status": "missing", "items": []}
    assert payload["ledger"] == {
        "cash": 0.0,
        "positions": [],
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "accounts": [],
        "status": "unknown",
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
    assert payload["profile_list"] == {"status": "degraded"}
    assert payload["charts"] == []
    assert payload["chart_list"] == {"status": "degraded"}


def test_report_routes_list_and_serve_only_verified_archives(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    state_dir = tmp_path / "state"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    monkeypatch.setattr(web_api, "_shanghai_today", lambda: date(2026, 7, 12))
    archive_paths = write_premarket_report(
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
    insert_report_archive(
        connection,
        database_run_id="current-run",
        report_type="premarket",
        report_date="2026-07-11",
        markdown_path=archive_paths.markdown_path,
        json_path=archive_paths.json_path,
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


def test_small_report_page_verifies_only_bounded_archive_candidates(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    start = date(2026, 1, 1)
    for index in range(180):
        report_date = (start + timedelta(days=index)).isoformat()
        paths = write_premarket_report(
            report_date,
            [],
            reports_root,
            quality_results=[QualityResult("market", "blocking", True, "current")],
        )
        run_id = f"history-{index}"
        connection.execute(
            "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) "
            "VALUES (?, 'premarket', ?, 'passed', ?)",
            (run_id, report_date, f"{report_date}T08:30:00+08:00"),
        )
        insert_report_archive(
            connection,
            database_run_id=run_id,
            report_type="premarket",
            report_date=report_date,
            markdown_path=paths.markdown_path,
            json_path=paths.json_path,
        )
    connection.commit()
    connection.close()
    verification_calls = 0
    real_reader = web_api.read_verified_archive

    def counted_reader(*args, **kwargs):
        nonlocal verification_calls
        verification_calls += 1
        return real_reader(*args, **kwargs)

    monkeypatch.setattr(web_api, "read_verified_archive", counted_reader)

    response = TestClient(create_app(tmp_path, db_path=db_path)).get(
        "/api/reports?start_date=2026-01-01&end_date=2026-06-29&limit=1"
    )

    assert response.status_code == 200
    assert response.json()["reports"][0]["report_date"] == "2026-06-29"
    assert response.json()["truncated"] is True
    assert verification_calls <= 20


def test_report_page_with_only_invalid_candidates_returns_empty_truncated_page(
    tmp_path, monkeypatch
):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    monkeypatch.setattr(web_api, "_MAX_REPORT_ARCHIVE_ROWS", 3)
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    for index in range(3):
        run_id = f"invalid-{index}"
        connection.execute(
            "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) "
            "VALUES (?, 'premarket', '2026-07-12', 'passed', '2026-07-12T08:30:00+08:00')",
            (run_id,),
        )
        connection.execute(
            "INSERT INTO report_archive "
            "(report_id, run_id, report_type, report_date, markdown_path, json_path, created_at) "
            "VALUES (?, ?, 'premarket', '2026-07-12', ?, ?, '2026-07-12T08:31:00+08:00')",
            (
                f"report-{index}",
                run_id,
                str(reports_root / "outside" / f"premarket.{run_id}.md"),
                str(reports_root / "outside" / f"premarket.{run_id}.json"),
            ),
        )
    connection.commit()
    connection.close()

    response = TestClient(create_app(tmp_path, db_path=db_path)).get(
        "/api/reports?start_date=2026-07-12&end_date=2026-07-12&limit=1"
    )

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["next_cursor"] is None
    assert response.json()["truncated"] is True


def test_report_routes_hide_verified_archive_without_committed_archive_row(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    monkeypatch.setattr(web_api, "_shanghai_today", lambda: date(2026, 7, 12))
    write_premarket_report(
        "2026-07-11",
        [],
        reports_root,
        quality_results=[QualityResult("market_data", "blocking", True, "current")],
    )
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("current-run", "premarket", "2026-07-12", "passed", "2026-07-12T08:30:00+08:00"),
    )
    connection.execute(
        "INSERT INTO data_quality_checks (check_id, run_id, check_name, severity, status, "
        "details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("current-check", "current-run", "market", "blocking", "passed", "{}", "2026-07-12T08:31:00+08:00"),
    )
    connection.commit()
    connection.close()
    client = TestClient(create_app(tmp_path))

    listing = client.get(
        "/api/reports?start_date=2026-07-11&end_date=2026-07-11"
    )

    assert listing.status_code == 200
    assert listing.json()["reports"] == []
    assert client.get(
        "/api/reports/2026-07-11/premarket?run_id=initial"
    ).status_code == 404


def test_current_report_remains_eligible_after_archive_history_exceeds_capacity(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    monkeypatch.setattr(web_api, "_shanghai_today", lambda: date(2026, 7, 12))
    advice = [AdviceItem("advice-1", "600519", "watch", 0.7, "current", ["evidence-1"])]
    report_paths = write_premarket_report(
        "2026-07-12",
        advice,
        reports_root,
        quality_results=[QualityResult("market", "blocking", True, "current")],
    )
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    historical_runs = [
        (f"historical-{index}", "premarket", "2020-01-01", "passed", "2020-01-01T08:30:00+08:00")
        for index in range(web_api._MAX_REPORT_ARCHIVE_ROWS + 1)
    ]
    connection.executemany(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) "
        "VALUES (?, ?, ?, ?, ?)",
        historical_runs,
    )
    connection.executemany(
        "INSERT INTO report_archive "
        "(report_id, run_id, report_type, report_date, markdown_path, json_path, created_at) "
        "VALUES (?, ?, 'premarket', '2020-01-01', ?, ?, '2020-01-01T08:31:00+08:00')",
        [
            (
                f"historical-report-{index}",
                run_id,
                str(reports_root / "2020-01-01" / f"premarket.history-{index}.md"),
                str(reports_root / "2020-01-01" / f"premarket.history-{index}.json"),
            )
            for index, (run_id, *_rest) in enumerate(historical_runs)
        ],
    )
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("current-run", "premarket", "2026-07-12", "passed", "2026-07-12T08:30:00+08:00"),
    )
    connection.execute(
        "INSERT INTO data_quality_checks "
        "(check_id, run_id, check_name, severity, status, details_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("current-check", "current-run", "market", "blocking", "passed", "{}", "2026-07-12T08:31:00+08:00"),
    )
    insert_report_archive(
        connection,
        database_run_id="current-run",
        report_type="premarket",
        report_date="2026-07-12",
        markdown_path=report_paths.markdown_path,
        json_path=report_paths.json_path,
    )
    connection.commit()
    connection.close()
    client = TestClient(create_app(tmp_path))

    listing = client.get("/api/reports?start_date=2026-07-12&end_date=2026-07-12")
    detail = client.get("/api/reports/2026-07-12/premarket?run_id=initial")
    current_state = client.get("/api/current-state")

    assert [report["report_date"] for report in listing.json()["reports"]] == ["2026-07-12"]
    assert detail.status_code == 200
    assert detail.json()["json"]["advice"] == [advice[0].to_dict()]
    assert current_state.json()["advice"] == [advice[0].to_dict()]
    assert [report["report_date"] for report in current_state.json()["reports"]] == ["2026-07-12"]


def test_current_state_keeps_active_chain_after_same_day_archive_capacity(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    monkeypatch.setattr(web_api, "_shanghai_today", lambda: date(2026, 7, 12))
    initial_advice = AdviceItem("advice-initial", "600519", "watch", 0.6, "initial", [])
    rerun_advice = AdviceItem("advice-rerun", "000001", "watch", 0.8, "rerun", [])
    initial_paths = write_premarket_report(
        "2026-07-12",
        [initial_advice],
        reports_root,
        quality_results=[QualityResult("market", "blocking", True, "initial")],
    )
    rerun_paths = write_premarket_report(
        "2026-07-12",
        [rerun_advice],
        reports_root,
        quality_results=[QualityResult("market", "blocking", True, "rerun")],
        run_id="rerun1",
        rerun_reason="candidate update",
        supersedes="initial",
    )
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    noisy_runs = [
        (
            f"noise-{index}",
            "premarket",
            "2020-01-01T08:30:00+08:00",
            "passed",
            "2020-01-01T08:30:00+08:00",
        )
        for index in range(web_api._MAX_REPORT_ARCHIVE_ROWS + 1)
    ]
    connection.executemany(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) "
        "VALUES (?, ?, ?, ?, ?)",
        noisy_runs,
    )
    connection.executemany(
        "INSERT INTO report_archive "
        "(report_id, run_id, report_type, report_date, markdown_path, json_path, created_at) "
        "VALUES (?, ?, 'premarket', '2026-07-12', ?, ?, '2026-07-12T09:00:00+08:00')",
        [
            (
                f"noise-report-{index}",
                run_id,
                str(reports_root / "2026-07-12" / f"premarket.noise-{index}.md"),
                str(reports_root / "2026-07-12" / f"premarket.noise-{index}.json"),
            )
            for index, (run_id, *_rest) in enumerate(noisy_runs)
        ],
    )
    connection.executemany(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) "
        "VALUES (?, 'premarket', '2026-07-12T08:30:00+08:00', 'passed', ?)",
        [
            ("current-initial", "2026-07-12T08:30:00+08:00"),
            ("current-rerun", "2026-07-12T08:31:00+08:00"),
        ],
    )
    connection.execute(
        "INSERT INTO data_quality_checks "
        "(check_id, run_id, check_name, severity, status, details_json, created_at) "
        "VALUES ('current-check', 'current-rerun', 'market', 'blocking', 'passed', '{}', "
        "'2026-07-12T08:32:00+08:00')"
    )
    insert_report_archive(
        connection,
        database_run_id="current-initial",
        report_type="premarket",
        report_date="2026-07-12",
        markdown_path=initial_paths.markdown_path,
        json_path=initial_paths.json_path,
    )
    insert_report_archive(
        connection,
        database_run_id="current-rerun",
        report_type="premarket",
        report_date="2026-07-12",
        markdown_path=rerun_paths.markdown_path,
        json_path=rerun_paths.json_path,
    )
    connection.commit()
    connection.close()

    payload = TestClient(create_app(tmp_path)).get("/api/current-state").json()

    assert payload["advice"] == [rerun_advice.to_dict()]
    assert any(report["run_id"] == "rerun1" for report in payload["reports"])


def test_report_apis_keep_db_backed_archive_when_same_day_directory_exceeds_scan_limit(
    tmp_path, monkeypatch
):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    monkeypatch.setattr(web_api, "_shanghai_today", lambda: date(2026, 7, 12))
    advice = [AdviceItem("advice-current", "600519", "watch", 0.7, "current", [])]
    report_paths = write_premarket_report(
        "2026-07-12",
        advice,
        reports_root,
        quality_results=[QualityResult("market", "blocking", True, "current")],
    )
    report_directory = reports_root / "2026-07-12"
    for index in range(168):
        for suffix in ("md", "json", "complete.json"):
            (report_directory / f"premarket.noise{index:03d}.{suffix}").write_text(
                "noise", encoding="utf-8"
            )

    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) "
        "VALUES ('current-run', 'premarket', '2026-07-12T08:30:00+08:00', "
        "'passed', '2026-07-12T08:30:00+08:00')"
    )
    connection.execute(
        "INSERT INTO data_quality_checks "
        "(check_id, run_id, check_name, severity, status, details_json, created_at) "
        "VALUES ('current-check', 'current-run', 'market', 'blocking', 'passed', '{}', "
        "'2026-07-12T08:31:00+08:00')"
    )
    insert_report_archive(
        connection,
        database_run_id="current-run",
        report_type="premarket",
        report_date="2026-07-12",
        markdown_path=report_paths.markdown_path,
        json_path=report_paths.json_path,
    )
    connection.commit()
    connection.close()
    client = TestClient(create_app(tmp_path))

    current = client.get("/api/current-state")
    listing = client.get("/api/reports?start_date=2026-07-12&end_date=2026-07-12")
    detail = client.get("/api/reports/2026-07-12/premarket?run_id=initial")

    assert current.status_code == 200
    assert current.json()["advice"] == [advice[0].to_dict()]
    assert [item["run_id"] for item in current.json()["reports"]] == ["initial"]
    assert listing.status_code == 200
    assert [item["run_id"] for item in listing.json()["reports"]] == ["initial"]
    assert detail.status_code == 200
    assert detail.json()["json"]["advice"] == [advice[0].to_dict()]


def test_report_cursor_cannot_be_replayed_after_app_restart(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    for index, report_date in enumerate(("2026-07-10", "2026-07-11")):
        paths = write_premarket_report(
            report_date,
            [],
            reports_root,
            quality_results=[QualityResult("market_data", "blocking", True, "current")],
        )
        run_id = f"cursor-run-{index}"
        connection.execute(
            "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) "
            "VALUES (?, 'premarket', ?, 'passed', ?)",
            (run_id, report_date, f"{report_date}T08:30:00+08:00"),
        )
        insert_report_archive(
            connection, database_run_id=run_id, report_type="premarket",
            report_date=report_date, markdown_path=paths.markdown_path,
            json_path=paths.json_path,
        )
    connection.commit()
    connection.close()
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
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    for index, report_date in enumerate(("2026-07-09", "2026-07-11")):
        paths = write_premarket_report(
            report_date,
            [],
            reports_root,
            quality_results=[QualityResult("market_data", "blocking", True, "current")],
        )
        run_id = f"cursor-run-{index}"
        connection.execute(
            "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) "
            "VALUES (?, 'premarket', ?, 'passed', ?)",
            (run_id, report_date, f"{report_date}T08:30:00+08:00"),
        )
        insert_report_archive(
            connection, database_run_id=run_id, report_type="premarket",
            report_date=report_date, markdown_path=paths.markdown_path,
            json_path=paths.json_path,
        )
    connection.commit()
    client = TestClient(create_app(tmp_path))
    first = client.get(
        "/api/reports?start_date=2026-07-09&end_date=2026-07-11&limit=1"
    ).json()
    added_paths = write_premarket_report(
        "2026-07-10",
        [],
        reports_root,
        quality_results=[QualityResult("market_data", "blocking", True, "current")],
    )
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) "
        "VALUES ('cursor-added', 'premarket', '2026-07-10', 'passed', "
        "'2026-07-10T08:30:00+08:00')"
    )
    insert_report_archive(
        connection, database_run_id="cursor-added", report_type="premarket",
        report_date="2026-07-10", markdown_path=added_paths.markdown_path,
        json_path=added_paths.json_path,
    )
    connection.commit()
    connection.close()

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


def test_current_state_degrades_when_db_backed_archive_verification_fails(tmp_path, monkeypatch):
    def failed_verification(*args, **kwargs):
        raise ValueError("archive verification unavailable")

    monkeypatch.setattr(web_api, "_read_verified_report_items", failed_verification)

    payload = TestClient(create_app(tmp_path)).get("/api/current-state").json()

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


def test_current_state_degrades_profile_list_on_validation_failure(tmp_path):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO securities (code, name, exchange, concepts_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("600519", "Moutai", "SSE", "[]", "2026-07-12T08:00:00+08:00", "2026-07-12T08:00:00+08:00"),
    )
    connection.execute(
        "INSERT INTO stock_profiles (code, thesis_json, updated_at) VALUES (?, ?, ?)",
        ("600519", "not-json", "2026-07-12T08:30:00+08:00"),
    )
    connection.commit()
    connection.close()

    payload = TestClient(create_app(tmp_path)).get("/api/current-state").json()

    assert payload["profile_list"] == {"status": "degraded"}
    assert payload["profiles"] == []


def test_current_state_degrades_profile_list_on_read_failure(tmp_path):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute("DROP TABLE stock_profiles")
    connection.commit()
    connection.close()

    payload = TestClient(create_app(tmp_path)).get("/api/current-state").json()

    assert payload["profile_list"] == {"status": "degraded"}
    assert payload["profiles"] == []


def test_current_state_degrades_profile_list_for_prefixed_code(tmp_path):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO stock_profiles (code, updated_at) VALUES (?, ?)",
        ("SH600519", "2026-07-12T08:30:00+08:00"),
    )
    connection.commit()
    connection.close()

    payload = TestClient(create_app(tmp_path)).get("/api/current-state").json()

    assert payload["profile_list"] == {"status": "degraded"}
    assert payload["profiles"] == []


def test_current_state_degrades_profile_list_for_invalid_row_after_display_cap(tmp_path):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.executemany(
        "INSERT INTO stock_profiles (code, updated_at) VALUES (?, ?)",
        [
            (f"{600000 + index:06d}", "2026-07-12T08:30:00+08:00")
            for index in range(100)
        ] + [("SH600519", "2026-07-12T08:30:00+08:00")],
    )
    connection.commit()
    connection.close()

    payload = TestClient(create_app(tmp_path)).get("/api/current-state").json()

    assert payload["profile_list"] == {"status": "degraded"}
    assert payload["profiles"] == []


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


def test_current_state_degrades_chart_list_on_validation_failure(tmp_path):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    chart_path = tmp_path / "charts" / "invalid-metadata.png"
    chart_path.parent.mkdir()
    chart_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO chart_assets (asset_id, code, chart_type, as_of, path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("chart-1", "600519", "kline", "not-a-date", str(chart_path), "2026-07-12T08:30:00+08:00"),
    )
    connection.commit()
    connection.close()

    payload = TestClient(create_app(tmp_path)).get("/api/current-state").json()

    assert payload["chart_list"] == {"status": "degraded"}
    assert payload["charts"] == []


def test_current_state_degrades_chart_list_on_read_failure(tmp_path):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute("DROP TABLE chart_assets")
    connection.commit()
    connection.close()

    payload = TestClient(create_app(tmp_path)).get("/api/current-state").json()

    assert payload["chart_list"] == {"status": "degraded"}
    assert payload["charts"] == []


def test_current_state_degrades_chart_list_for_non_kline_chart(tmp_path):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    chart_path = tmp_path / "charts" / "thumbnail.png"
    chart_path.parent.mkdir()
    chart_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO chart_assets (asset_id, code, chart_type, as_of, path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("chart-thumbnail", "600519", "thumbnail", "2026-07-12", str(chart_path), "2026-07-12T08:30:00+08:00"),
    )
    connection.commit()
    connection.close()

    payload = TestClient(create_app(tmp_path)).get("/api/current-state").json()

    assert payload["chart_list"] == {"status": "degraded"}
    assert payload["charts"] == []


def test_current_state_degrades_chart_list_for_timestamp_as_of(tmp_path):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    chart_path = tmp_path / "charts" / "timestamp-as-of.png"
    chart_path.parent.mkdir()
    chart_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO chart_assets (asset_id, code, chart_type, as_of, path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("chart-timestamp", "600519", "kline", "2026-07-12T08:30:00+08:00", str(chart_path), "2026-07-12T08:30:00+08:00"),
    )
    connection.commit()
    connection.close()

    payload = TestClient(create_app(tmp_path)).get("/api/current-state").json()

    assert payload["chart_list"] == {"status": "degraded"}
    assert payload["charts"] == []


def test_current_state_degrades_chart_list_when_display_cap_is_exceeded(tmp_path):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    chart_path = tmp_path / "charts" / "overflow.png"
    chart_path.parent.mkdir()
    chart_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    connection = sqlite3.connect(db_path)
    connection.executemany(
        "INSERT INTO chart_assets (asset_id, code, chart_type, as_of, path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (f"chart-{index:03d}", "600519", "kline", "2026-07-12", str(chart_path), "2026-07-12T08:30:00+08:00")
            for index in range(101)
        ],
    )
    connection.commit()
    connection.close()

    payload = TestClient(create_app(tmp_path)).get("/api/current-state").json()

    assert payload["chart_list"] == {"status": "degraded"}
    assert payload["charts"] == []


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


def test_ledger_uses_explicit_operational_database_path(tmp_path):
    db_path = tmp_path / "database" / "operational.sqlite"
    response = TestClient(create_app(tmp_path, db_path=db_path)).post(
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

    assert response.status_code == 201
    assert db_path.is_file()
    assert not (tmp_path / "advisor.sqlite").exists()
    connection = sqlite3.connect(db_path)
    assert connection.execute("SELECT transaction_id FROM ledger_transactions").fetchone() == ("cash-1",)
    connection.close()


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


@pytest.mark.parametrize("path", ["/api/ledger/transactions", "/api/ledger/import"])
def test_ledger_routes_map_json_value_errors_before_database_write(tmp_path, path):
    db_path = tmp_path / "advisor.sqlite"
    body = '{"amount":' + "9" * 5000 + "}"

    response = TestClient(create_app(tmp_path, db_path=db_path)).post(
        path,
        content=body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid ledger request body"
    assert not db_path.exists()


@pytest.mark.parametrize("path", ["/api/ledger/transactions", "/api/ledger/import"])
def test_ledger_routes_map_json_recursion_errors_before_database_write(tmp_path, path):
    db_path = tmp_path / "advisor.sqlite"
    body = "[" * 2000 + "]" * 2000

    response = TestClient(create_app(tmp_path, db_path=db_path)).post(
        path,
        content=body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid ledger request body"
    assert not db_path.exists()


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


def test_ledger_import_rejects_non_finite_replayed_cash_atomically(tmp_path):
    db_path = tmp_path / "advisor.sqlite"
    response = TestClient(create_app(tmp_path, db_path=db_path)).post(
        "/api/ledger/import",
        json=[
            {"transaction_id": "deposit-1", "trade_date": "2026-07-09", "transaction_type": "cash_deposit", "quantity": 0, "price": 0, "amount": 1e308, "fees": 0},
            {"transaction_id": "deposit-2", "trade_date": "2026-07-10", "transaction_type": "cash_deposit", "quantity": 0, "price": 0, "amount": 1e308, "fees": 0},
        ],
    )

    assert response.status_code == 422
    connection = sqlite3.connect(db_path)
    for table in ("ledger_transactions", "positions", "portfolio_snapshots"):
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
    connection.close()


@pytest.mark.parametrize("nested", [["unexpected"], {"unexpected": True}])
def test_ledger_import_rejects_nested_values_before_database_write(tmp_path, nested):
    db_path = tmp_path / "advisor.sqlite"
    response = TestClient(create_app(tmp_path, db_path=db_path)).post(
        "/api/ledger/import",
        json=[
            {
                "transaction_id": "deposit-1",
                "trade_date": "2026-07-09",
                "transaction_type": "cash_deposit",
                "quantity": 0,
                "price": 0,
                "amount": 1,
                "fees": 0,
                "unknown": nested,
            }
        ],
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid ledger request shape"
    assert not db_path.exists()


def test_ledger_import_rejects_huge_unknown_field_before_database_write(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(web_api, "_MAX_LEDGER_FIELD_LENGTH", 16)
    db_path = tmp_path / "advisor.sqlite"
    response = TestClient(create_app(tmp_path, db_path=db_path)).post(
        "/api/ledger/import",
        json=[
            {
                "transaction_id": "deposit-1",
                "trade_date": "2026-07-09",
                "transaction_type": "cash_deposit",
                "quantity": 0,
                "price": 0,
                "amount": 1,
                "fees": 0,
                "unknown": "x" * 17,
            }
        ],
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "ledger field is too long"
    assert not db_path.exists()


def test_ledger_transaction_rejects_oversized_raw_body_before_json_decode(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(web_api, "_MAX_LEDGER_REQUEST_BYTES", 32, raising=False)
    db_path = tmp_path / "advisor.sqlite"
    body = json.dumps(
        {
            "transaction_id": "cash-1",
            "trade_date": "2026-07-11",
            "transaction_type": "cash_deposit",
            "quantity": 0,
            "price": 0,
            "amount": 100,
            "fees": 0,
        }
    )

    response = TestClient(create_app(tmp_path, db_path=db_path)).post(
        "/api/ledger/transactions",
        content=body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "ledger request body is too large"
    assert not db_path.exists()


def test_ledger_import_rejects_oversized_raw_body_before_json_decode(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(web_api, "_MAX_LEDGER_REQUEST_BYTES", 32, raising=False)
    db_path = tmp_path / "advisor.sqlite"
    body = json.dumps(
        [
            {
                "transaction_id": "cash-1",
                "trade_date": "2026-07-11",
                "transaction_type": "cash_deposit",
                "quantity": 0,
                "price": 0,
                "amount": 100,
                "fees": 0,
            }
        ]
    )

    response = TestClient(create_app(tmp_path, db_path=db_path)).post(
        "/api/ledger/import",
        content=body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "ledger request body is too large"
    assert not db_path.exists()


@pytest.mark.parametrize("path", ["/api/ledger/transactions", "/api/ledger/import"])
def test_ledger_routes_reject_streamed_body_over_limit_without_content_length(
    tmp_path, monkeypatch, path
):
    monkeypatch.setattr(web_api, "_MAX_LEDGER_REQUEST_BYTES", 8, raising=False)
    db_path = tmp_path / "advisor.sqlite"

    status, payload, receive_count = run_streamed_request(
        create_app(tmp_path, db_path=db_path),
        path,
        [b'{"x":"123', b'456789"}'],
    )

    assert status == 413
    assert payload["detail"] == "ledger request body is too large"
    assert receive_count == 1
    assert not db_path.exists()


def test_ledger_import_rejects_rows_over_key_limit_before_database_write(tmp_path):
    db_path = tmp_path / "advisor.sqlite"
    row = {
        "transaction_id": "deposit-1",
        "trade_date": "2026-07-09",
        "transaction_type": "cash_deposit",
        "quantity": 0,
        "price": 0,
        "amount": 1,
        "fees": 0,
    }
    row.update({f"unknown-{index}": index for index in range(3)})

    response = TestClient(create_app(tmp_path, db_path=db_path)).post(
        "/api/ledger/import", json=[row]
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid ledger request shape"
    assert not db_path.exists()


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


def test_api_import_materializes_positions_snapshot_and_profile_exposure(tmp_path):
    from advisor.ledger.importer import ledger_exposure_by_code

    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    client = TestClient(create_app(tmp_path, db_path=db_path))
    transactions = [
        {"transaction_id": "deposit", "account_id": "api", "trade_date": "2026-07-10", "transaction_type": "cash_deposit", "quantity": 0, "price": 0, "amount": 20000, "fees": 0},
        {"transaction_id": "buy", "account_id": "api", "trade_date": "2026-07-10", "transaction_type": "buy", "code": "600519", "quantity": 100, "price": 100, "amount": -10000, "fees": 0},
    ]

    response = client.post("/api/ledger/import", json=transactions)

    assert response.status_code == 201
    connection = sqlite3.connect(db_path)
    assert connection.execute(
        "SELECT account_id, code, quantity, cost_basis FROM positions"
    ).fetchall() == [("api", "600519", 100, 10000.0)]
    snapshot = connection.execute(
        "SELECT cash, exposure_json FROM portfolio_snapshots WHERE account_id = 'api'"
    ).fetchone()
    assert snapshot[0] == 10000.0
    assert json.loads(snapshot[1])["positions"]["600519"]["quantity"] == 100
    connection.row_factory = sqlite3.Row
    assert ledger_exposure_by_code(connection, ("600519",))["600519"]["quantity"] == 100
    connection.close()


def test_api_import_materializes_multiple_accounts_atomically(tmp_path):
    db_path = tmp_path / "advisor.sqlite"
    client = TestClient(create_app(tmp_path, db_path=db_path))

    response = client.post("/api/ledger/import", json=[
        {"transaction_id": "deposit-a", "account_id": "a", "trade_date": "2026-07-10", "transaction_type": "cash_deposit", "quantity": 0, "price": 0, "amount": 2000, "fees": 0},
        {"transaction_id": "buy-a", "account_id": "a", "trade_date": "2026-07-10", "transaction_type": "buy", "code": "600519", "quantity": 100, "price": 10, "amount": -1000, "fees": 0},
        {"transaction_id": "deposit-b", "account_id": "b", "trade_date": "2026-07-10", "transaction_type": "cash_deposit", "quantity": 0, "price": 0, "amount": 3000, "fees": 0},
        {"transaction_id": "buy-b", "account_id": "b", "trade_date": "2026-07-10", "transaction_type": "buy", "code": "000001", "quantity": 100, "price": 20, "amount": -2000, "fees": 0},
    ])

    assert response.status_code == 201
    connection = sqlite3.connect(db_path)
    assert connection.execute(
        "SELECT account_id, code, quantity FROM positions ORDER BY account_id"
    ).fetchall() == [("a", "600519", 100), ("b", "000001", 100)]
    assert connection.execute(
        "SELECT account_id, cash FROM portfolio_snapshots ORDER BY account_id"
    ).fetchall() == [("a", 1000.0), ("b", 1000.0)]
    connection.close()


def test_api_and_cli_imports_materialize_equivalent_account_state(tmp_path):
    from advisor.ledger.importer import import_ledger_csv

    cli_db = tmp_path / "cli.sqlite"
    api_db = tmp_path / "api.sqlite"
    csv_path = tmp_path / "ledger.csv"
    csv_path.write_text(
        "transaction_id,trade_date,transaction_type,code,quantity,price,amount,fees\n"
        "deposit,2026-07-10,cash_deposit,,0,0,20000,0\n"
        "buy,2026-07-10,buy,600519,100,100,-10000,0\n",
        encoding="utf-8",
    )
    as_of = datetime(2026, 7, 12, 8, 30, tzinfo=web_api._SHANGHAI)
    import_ledger_csv(csv_path, cli_db, account_id="same", source="import", as_of=as_of)
    monkeypatch_time = pytest.MonkeyPatch()
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return as_of
    monkeypatch_time.setattr(web_api, "datetime", FixedDatetime)
    try:
        response = TestClient(create_app(tmp_path, db_path=api_db)).post("/api/ledger/import", json=[
            {"transaction_id": "deposit", "account_id": "same", "trade_date": "2026-07-10", "transaction_type": "cash_deposit", "quantity": 0, "price": 0, "amount": 20000, "fees": 0},
            {"transaction_id": "buy", "account_id": "same", "trade_date": "2026-07-10", "transaction_type": "buy", "code": "600519", "quantity": 100, "price": 100, "amount": -10000, "fees": 0},
        ])
    finally:
        monkeypatch_time.undo()

    assert response.status_code == 201
    def state(db):
        connection = sqlite3.connect(db)
        try:
            return (
                connection.execute("SELECT account_id, code, quantity, cost_basis FROM positions").fetchall(),
                connection.execute("SELECT account_id, as_of, cash, market_value, realized_pnl, unrealized_pnl, exposure_json FROM portfolio_snapshots").fetchall(),
            )
        finally:
            connection.close()
    assert state(api_db) == state(cli_db)


def test_api_and_csv_share_beijing_stock_code_validation(tmp_path, monkeypatch):
    from advisor.ledger.importer import import_ledger_csv

    as_of = datetime(2026, 7, 12, 8, 30, tzinfo=web_api._SHANGHAI)
    cli_db = tmp_path / "cli.sqlite"
    api_db = tmp_path / "api.sqlite"
    csv_path = tmp_path / "beijing.csv"
    csv_path.write_text(
        "transaction_id,trade_date,transaction_type,code,quantity,price,amount,fees\n"
        "deposit,2026-07-10,cash_deposit,,0,0,20000,0\n"
        "buy,2026-07-10,buy,430047,100,10,-1000,0\n",
        encoding="utf-8",
    )
    import_ledger_csv(csv_path, cli_db, account_id="same", source="import", as_of=as_of)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return as_of

    monkeypatch.setattr(web_api, "datetime", FixedDatetime)
    response = TestClient(create_app(tmp_path, db_path=api_db)).post(
        "/api/ledger/import",
        json=[
            {"transaction_id": "deposit", "account_id": "same", "trade_date": "2026-07-10", "transaction_type": "cash_deposit", "quantity": 0, "price": 0, "amount": 20000, "fees": 0},
            {"transaction_id": "buy", "account_id": "same", "trade_date": "2026-07-10", "transaction_type": "buy", "code": "430047", "quantity": 100, "price": 10, "amount": -1000, "fees": 0},
        ],
    )

    assert response.status_code == 201
    for db_path in (cli_db, api_db):
        connection = sqlite3.connect(db_path)
        assert connection.execute(
            "SELECT account_id, code, quantity FROM positions"
        ).fetchall() == [("same", "430047", 100)]
        connection.close()


def test_api_ledger_rejects_oversized_string_field_before_database_write(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(web_api, "_MAX_LEDGER_FIELD_LENGTH", 16, raising=False)
    db_path = tmp_path / "advisor.sqlite"
    response = TestClient(create_app(tmp_path, db_path=db_path)).post(
        "/api/ledger/transactions",
        json={
            "transaction_id": "x" * 17,
            "trade_date": "2026-07-10",
            "transaction_type": "cash_deposit",
            "quantity": 0,
            "price": 0,
            "amount": 1,
            "fees": 0,
        },
    )

    assert response.status_code == 422
    assert not db_path.exists()


def test_current_state_reads_verified_reports_and_local_dashboard_fixtures(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    today = date.today().isoformat()
    advice = [AdviceItem("advice-1", "600519", "watch", 0.7, "fixture rationale", ["evidence-1"])]
    premarket_paths = write_premarket_report(today, advice, reports_root, quality_results=[QualityResult("market", "blocking", True, "current")])
    review_paths = write_review_report(
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
    connection.executemany(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) "
        "VALUES (?, ?, ?, 'passed', ?)",
        [
            ("archive-premarket", "premarket", today, f"{today}T00:00:00+08:00"),
            ("archive-review", "review", today, f"{today}T00:00:00+08:00"),
        ],
    )
    insert_report_archive(
        connection,
        database_run_id="archive-premarket",
        report_type="premarket",
        report_date=today,
        markdown_path=premarket_paths.markdown_path,
        json_path=premarket_paths.json_path,
    )
    insert_report_archive(
        connection,
        database_run_id="archive-review",
        report_type="review",
        report_date=today,
        markdown_path=review_paths.markdown_path,
        json_path=review_paths.json_path,
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
    connection.execute(
        "INSERT INTO ledger_accounts (account_id, name, created_at) VALUES (?, ?, ?)",
        ("default", "default", now),
    )
    connection.execute(
        "INSERT INTO ledger_transactions (transaction_id, account_id, trade_date, transaction_type, quantity, price, amount, fees, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("cash-1", "default", today, "cash_deposit", 0, 0, 1000, 0, "manual", now),
    )
    connection.commit()
    connection.close()
    (tmp_path / "health.json").write_text('{"components":{"collector":"running","token":"secret"}}', encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    payload = client.get("/api/current-state").json()

    assert payload["advice"] == []
    assert payload["advice_status"] == "blocked"
    assert payload["review"] == {"status": "blocked", "items": []}
    assert payload["ledger"]["status"] == "ok"
    assert payload["ledger"]["cash"] == 1000.0
    assert payload["last_successful_data_update"] == web_api._parse_shanghai_datetime(now).isoformat()
    assert payload["flows"] == {
        "information": {"status": "ok", "count": 1},
        "capital": {"status": "ok", "count": 0},
        "analyst": {"status": "ok", "count": 1},
    }
    assert payload["blocking_quality_checks"] == [
        {
            "check_name": "market_stale",
            "severity": "blocking",
            "status": "failed",
            "created_at": web_api._parse_shanghai_datetime(now).isoformat(),
        }
    ]
    assert payload["reports"][0]["report_date"] == today
    assert payload["profiles"] == [{"code": "600519", "name": "Moutai", "href": "/api/profiles/600519"}]
    assert payload["profile_list"] == {"status": "ok"}
    assert payload["charts"][0]["asset_id"] == "chart-1"
    assert payload["chart_list"] == {"status": "ok"}
    assert payload["health"]["collector"] == "running"
    assert "token" not in payload["health"]


def test_current_state_ignores_yesterday_quality_failure_for_today_passed_report(tmp_path, monkeypatch):
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports_root)
    today = date.today().isoformat()
    advice = [AdviceItem("advice-1", "600519", "watch", 0.7, "fixture", ["evidence-1"])]
    report_paths = write_premarket_report(today, advice, reports_root, quality_results=[QualityResult("market", "blocking", True, "current")])
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
    insert_report_archive(
        connection,
        database_run_id="today-run",
        report_type="premarket",
        report_date=today,
        markdown_path=report_paths.markdown_path,
        json_path=report_paths.json_path,
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
    monkeypatch.setattr(web_api, "_read_report_links", lambda _today, _secret, _eligible: ([], {"status": "ok", "truncated": False}))
    monkeypatch.setattr(web_api, "_read_today_report", lambda _today, report_type, _eligible: malformed_report if report_type == "premarket" else None)

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


@pytest.mark.parametrize("field", ["as_of", "started_at", "created_at"])
def test_current_quality_resolver_rejects_oversized_parseable_timestamp(tmp_path, monkeypatch, field):
    monkeypatch.setattr(web_api, "_shanghai_today", lambda: date(2026, 7, 12))
    oversized = "2026-07-12T08:30:00." + "1" * 100_000 + "+08:00"
    run_values = {
        "as_of": "2026-07-12T08:30:00+08:00",
        "started_at": "2026-07-12T08:30:00+08:00",
    }
    check_created_at = "2026-07-12T08:31:00+08:00"
    if field == "created_at":
        check_created_at = oversized
    else:
        run_values[field] = oversized
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) VALUES (?, ?, ?, ?, ?)",
        ("oversized-run", "premarket", run_values["as_of"], "passed", run_values["started_at"]),
    )
    connection.execute(
        "INSERT INTO data_quality_checks (check_id, run_id, check_name, severity, status, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("oversized-check", "oversized-run", "market", "blocking", "failed", "{}", check_created_at),
    )
    connection.commit()
    connection.close()

    response = TestClient(create_app(tmp_path)).get("/api/current-state")

    assert response.json()["advice_status"] == "blocked"
    assert response.json()["blocking_quality_checks"] == []
    assert "1" * 1000 not in response.text


def test_current_quality_normalizes_exposed_check_timestamp_to_shanghai(tmp_path, monkeypatch):
    monkeypatch.setattr(web_api, "_shanghai_today", lambda: date(2026, 7, 12))
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) VALUES (?, ?, ?, ?, ?)",
        ("failed-check-run", "premarket", "2026-07-12", "passed", "2026-07-12T08:30:00+08:00"),
    )
    connection.execute(
        "INSERT INTO data_quality_checks (check_id, run_id, check_name, severity, status, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("failed-check", "failed-check-run", "market", "blocking", "failed", "{}", "2026-07-12T00:31:00Z"),
    )
    connection.commit()
    connection.close()

    checks = TestClient(create_app(tmp_path)).get("/api/current-state").json()["blocking_quality_checks"]

    assert checks == [
        {
            "check_name": "market",
            "severity": "blocking",
            "status": "failed",
            "created_at": "2026-07-12T08:31:00+08:00",
        }
    ]


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
    report_paths = write_premarket_report(
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
    connection.execute(
        "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("historical-run", "premarket", "2026-07-11", "passed", "2026-07-11T08:30:00+08:00"),
    )
    insert_report_archive(
        connection,
        database_run_id="historical-run",
        report_type="premarket",
        report_date="2026-07-11",
        markdown_path=report_paths.markdown_path,
        json_path=report_paths.json_path,
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
