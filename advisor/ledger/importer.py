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
from typing import Any, Sequence
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
MAX_LEDGER_ROWS = 10_000


class LedgerCapacityError(ValueError):
    pass


class LedgerConflictError(ValueError):
    pass


@dataclass(frozen=True)
class LedgerImportResult:
    status: str
    account_id: str
    imported_count: int
    snapshot_id: str
    as_of: str
    quality_flags: tuple[dict[str, object], ...] = ()


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
            if len(transactions) >= MAX_LEDGER_ROWS:
                raise ValueError(f"ledger import exceeds {MAX_LEDGER_ROWS} row limit")
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
    max_rows: int | None = None,
) -> LedgerImportResult:
    results = import_ledger_entries(
        [(account_id, item) for item in transactions],
        db_path,
        account_names={account_id: account_name} if account_name is not None else None,
        source=source,
        as_of=as_of,
        max_rows=max_rows,
    )
    return results[0]


def import_ledger_entries(
    entries: Sequence[tuple[str, LedgerTransaction]],
    db_path: Path,
    *,
    account_names: dict[str, str | None] | None = None,
    source: str = "import",
    as_of: datetime | None = None,
    max_rows: int | None = None,
) -> tuple[LedgerImportResult, ...]:
    active_as_of = as_of or datetime.now(_SHANGHAI)
    replay_limit = MAX_LEDGER_ROWS if max_rows is None else max_rows
    if not isinstance(replay_limit, int) or replay_limit <= 0:
        raise ValueError("invalid ledger replay limit")
    if isinstance(entries, (str, bytes)) or not entries:
        raise ValueError("transactions are required")
    if len(entries) > replay_limit:
        raise LedgerCapacityError(f"ledger import exceeds {replay_limit} row limit")
    ordered_entries = sorted(entries, key=lambda item: (item[0], item[1].trade_date, item[1].transaction_id))
    transaction_ids = [transaction.transaction_id for _, transaction in ordered_entries]
    if len(transaction_ids) != len(set(transaction_ids)):
        raise ValueError("duplicate transaction id")
    names = account_names or {}
    for active_account_id, transaction in ordered_entries:
        _validate_import_request(
            [transaction], active_account_id, names.get(active_account_id), source, active_as_of
        )
    migrate_database(db_path)
    connection = connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing_rows = connection.execute(
            """
            SELECT transaction_id, account_id, trade_date, transaction_type, code,
                   quantity, price, amount, fees
            FROM ledger_transactions
            ORDER BY account_id, trade_date, transaction_id LIMIT ?
            """,
            (replay_limit + 1,),
        ).fetchall()
        if len(existing_rows) > replay_limit or len(existing_rows) + len(ordered_entries) > replay_limit:
            raise LedgerCapacityError("ledger history exceeds replay limit")
        existing_ids = {row["transaction_id"] for row in existing_rows}
        if existing_ids.intersection(transaction_ids):
            raise LedgerConflictError("duplicate transaction id")
        grouped = _group_stored_transactions(existing_rows)
        for active_account_id, transaction in ordered_entries:
            grouped.setdefault(active_account_id, []).append(transaction)
        affected_accounts = tuple(sorted({account_id for account_id, _ in ordered_entries}))
        for active_account_id in affected_accounts:
            grouped[active_account_id].sort(key=lambda item: (item.trade_date, item.transaction_id))
            apply_transactions(grouped[active_account_id])
        now = active_as_of.isoformat()
        for active_account_id in affected_accounts:
            connection.execute(
                """
                INSERT INTO ledger_accounts (account_id, name, currency, created_at)
                VALUES (?, ?, 'CNY', ?)
                ON CONFLICT(account_id) DO NOTHING
                """,
                (active_account_id, names.get(active_account_id) or active_account_id, now),
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
                    item.transaction_id, active_account_id, item.trade_date, item.transaction_type,
                    item.code, item.quantity, item.price, item.amount, item.fees, source, now,
                )
                for active_account_id, item in ordered_entries
            ],
        )
        snapshots = _materialize_grouped_accounts(
            connection, grouped, affected_accounts, active_as_of
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
    counts = {
        account: sum(1 for entry_account, _ in ordered_entries if entry_account == account)
        for account in affected_accounts
    }
    return tuple(
        LedgerImportResult(
            "imported", account, counts[account], snapshots[account]["snapshot_id"],
            active_as_of.isoformat(), tuple(snapshots[account]["quality_flags"]),
        )
        for account in affected_accounts
    )


def materialize_ledger_snapshots(
    connection: sqlite3.Connection,
    account_ids: Sequence[str],
    *,
    as_of: datetime,
    max_rows: int = MAX_LEDGER_ROWS,
) -> dict[str, dict[str, Any]]:
    requested = tuple(sorted(set(account_ids)))
    if not requested:
        return {}
    if any(not _ACCOUNT_ID_RE.fullmatch(account_id) for account_id in requested):
        raise ValueError("invalid account id")
    rows = connection.execute(
        """
        SELECT transaction_id, account_id, trade_date, transaction_type, code,
               quantity, price, amount, fees
        FROM ledger_transactions
        WHERE date(trade_date) <= date(?) AND julianday(created_at) <= julianday(?)
        ORDER BY account_id, trade_date, transaction_id LIMIT ?
        """,
        (as_of.date().isoformat(), as_of.isoformat(), max_rows + 1),
    ).fetchall()
    if len(rows) > max_rows:
        raise LedgerCapacityError("ledger history exceeds replay limit")
    grouped = _group_stored_transactions(rows)
    missing = set(requested) - set(grouped)
    if missing:
        raise ValueError("ledger account has no transactions")
    return _materialize_grouped_accounts(connection, grouped, requested, as_of)


def ledger_exposure_by_code(
    connection: sqlite3.Connection,
    codes: Sequence[str] | None = None,
    *,
    as_of: datetime | date | str | None = None,
) -> dict[str, dict[str, float | int | str | None]]:
    requested = tuple(dict.fromkeys(codes or ()))
    if len(requested) > MAX_LEDGER_ROWS:
        raise ValueError(f"ledger exposure exceeds {MAX_LEDGER_ROWS} code limit")
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


def _group_stored_transactions(
    rows: Sequence[sqlite3.Row],
) -> dict[str, list[LedgerTransaction]]:
    grouped: dict[str, list[LedgerTransaction]] = {}
    for row in rows:
        account_id = row["account_id"]
        if not isinstance(account_id, str) or not _ACCOUNT_ID_RE.fullmatch(account_id):
            raise ValueError("invalid stored ledger account")
        grouped.setdefault(account_id, []).append(_transaction_from_row(row))
    return grouped


def _materialize_grouped_accounts(
    connection: sqlite3.Connection,
    grouped: dict[str, list[LedgerTransaction]],
    account_ids: Sequence[str],
    as_of: datetime,
) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    cutoff = as_of.date().isoformat()
    for account_id in account_ids:
        transactions = sorted(
            grouped[account_id], key=lambda item: (item.trade_date, item.transaction_id)
        )
        full_state = apply_transactions(transactions)
        _replace_positions(connection, account_id, full_state, as_of.isoformat())
        snapshot_transactions = [item for item in transactions if item.trade_date <= cutoff]
        snapshot_state = apply_transactions(snapshot_transactions)
        quality_flags = _ledger_quality_flags(snapshot_transactions)
        snapshots[account_id] = _persist_snapshot(
            connection, account_id, as_of, snapshot_state, quality_flags
        )
    return snapshots


def _ledger_quality_flags(
    transactions: Sequence[LedgerTransaction],
) -> list[dict[str, object]]:
    flags: list[dict[str, object]] = []
    positions: dict[str, int] = {}
    active_date: str | None = None
    opening_positions: dict[str, int] = {}
    sold_today: dict[str, int] = {}
    for transaction in transactions:
        if transaction.trade_date != active_date:
            active_date = transaction.trade_date
            opening_positions = dict(positions)
            sold_today = {}
        if transaction.transaction_type not in {"buy", "sell"} or transaction.code is None:
            continue
        if transaction.quantity % 100:
            flags.append(
                {
                    "flag": "a_share_lot_size",
                    "transaction_id": transaction.transaction_id,
                    "code": transaction.code,
                    "trade_date": transaction.trade_date,
                    "quantity": transaction.quantity,
                }
            )
        if transaction.transaction_type == "buy":
            positions[transaction.code] = positions.get(transaction.code, 0) + transaction.quantity
            continue
        sold_today[transaction.code] = sold_today.get(transaction.code, 0) + transaction.quantity
        if sold_today[transaction.code] > opening_positions.get(transaction.code, 0):
            flags.append(
                {
                    "flag": "a_share_t_plus_one",
                    "transaction_id": transaction.transaction_id,
                    "code": transaction.code,
                    "trade_date": transaction.trade_date,
                    "quantity": transaction.quantity,
                }
            )
        positions[transaction.code] = positions.get(transaction.code, 0) - transaction.quantity
    return flags


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
    state: LedgerState,
    quality_flags: Sequence[dict[str, object]],
) -> dict[str, Any]:
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
        "ledger_quality": list(quality_flags),
    }
    snapshot_id = "ledger-snapshot-" + hashlib.sha256(
        f"{account_id}\0{as_of.isoformat()}".encode("utf-8")
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
    return {
        "snapshot_id": snapshot_id,
        "account_id": account_id,
        "as_of": as_of.isoformat(),
        "cash": state.cash,
        "market_value": market_value,
        "realized_pnl": state.realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "exposure": exposure,
        "pricing_status": payload["pricing_status"],
        "quality_flags": list(quality_flags),
    }


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
