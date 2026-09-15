"""Shared SQLite connection factory.

Market Daily owns its immutable facts through ``advisor.market_daily.repository``.
This module deliberately contains no second market-data write path.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
