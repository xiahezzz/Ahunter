import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from advisor.charts import kline
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


def test_generate_kline_chart_excludes_future_and_failed_rows(tmp_path: Path, monkeypatch):
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
          ('600519','2026-07-10',10,11,9,10.5,1000,10000,'fixture','now','2026-07-10',1,'h1','passed'),
          ('600519','2026-07-11',20,21,19,20.5,1000,10000,'fixture','now','2026-07-11',1,'h2','failed'),
          ('600519','2026-07-13',30,31,29,30.5,1000,10000,'fixture','now','2026-07-13',1,'h3','passed');
        """
    )
    connection.close()
    captured = {}

    def capture_plot(frame, **_kwargs):
        captured["dates"] = [value.date().isoformat() for value in frame.index]
        output.write_bytes(b"png")

    monkeypatch.setattr(kline.mpf, "plot", capture_plot)

    generate_kline_chart(
        db_path,
        "600519",
        output,
        as_of=datetime(2026, 7, 12, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        report_date="2026-07-12",
    )

    assert captured["dates"] == ["2026-07-10"]
