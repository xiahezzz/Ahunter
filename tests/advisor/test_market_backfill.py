import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from advisor.data_sources.backfill import main, update_market_database
from advisor.data_sources.contracts import DailyBar, MarketDataProvider, MarketSourceError
from advisor.db.repository import connect
from advisor.web.api import create_app


def make_bar(code: str, *, content_hash: str = "bar-1", trade_date: date = date(2026, 7, 10)) -> DailyBar:
    return DailyBar(
        code=code,
        trade_date=trade_date,
        open=10.0,
        high=10.8,
        low=9.8,
        close=10.5,
        volume=1000000,
        amount=0,
        source="fake_free_source",
        fetched_at="2026-07-11T08:00:00+08:00",
        as_of_date=trade_date,
        content_hash=content_hash,
    )


class FakeProvider(MarketDataProvider):
    source = "fake_free_source"
    endpoint = "http://free.example.test/kline"

    def __init__(self, *, failed_codes=(), bar_factory=make_bar):
        self.failed_codes = set(failed_codes)
        self.bar_factory = bar_factory
        self.calls = []

    def fetch_daily_bars(self, code: str, start: date, end: date) -> list[DailyBar]:
        self.calls.append((code, start, end))
        if code in self.failed_codes:
            raise MarketSourceError("recorded source failure")
        return [self.bar_factory(code)]


def test_backfill_issues_36_month_request_and_writes_api_database(tmp_path: Path):
    state_dir = tmp_path / "data" / "advisor"
    db_path = state_dir / "advisor.sqlite"
    provider = FakeProvider()

    result = update_market_database(
        db_path,
        provider,
        ["600519"],
        date(2023, 7, 12),
        date(2026, 7, 12),
        sleep=lambda _: None,
    )

    assert provider.calls == [("600519", date(2023, 7, 12), date(2026, 7, 12))]
    assert result.requested_codes == ("600519",)
    assert result.completed_codes == ("600519",)
    assert result.failed_codes == ()
    assert result.inserted_rows == 1
    payload = TestClient(create_app(state_dir)).get("/api/current-state").json()
    assert payload["last_successful_data_update"] == "2026-07-11T08:00:00+08:00"


def test_backfill_is_idempotent_but_records_every_attempt(tmp_path: Path):
    db_path = tmp_path / "advisor.sqlite"
    provider = FakeProvider()

    first = update_market_database(db_path, provider, ["600519"], date(2023, 7, 12), date(2026, 7, 12), sleep=lambda _: None)
    second = update_market_database(db_path, provider, ["600519"], date(2023, 7, 12), date(2026, 7, 12), sleep=lambda _: None)

    connection = connect(db_path)
    try:
        assert first.inserted_rows == 1
        assert second.inserted_rows == 0
        assert connection.execute("SELECT COUNT(*) FROM market_daily").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM market_sources").fetchone()[0] == 2
    finally:
        connection.close()


def test_failed_second_code_preserves_committed_first_code(tmp_path: Path):
    db_path = tmp_path / "advisor.sqlite"
    provider = FakeProvider(failed_codes={"000001"})

    result = update_market_database(
        db_path, provider, ["600519", "000001"], date(2026, 7, 1), date(2026, 7, 12), sleep=lambda _: None
    )

    connection = connect(db_path)
    try:
        assert result.completed_codes == ("600519",)
        assert result.failed_codes == ("000001",)
        assert connection.execute("SELECT code FROM market_daily").fetchall()[0][0] == "600519"
        attempts = connection.execute("SELECT status, details_json FROM market_sources ORDER BY fetched_at, rowid").fetchall()
        assert [row["status"] for row in attempts] == ["passed", "failed"]
        assert json.loads(attempts[1]["details_json"])["code"] == "000001"
    finally:
        connection.close()


def test_failed_refresh_never_deletes_prior_good_row(tmp_path: Path):
    db_path = tmp_path / "advisor.sqlite"
    update_market_database(db_path, FakeProvider(), ["600519"], date(2026, 7, 1), date(2026, 7, 12), sleep=lambda _: None)

    result = update_market_database(
        db_path, FakeProvider(failed_codes={"600519"}), ["600519"], date(2026, 7, 1), date(2026, 7, 12), sleep=lambda _: None
    )

    connection = connect(db_path)
    try:
        assert result.failed_codes == ("600519",)
        assert connection.execute("SELECT content_hash FROM market_daily").fetchone()[0] == "bar-1"
    finally:
        connection.close()


@pytest.mark.parametrize(
    "bar_factory",
    [
        lambda code: make_bar(code, trade_date=date(2026, 6, 30)),
        lambda code: DailyBar(**{**make_bar(code).__dict__, "as_of_date": date(2026, 7, 13)}),
    ],
)
def test_backfill_rejects_provider_rows_outside_request(tmp_path: Path, bar_factory):
    result = update_market_database(
        tmp_path / "advisor.sqlite",
        FakeProvider(bar_factory=bar_factory),
        ["600519"],
        date(2026, 7, 1),
        date(2026, 7, 12),
        sleep=lambda _: None,
    )

    assert result.failed_codes == ("600519",)
    assert connect(tmp_path / "advisor.sqlite").execute("SELECT COUNT(*) FROM market_daily").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("codes", "start", "end"),
    [([], date(2026, 7, 1), date(2026, 7, 12)), (["bad"], date(2026, 7, 1), date(2026, 7, 12)), (["600519"] * 201, date(2026, 7, 1), date(2026, 7, 12)), (["600519"], date(2026, 7, 13), date(2026, 7, 12))],
)
def test_invalid_request_does_not_open_sqlite(tmp_path: Path, codes, start, end):
    db_path = tmp_path / "advisor.sqlite"

    with pytest.raises(ValueError):
        update_market_database(db_path, FakeProvider(), codes, start, end, sleep=lambda _: None)

    assert not db_path.exists()


def test_partial_cli_exits_nonzero_and_records_failure(tmp_path: Path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "advisor.yaml"
    config_path.write_text(
        """
market: {primary: A股}
schedule: {premarket_time: "08:30", review_time: "22:30"}
storage: {database: data/advisor/advisor.sqlite}
data_sources: {allow_tushare: false, free_sources: [sina]}
""",
        encoding="utf-8",
    )
    (config_dir / "data-sources.yaml").write_text(
        "sources:\n  sina:\n    enabled: true\n    rate_limit_per_second: 100\n",
        encoding="utf-8",
    )
    provider = FakeProvider(failed_codes={"000001"})

    class Registry:
        historical_provider = provider
        rate_limit_per_second = 100

    monkeypatch.setattr("advisor.data_sources.backfill.ConfiguredProviderRegistry.from_yaml", lambda _: Registry())

    with pytest.raises(SystemExit) as exit_info:
        main(["--config", str(config_path), "--codes", "600519,000001", "--start", "2026-07-01", "--end", "2026-07-12"])

    assert exit_info.value.code == 1
    db_path = tmp_path / "data" / "advisor" / "advisor.sqlite"
    statuses = sqlite3.connect(db_path).execute("SELECT status FROM market_sources ORDER BY rowid").fetchall()
    assert statuses == [("passed",), ("failed",)]
