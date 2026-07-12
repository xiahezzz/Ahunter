import sqlite3
from pathlib import Path


SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def migrate_database(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        connection.commit()
    finally:
        connection.close()
