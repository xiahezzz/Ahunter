from datetime import datetime
from zoneinfo import ZoneInfo

from advisor.db.repository import connect
from advisor.market_daily.reset import MarketDailyReset


NOW = datetime(2026, 8, 7, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_reset_only_deletes_explicit_market_daily_tables(tmp_path):
    database = tmp_path / "advisor.sqlite"
    reset = MarketDailyReset(database)
    connection = connect(database)
    try:
        connection.execute(
            "INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at) VALUES (?, 'manual', ?, 'passed', ?)",
            ("keep-run", NOW.isoformat(), NOW.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO market_daily (
              code, trade_date, open, high, low, close, volume, amount, source,
              source_at, fetched_at, as_of_date, content_hash, quality_status
            ) VALUES ('600519', '2026-08-07', 1, 1, 1, 1, 100, 100, 'eastmoney', ?, ?, '2026-08-07', 'old-bar', 'passed')
            """,
            (NOW.isoformat(), NOW.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO trading_session_observations (
              observation_id, observed_at, primary_source, fallback_source, latest_session, session_set_hash, content_hash
            ) VALUES (?, ?, 'eastmoney', 'tdx', '2026-08-07', ?, ?)
            """,
            ("c" * 64, NOW.isoformat(), "a" * 64, "b" * 64),
        )
        connection.execute(
            """
            INSERT INTO market_sources (source_key, source, endpoint, params_hash, fetched_at, status, details_json)
            VALUES ('keep-news', 'public-news', 'news', 'hash', ?, 'passed', '{}')
            """,
            (NOW.isoformat(),),
        )
        connection.execute(
            """
            INSERT INTO market_sources (source_key, source, endpoint, params_hash, fetched_at, status, details_json)
            VALUES ('old-sina', 'sina_http', 'sina', 'hash', ?, 'passed', '{}')
            """,
            (NOW.isoformat(),),
        )
        connection.execute("CREATE TABLE trading_calendar_proofs (proof_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO trading_calendar_proofs (proof_id) VALUES ('old-proof')")
        connection.commit()
    finally:
        connection.close()

    preview = reset.preview()
    result = reset.clear()
    connection = connect(database)
    try:
        assert preview["market_daily"] == 1
        assert preview["trading_session_observations"] == 1
        assert preview["legacy_market_sources"] == 1
        assert preview["legacy_trading_calendar_proofs"] == 1
        assert result.deleted["market_daily"] == 1
        assert result.deleted["trading_session_observations"] == 1
        assert result.deleted["legacy_market_sources"] == 1
        assert result.deleted["legacy_trading_calendar_proofs"] == 1
        assert connection.execute("SELECT COUNT(*) FROM market_daily").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM trading_session_observations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM market_sources WHERE source = 'public-news'").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM market_sources WHERE source = 'sina_http'").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM trading_calendar_proofs").fetchone()[0] == 0
        columns = {row[1]: row[2].upper() for row in connection.execute("PRAGMA table_info(market_daily)")}
        assert columns["volume"] == "INTEGER"
        assert connection.execute("SELECT COUNT(*) FROM advisor_runs WHERE run_id = 'keep-run'").fetchone()[0] == 1
    finally:
        connection.close()
