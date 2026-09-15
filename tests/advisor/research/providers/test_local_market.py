from __future__ import annotations

from datetime import date, datetime
import gzip
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from advisor.market_daily.contracts import AdjustmentFactor, CanonicalDailyBar, MarketSecurity, ObservedTradingSession
from advisor.market_daily.control import MarketDailyControlPlane, RunSecurity
from advisor.market_daily.repository import MarketDailyRepository
from advisor.research.contracts import ResearchBoundary, ResearchSubject, VersionRef
from advisor.research.data_products.engine import ProductRequest
from advisor.research.providers.local import LocalMarketProvider


SHANGHAI = ZoneInfo("Asia/Shanghai")
OBSERVED = datetime(2026, 8, 5, 21, 0, tzinfo=SHANGHAI)


def _request() -> ProductRequest:
    return ProductRequest(
        VersionRef.parse("market_daily_bars@1"),
        ResearchSubject(code="600519"),
        ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=ZoneInfo("UTC"))),
    )


def _market_request(as_of: datetime | None = None) -> ProductRequest:
    return ProductRequest(
        VersionRef.parse("whole_market_daily_history@1"),
        ResearchSubject(scope="market"),
        ResearchBoundary(as_of=as_of or datetime(2026, 8, 6, 8, 30, tzinfo=ZoneInfo("UTC"))),
    )


def _ready_database(database: Path, *, amount=100000) -> None:
    repository = MarketDailyRepository(database)
    control = MarketDailyControlPlane(database)
    security = MarketSecurity(
        "600519", "贵州茅台", "SH", date(2001, 8, 27), None, "active", False,
        "fixture_exchange", OBSERVED, OBSERVED,
    )
    repository.upsert_security(security)
    repository.upsert_session(
        ObservedTradingSession(date(2026, 8, 5), "fixture_primary", "fixture_fallback", OBSERVED, OBSERVED, OBSERVED)
    )
    request = control.submit_cold_start(date(2026, 8, 5), date(2026, 8, 5), OBSERVED)
    claimed = control.claim_next_request("fixture-service", OBSERVED)
    assert claimed is not None
    run = control.create_run(
        request.request_id,
        "a" * 64,
        (RunSecurity("600519", "贵州茅台", "SH", date(2001, 8, 27), None, "active"),),
        OBSERVED,
    )
    control.start_run(run.run_id, OBSERVED)
    control.mark_item(run.run_id, "600519", "completed", OBSERVED)
    control.finalize_run(run.run_id, OBSERVED)
    bar = CanonicalDailyBar(
        "600519", date(2026, 8, 5), 100, 102, 99, 101, 1000, amount,
        "fixture", OBSERVED, OBSERVED,
    )
    factor = AdjustmentFactor(
        "600519", date(2026, 8, 5), 1.0, "fixture_factor", OBSERVED, OBSERVED, "forward-adjustment@1"
    )
    repository.commit_security_observations((bar,), (factor,), ())


def test_local_market_reads_only_complete_visible_local_market_daily_facts(tmp_path: Path):
    database = tmp_path / "advisor.sqlite"
    _ready_database(database)

    observation = LocalMarketProvider(database).fetch(_request())

    assert observation.provider == "local-market"
    assert observation.payload["rows"][0]["close"] == 101.0
    assert observation.payload["raw_rows"][0]["adjustment"] == "unadjusted"
    assert observation.payload["research_price_series"]["bars"][0]["close"] == 101.0
    assert len(observation.payload["snapshot_proof"]["factor_set_hash"]) == 64
    assert observation.source_locator == "sqlite://advisor.sqlite/market_daily"


def test_local_market_marks_missing_or_unproven_market_data_unavailable(tmp_path: Path):
    database = tmp_path / "advisor.sqlite"
    MarketDailyRepository(database)

    observation = LocalMarketProvider(database).fetch(_request())

    assert observation.quality_status == "warning"
    assert observation.coverage == 0.0


def test_whole_market_history_requires_a_complete_market_daily_run(tmp_path: Path):
    database = tmp_path / "advisor.sqlite"
    repository = MarketDailyRepository(database)
    control = MarketDailyControlPlane(database)
    ready = MarketSecurity(
        "600519", "贵州茅台", "SH", date(2001, 8, 27), None, "active", False,
        "fixture_exchange", OBSERVED, OBSERVED,
    )
    missing = MarketSecurity(
        "000001", "平安银行", "SZ", date(1991, 4, 3), None, "active", False,
        "fixture_exchange", OBSERVED, OBSERVED,
    )
    repository.upsert_security(ready)
    repository.upsert_security(missing)
    repository.upsert_session(
        ObservedTradingSession(date(2026, 8, 5), "fixture_primary", "fixture_fallback", OBSERVED, OBSERVED, OBSERVED)
    )
    request = control.submit_cold_start(date(2026, 8, 5), date(2026, 8, 5), OBSERVED)
    assert control.claim_next_request("fixture-service", OBSERVED) is not None
    run = control.create_run(
        request.request_id,
        "b" * 64,
        (
            RunSecurity("600519", "贵州茅台", "SH", date(2001, 8, 27), None, "active"),
            RunSecurity("000001", "平安银行", "SZ", date(1991, 4, 3), None, "active"),
        ),
        OBSERVED,
    )
    control.start_run(run.run_id, OBSERVED)
    control.mark_item(run.run_id, ready.code, "completed", OBSERVED)
    control.mark_item(run.run_id, missing.code, "source_missing", OBSERVED, error="fixture unavailable")
    control.finalize_run(run.run_id, OBSERVED)

    observation = LocalMarketProvider(database).fetch(_market_request())

    assert observation.quality_status == "warning"
    assert observation.coverage == 0.0
    assert "全市场运行尚未完整" in str(observation.quality_message)


def test_whole_market_history_revalidates_facts_after_a_complete_run(tmp_path: Path):
    database = tmp_path / "advisor.sqlite"
    _ready_database(database)
    provider = LocalMarketProvider(database)

    assert provider.fetch(_market_request()).quality_status == "passed"

    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "DELETE FROM market_daily WHERE code = '600519' AND trade_date = '2026-08-05'"
        )
        connection.commit()
    finally:
        connection.close()

    observation = provider.fetch(_market_request())

    assert observation.quality_status == "warning"
    assert "事实覆盖不完整" in str(observation.quality_message)


def test_whole_market_history_compares_visible_instants_across_timezones(tmp_path: Path):
    database = tmp_path / "advisor.sqlite"
    _ready_database(database)
    boundary = datetime(2026, 8, 5, 14, 0, tzinfo=ZoneInfo("UTC"))  # 22:00 Shanghai, after OBSERVED.

    observation = LocalMarketProvider(database).fetch(_market_request(boundary))

    assert observation.quality_status == "passed"
    assert [row["code"] for row in observation.payload["rows"]] == ["600519"]


def test_whole_market_history_excludes_a_security_listed_after_the_latest_visible_session(
    tmp_path: Path,
):
    database = tmp_path / "advisor.sqlite"
    _ready_database(database)
    repository = MarketDailyRepository(database)
    repository.upsert_security(
        MarketSecurity(
            "000001",
            "边界后上市样例",
            "SZ",
            date(2026, 8, 6),
            None,
            "active",
            False,
            "fixture_exchange",
            OBSERVED,
            OBSERVED,
        )
    )

    observation = LocalMarketProvider(database).fetch(_market_request())

    assert observation.quality_status == "passed"
    assert observation.payload["securities"] == [
        {
            "code": "600519",
            "name": "贵州茅台",
            "list_date": "2001-08-27",
            "delist_date": None,
        }
    ]


def test_whole_market_history_streams_rows_to_a_bounded_query_backing(tmp_path: Path):
    database = tmp_path / "advisor.sqlite"
    _ready_database(database)

    staging = tmp_path / "staging"
    staging.mkdir()
    observation = LocalMarketProvider(
        database,
        stream_threshold_rows=0,
        staging_dir=staging,
    ).fetch(_market_request())

    assert observation.quality_status == "passed"
    assert observation.payload["rows"] == []
    assert observation.payload["row_count"] == 1
    assert observation.payload['field_coverage']['amount'] == {'numeric_count': 1, 'missing_count': 0}
    assert observation.query_backing is not None
    try:
        with gzip.open(observation.query_backing.path, "rt", encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle]
        assert records[0]["kind"] == "bar"
        assert records[0]["row"]["code"] == "600519"
    finally:
        observation.query_backing.path.unlink(missing_ok=True)


def test_whole_market_history_rejects_a_non_session_fact(tmp_path: Path):
    database = tmp_path / "advisor.sqlite"
    _ready_database(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE market_daily_runs SET start_date = '2026-08-01' WHERE run_type = 'cold_start'"
        )
        connection.commit()
    finally:
        connection.close()
    repository = MarketDailyRepository(database)
    repository.commit_security_observations(
        (
            CanonicalDailyBar(
                "600519", date(2026, 8, 2), 100, 102, 99, 101, 1000, 100000,
                "fixture", OBSERVED, OBSERVED,
            ),
        ),
        (),
        (),
    )

    observation = LocalMarketProvider(database).fetch(_market_request())

    assert observation.quality_status == "warning"
    assert "事实集合不精确" in str(observation.quality_message)


def test_history_exposes_missing_amount_before_model_query_planning(tmp_path):
    from advisor.research.data_products.engine import _whole_market_history_summary
    database = tmp_path / "advisor.sqlite"
    _ready_database(database, amount=None)
    observation = LocalMarketProvider(database).fetch(_market_request())
    summary = _whole_market_history_summary(observation.payload)
    assert summary["field_coverage"]["amount"] == {"numeric_count": 0, "missing_count": 1}
    assert observation.payload["rows"][0].get("amount") is None
