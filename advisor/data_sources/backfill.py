import argparse
from datetime import date
from pathlib import Path

from advisor.data_sources.contracts import MarketDataProvider
from advisor.db.repository import connect


def backfill_daily_bars(
    db_path: Path,
    provider: MarketDataProvider,
    codes: list[str],
    start: date,
    end: date,
) -> int:
    inserted = 0
    connection = connect(db_path)
    try:
        for code in codes:
            for bar in provider.fetch_daily_bars(code, start, end):
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO market_daily (
                      code, trade_date, open, high, low, close, volume, amount,
                      adj_factor, limit_up, limit_down, source, fetched_at,
                      as_of_date, schema_version, content_hash, quality_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'passed')
                    """,
                    (
                        bar.code,
                        bar.trade_date.isoformat(),
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.volume,
                        bar.amount,
                        bar.adj_factor,
                        bar.limit_up,
                        bar.limit_down,
                        bar.source,
                        bar.fetched_at,
                        bar.as_of_date.isoformat(),
                        bar.content_hash,
                    ),
                )
                inserted += cursor.rowcount
        connection.commit()
        return inserted
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--codes", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.parse_args()
    raise SystemExit(
        "advisor-backfill requires the live provider wiring task before command-line use"
    )
