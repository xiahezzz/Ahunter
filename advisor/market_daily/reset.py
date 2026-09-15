"""Explicit, narrowly scoped cleanup for obsolete Market Daily facts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from advisor.db.migrate import migrate_database, recreate_empty_market_daily_table
from advisor.db.repository import connect


@dataclass(frozen=True)
class MarketResetResult:
    deleted: dict[str, int]

    @property
    def total_deleted(self) -> int:
        return sum(self.deleted.values())


class MarketDailyReset:
    """Clear only Market Daily facts; never touch research, reports, or ledger data."""

    _tables = (
        "market_daily_repairs",
        "market_daily_run_items",
        "market_daily_run_securities",
        "market_daily_runs",
        "market_daily_requests",
        "market_daily_service_leases",
        "market_daily_absences",
        "market_adjustment_factors",
        "trading_session_observations",
        "trading_sessions",
        "market_daily",
    )
    _legacy_source = "sina_http"
    _legacy_calendar_table = "trading_calendar_proofs"

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        migrate_database(self.database_path)

    def preview(self) -> dict[str, int]:
        connection = connect(self.database_path)
        try:
            result = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in self._tables
            }
            result["legacy_market_sources"] = int(
                connection.execute("SELECT COUNT(*) FROM market_sources WHERE source = ?", (self._legacy_source,)).fetchone()[0]
            )
            if self._table_exists(connection, self._legacy_calendar_table):
                result["legacy_trading_calendar_proofs"] = int(
                    connection.execute(f"SELECT COUNT(*) FROM {self._legacy_calendar_table}").fetchone()[0]
                )
            return result
        finally:
            connection.close()

    def clear(self) -> MarketResetResult:
        """Perform the exact destructive cleanup after an explicit live preflight."""

        connection = connect(self.database_path)
        result: MarketResetResult | None = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            deleted: dict[str, int] = {}
            for table in self._tables:
                deleted[table] = connection.execute(f"DELETE FROM {table}").rowcount
            deleted["legacy_market_sources"] = connection.execute(
                "DELETE FROM market_sources WHERE source = ?", (self._legacy_source,)
            ).rowcount
            if self._table_exists(connection, self._legacy_calendar_table):
                deleted["legacy_trading_calendar_proofs"] = connection.execute(
                    f"DELETE FROM {self._legacy_calendar_table}"
                ).rowcount
            connection.commit()
            result = MarketResetResult(deleted)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        # The old project used a different declaration for this table.  Facts
        # are already gone at this point, so recreate the sole market-fact
        # table from the repository's current contract rather than carrying a
        # compatibility layout into the new baseline.
        recreate_empty_market_daily_table(self.database_path)
        assert result is not None
        return result

    @staticmethod
    def _table_exists(connection, table: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone() is not None
