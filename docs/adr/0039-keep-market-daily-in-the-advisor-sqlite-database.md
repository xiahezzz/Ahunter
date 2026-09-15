# Keep Market Daily in the advisor SQLite database

Market Securities, Canonical Daily Bars, adjustment factors, coverage, and ingestion-run state remain in `data/advisor/advisor.sqlite`; WAL mode, bounded batch transactions, and targeted indexes provide the required concurrency and scale for the expected multi-million-row history. A second SQLite file, PostgreSQL, DuckDB, and Parquet are rejected for this scope so deployment, backup, and local research retain one database lifecycle.
