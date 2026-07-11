import sqlite3
from pathlib import Path

from advisor.charts.kline import generate_kline_chart


def test_generate_kline_chart_writes_non_empty_png(tmp_path: Path):
    db_path = tmp_path / "advisor.sqlite"
    output = tmp_path / "600519-kline.png"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE market_daily (
          code TEXT, trade_date TEXT, open REAL, high REAL, low REAL,
          close REAL, volume REAL, amount REAL, source TEXT, fetched_at TEXT,
          as_of_date TEXT, schema_version INTEGER, content_hash TEXT,
          quality_status TEXT
        );
        INSERT INTO market_daily VALUES
          ('600519','2026-07-08',10,11,9,10.5,1000,10000,'fixture','now','2026-07-08',1,'h1','passed'),
          ('600519','2026-07-09',10.5,12,10,11.5,1200,13000,'fixture','now','2026-07-09',1,'h2','passed'),
          ('600519','2026-07-10',11.5,12.5,11,12,1500,18000,'fixture','now','2026-07-10',1,'h3','passed');
        """
    )
    connection.close()
    result = generate_kline_chart(db_path, "600519", output)
    assert result == output
    assert output.exists()
    assert output.stat().st_size > 1000
