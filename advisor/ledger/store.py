from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Sequence

from advisor.db.migrate import migrate_database
from advisor.db.repository import connect
import advisor.ledger.importer as ledger_importer
from advisor.ledger.importer import LedgerImportResult
from advisor.ledger.model import (
    LedgerState,
    LedgerTransaction,
    apply_transactions,
    ledger_transaction_sort_key,
    validate_ledger_transaction,
)


class LedgerStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def append(
        self,
        account_id: str,
        transaction: LedgerTransaction,
        *,
        source: str = "manual",
        as_of: datetime | None = None,
        max_rows: int | None = None,
    ) -> LedgerImportResult:
        return self.import_many(
            [(account_id, transaction)],
            source=source,
            as_of=as_of,
            max_rows=max_rows,
        )[0]

    def import_many(
        self,
        entries: Sequence[tuple[str, LedgerTransaction]],
        *,
        source: str = "import",
        as_of: datetime | None = None,
        max_rows: int | None = None,
    ) -> tuple[LedgerImportResult, ...]:
        return ledger_importer.import_ledger_entries(
            entries,
            self.db_path,
            source=source,
            as_of=as_of,
            max_rows=max_rows,
        )

    def replay(self, *, max_rows: int | None = None) -> dict[str, LedgerState]:
        if not self.db_path.exists():
            return {}
        limit = ledger_importer.MAX_LEDGER_ROWS if max_rows is None else max_rows
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError("invalid ledger replay limit")
        connection = connect(self.db_path)
        try:
            rows = connection.execute(
                """
                SELECT transaction_id, account_id, trade_date, transaction_type, code,
                       quantity, price, amount, fees
                FROM ledger_transactions
                ORDER BY account_id, trade_date, transaction_id LIMIT ?
                """,
                (limit + 1,),
            ).fetchall()
        finally:
            connection.close()
        if len(rows) > limit:
            raise ledger_importer.LedgerCapacityError("ledger history exceeds replay limit")
        grouped: dict[str, list[LedgerTransaction]] = defaultdict(list)
        for row in rows:
            transaction = LedgerTransaction(
                row["transaction_id"],
                row["trade_date"],
                row["transaction_type"],
                row["code"],
                row["quantity"],
                row["price"],
                row["amount"],
                row["fees"],
            )
            validate_ledger_transaction(transaction)
            grouped[row["account_id"]].append(transaction)
        return {
            account_id: apply_transactions(
                sorted(transactions, key=ledger_transaction_sort_key)
            )
            for account_id, transactions in grouped.items()
        }

    def snapshot(
        self,
        account_ids: Sequence[str],
        *,
        as_of: datetime,
        max_rows: int | None = None,
        snapshot_source: str | None = None,
    ) -> dict[str, dict]:
        migrate_database(self.db_path)
        connection = connect(self.db_path)
        try:
            snapshots = ledger_importer.materialize_ledger_snapshots(
                connection,
                account_ids,
                as_of=as_of,
                max_rows=ledger_importer.MAX_LEDGER_ROWS if max_rows is None else max_rows,
                snapshot_source=snapshot_source,
            )
            connection.commit()
            return snapshots
        except sqlite3.Error:
            connection.rollback()
            raise
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
