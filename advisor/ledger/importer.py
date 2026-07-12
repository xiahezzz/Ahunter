from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from advisor.config import load_advisor_config, resolve_state_db
from advisor.db.migrate import migrate_database
from advisor.db.repository import connect
from advisor.ledger.model import (
    LedgerState,
    LedgerTransaction,
    apply_transactions,
    validate_ledger_transaction,
)
from advisor.paths import repo_root


_ACCOUNT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_SOURCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class LedgerImportResult:
    status: str
    account_id: str
    imported_count: int
    snapshot_id: str
    as_of: str


def load_ledger_csv(path: Path) -> list[LedgerTransaction]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = csv.DictReader(handle)
        required = {
            "transaction_id", "trade_date", "transaction_type", "code",
            "quantity", "price", "amount", "fees",
        }
        if rows.fieldnames is None or set(rows.fieldnames) != required:
            raise ValueError("ledger CSV has invalid columns")
        transactions = []
        for row_number, row in enumerate(rows, start=2):
            try:
                transaction = LedgerTransaction(
                    transaction_id=row["transaction_id"],
                    trade_date=row["trade_date"],
                    transaction_type=row["transaction_type"],
                    code=row["code"] or None,
                    quantity=int(row["quantity"]),
                    price=float(row["price"]),
                    amount=float(row["amount"]),
                    fees=float(row["fees"]),
                )
                validate_ledger_transaction(transaction)
            except (TypeError, ValueError) as error:
                raise ValueError(f"invalid ledger CSV row {row_number}: {error}") from error
            transactions.append(transaction)
    if not transactions:
        raise ValueError("ledger CSV contains no transactions")
    return transactions


def import_ledger_csv(
    csv_path: Path,
    db_path: Path,
    *,
    account_id: str = "default",
    account_name: str | None = None,
    source: str = "csv",
    as_of: datetime | None = None,
) -> LedgerImportResult:
    transactions = load_ledger_csv(Path(csv_path))
    return import_ledger_transactions(
        transactions,
        Path(db_path),
        account_id=account_id,
        account_name=account_name,
        source=source,
        as_of=as_of,
    )


def import_ledger_transactions(
    transactions: Sequence[LedgerTransaction],
    db_path: Path,
    *,
    account_id: str = "default",
    account_name: str | None = None,
    source: str = "csv",
    as_of: datetime | None = None,
) -> LedgerImportResult:
    active_as_of = as_of or datetime.now(_SHANGHAI)
    _validate_import_request(transactions, account_id, account_name, source, active_as_of)
    ordered_candidates = sorted(transactions, key=lambda item: (item.trade_date, item.transaction_id))
    migrate_database(db_path)
    connection = connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing_rows = connection.execute(
            """
            SELECT transaction_id, trade_date, transaction_type, code, quantity, price, amount, fees
            FROM ledger_transactions WHERE account_id = ?
            ORDER BY trade_date, transaction_id
            """,
            (account_id,),
        ).fetchall()
        candidate_ids = {item.transaction_id for item in ordered_candidates}
        existing_ids = {
            row[0]
            for row in connection.execute(
                f"SELECT transaction_id FROM ledger_transactions WHERE transaction_id IN ({','.join('?' for _ in candidate_ids)})",
                tuple(candidate_ids),
            ).fetchall()
        }
        if existing_ids:
            raise ValueError("duplicate transaction id")
        existing = [_transaction_from_row(row) for row in existing_rows]
        full_transactions = sorted(
            [*existing, *ordered_candidates], key=lambda item: (item.trade_date, item.transaction_id)
        )
        full_state = apply_transactions(full_transactions)
        now = active_as_of.isoformat()
        connection.execute(
            """
            INSERT INTO ledger_accounts (account_id, name, currency, created_at)
            VALUES (?, ?, 'CNY', ?)
            ON CONFLICT(account_id) DO NOTHING
            """,
            (account_id, account_name or account_id, now),
        )
        connection.executemany(
            """
            INSERT INTO ledger_transactions (
              transaction_id, account_id, trade_date, transaction_type, code,
              quantity, price, amount, fees, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item.transaction_id, account_id, item.trade_date, item.transaction_type,
                    item.code, item.quantity, item.price, item.amount, item.fees, source, now,
                )
                for item in ordered_candidates
            ],
        )
        _replace_positions(connection, account_id, full_state, now)
        snapshot_transactions = [
            item for item in full_transactions if item.trade_date <= active_as_of.date().isoformat()
        ]
        snapshot_state = apply_transactions(snapshot_transactions)
        snapshot_id = _persist_snapshot(
            connection, account_id, active_as_of, source, snapshot_state
        )
        connection.commit()
    except sqlite3.IntegrityError as error:
        connection.rollback()
        raise ValueError("ledger import conflicts with existing data") from error
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return LedgerImportResult(
        "imported", account_id, len(ordered_candidates), snapshot_id, active_as_of.isoformat()
    )


def ledger_exposure_by_code(
    connection: sqlite3.Connection,
    codes: Sequence[str] | None = None,
    *,
    as_of: datetime | date | str | None = None,
) -> dict[str, dict[str, float | int | str | None]]:
    requested = tuple(dict.fromkeys(codes or ()))
    params: list[object] = []
    where = "WHERE quantity != 0"
    if requested:
        where += f" AND code IN ({','.join('?' for _ in requested)})"
        params.extend(requested)
    rows = connection.execute(
        f"""
        SELECT code, SUM(quantity) AS quantity, SUM(cost_basis) AS cost_basis
        FROM positions {where} GROUP BY code ORDER BY code
        """,
        params,
    ).fetchall()
    cutoff = _as_of_date(as_of)
    return _exposure_for_positions(connection, rows, cutoff)


def _validate_import_request(
    transactions: Sequence[LedgerTransaction],
    account_id: str,
    account_name: str | None,
    source: str,
    as_of: datetime,
) -> None:
    if isinstance(transactions, (str, bytes)) or not transactions:
        raise ValueError("transactions are required")
    if not isinstance(account_id, str) or not _ACCOUNT_ID_RE.fullmatch(account_id):
        raise ValueError("invalid account id")
    if account_name is not None and (
        not isinstance(account_name, str) or not account_name.strip() or len(account_name) > 256
    ):
        raise ValueError("invalid account name")
    if not isinstance(source, str) or not _SOURCE_RE.fullmatch(source):
        raise ValueError("invalid source")
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be a timezone-aware datetime")
    ids = []
    for transaction in transactions:
        if not isinstance(transaction, LedgerTransaction):
            raise ValueError("invalid transaction")
        validate_ledger_transaction(transaction)
        if transaction.trade_date > as_of.date().isoformat():
            raise ValueError("transaction trade_date is after as_of")
        ids.append(transaction.transaction_id)
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate transaction id")


def _transaction_from_row(row: sqlite3.Row) -> LedgerTransaction:
    transaction = LedgerTransaction(
        row["transaction_id"], row["trade_date"], row["transaction_type"], row["code"],
        row["quantity"], row["price"], row["amount"], row["fees"],
    )
    validate_ledger_transaction(transaction)
    return transaction


def _replace_positions(
    connection: sqlite3.Connection,
    account_id: str,
    state: LedgerState,
    updated_at: str,
) -> None:
    connection.execute("DELETE FROM positions WHERE account_id = ?", (account_id,))
    connection.executemany(
        """
        INSERT INTO positions (account_id, code, quantity, cost_basis, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (account_id, code, quantity, state.cost_basis[code], updated_at)
            for code, quantity in sorted(state.positions.items())
        ],
    )


def _persist_snapshot(
    connection: sqlite3.Connection,
    account_id: str,
    as_of: datetime,
    source: str,
    state: LedgerState,
) -> str:
    rows = [
        {"code": code, "quantity": quantity, "cost_basis": state.cost_basis[code]}
        for code, quantity in sorted(state.positions.items())
    ]
    exposure = _exposure_for_positions(connection, rows, as_of.date().isoformat())
    market_value = sum(float(item["market_value"]) for item in exposure.values())
    unrealized_pnl = sum(float(item["unrealized_pnl"]) for item in exposure.values())
    payload = {
        "positions": exposure,
        "pricing_status": (
            "passed"
            if all(item["pricing_status"] == "passed" for item in exposure.values())
            else "missing_price"
        ),
    }
    snapshot_id = "ledger-snapshot-" + hashlib.sha256(
        f"{account_id}\0{as_of.isoformat()}\0{source}".encode("utf-8")
    ).hexdigest()
    connection.execute(
        """
        INSERT INTO portfolio_snapshots (
          snapshot_id, account_id, as_of, cash, market_value, realized_pnl,
          unrealized_pnl, exposure_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(snapshot_id) DO UPDATE SET
          cash = excluded.cash,
          market_value = excluded.market_value,
          realized_pnl = excluded.realized_pnl,
          unrealized_pnl = excluded.unrealized_pnl,
          exposure_json = excluded.exposure_json
        """,
        (
            snapshot_id, account_id, as_of.isoformat(), state.cash, market_value,
            state.realized_pnl, unrealized_pnl,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
        ),
    )
    return snapshot_id


def _exposure_for_positions(
    connection: sqlite3.Connection,
    rows: Sequence[sqlite3.Row | dict],
    cutoff: str,
) -> dict[str, dict[str, float | int | str | None]]:
    exposure = {}
    for row in rows:
        code = str(row["code"])
        quantity = int(row["quantity"])
        cost_basis = float(row["cost_basis"])
        price_row = connection.execute(
            """
            SELECT close FROM market_daily
            WHERE code = ? AND quality_status = 'passed' AND trade_date <= ?
            ORDER BY trade_date DESC LIMIT 1
            """,
            (code, cutoff),
        ).fetchone()
        price = float(price_row[0]) if price_row is not None else None
        market_value = quantity * price if price is not None else 0.0
        exposure[code] = {
            "quantity": quantity,
            "cost_basis": cost_basis,
            "market_price": price,
            "market_value": market_value,
            "unrealized_pnl": market_value - cost_basis if price is not None else 0.0,
            "pricing_status": "passed" if price is not None else "missing_price",
        }
    return exposure


def _as_of_date(value: datetime | date | str | None) -> str:
    if value is None:
        return datetime.now(_SHANGHAI).date().isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except (TypeError, ValueError) as error:
        raise ValueError("invalid exposure as_of") from error


def _parse_as_of(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("as_of must be an ISO datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("as_of must include a timezone")
    return parsed


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--account-id", default="default")
    parser.add_argument("--account-name")
    parser.add_argument("--source", default="csv")
    parser.add_argument("--as-of", type=_parse_as_of, default=None)
    args = parser.parse_args(argv)
    try:
        if args.db is not None:
            db_path = args.db.resolve()
        else:
            config_path = args.config.resolve() if args.config else repo_root() / "config" / "advisor.yaml"
            root = (
                config_path.parent.parent
                if config_path.parent.name == "config"
                else config_path.parent
            )
            db_path = resolve_state_db(load_advisor_config(config_path), root)
        result = import_ledger_csv(
            args.csv_path,
            db_path,
            account_id=args.account_id,
            account_name=args.account_name,
            source=args.source,
            as_of=args.as_of,
        )
    except (OSError, sqlite3.Error, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(asdict(result), sort_keys=True))
