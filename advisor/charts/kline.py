import sqlite3
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import mplfinance as mpf
import pandas as pd


def generate_kline_chart(db_path: Path, code: str, output_path: Path) -> Path:
    connection = sqlite3.connect(db_path)
    rows = connection.execute(
        """
        SELECT trade_date, open, high, low, close, volume
        FROM market_daily
        WHERE code = ?
        ORDER BY trade_date
        """,
        (code,),
    ).fetchall()
    connection.close()
    if not rows:
        raise ValueError(f"no market_daily rows for {code}")
    frame = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    frame["Date"] = pd.to_datetime(frame["Date"])
    frame = frame.set_index("Date")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mpf.plot(
        frame,
        type="candle",
        volume=True,
        mav=(5, 10),
        style="yahoo",
        title=f"{code} K-line",
        savefig=dict(fname=str(output_path), dpi=120, bbox_inches="tight"),
    )
    return output_path
