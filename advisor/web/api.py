"""Read-local-state FastAPI endpoints for the advisor dashboard."""

import json
import math
import os
import re
import secrets
import sqlite3
import stat
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse

from advisor import paths as advisor_paths
from advisor.db.migrate import migrate_database
from advisor.ledger.model import LedgerTransaction, apply_transactions
from advisor.reporting.contracts import (
    StaleArchiveCursorError,
    page_verified_archives,
    read_active_verified_archive,
    read_verified_archive,
)


_COMPONENTS = ("collector", "market_updater", "advisor_scheduler", "frontend", "api")
_HEALTH_STATUSES = frozenset({"ok", "healthy", "running", "degraded", "failed", "stopped", "unknown"})
_MAX_HEALTH_BYTES = 64 * 1024
_CODE_RE = re.compile(r"(?:[0368]\d{5}|(?:SH|SZ|BJ)\d{6})\Z")
_ASSET_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_ACCOUNT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_TRANSACTION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_CHART_TYPE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_MAX_IMPORT_TRANSACTIONS = 500
_LEDGER_ORDER_BY = "account_id, trade_date, transaction_id"
_MAX_LEDGER_REPLAY_ROWS = 10_000
_MAX_CHART_BYTES = 5 * 1024 * 1024
_MAX_CURRENT_RUN_CANDIDATES = 100
_MAX_CURRENT_QUALITY_CHECKS = 100
_MAX_QUALITY_DETAILS_BYTES = 64 * 1024
_MAX_CURRENT_REPORT_LINKS = 20
_CURRENT_REPORT_LOOKBACK_DAYS = 30
_MAX_PROFILE_JSON_BYTES = 64 * 1024
_MAX_PROFILE_JSON_DEPTH = 8
_MAX_PROFILE_JSON_ITEMS = 500
_MAX_PROFILE_STRING_LENGTH = 4096
_MAX_DB_NAME_LENGTH = 256
_MAX_DB_INDUSTRY_LENGTH = 256
_MAX_DB_TIMESTAMP_LENGTH = 64
_MAX_SQLITE_INTEGER = 2**63 - 1
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def create_app(state_dir: Path | None = None) -> FastAPI:
    """Create the local-only advisor API without creating state on read paths."""
    resolved_state_dir = Path(state_dir) if state_dir is not None else advisor_paths.advisor_data_dir()
    report_cursor_secret = secrets.token_bytes(32)
    app = FastAPI(title="A Hunter Advisor")

    @app.get("/api/health")
    def health() -> dict:
        return _health_payload(resolved_state_dir)

    @app.get("/api/current-state")
    def current_state() -> dict:
        return _current_state(resolved_state_dir, report_cursor_secret)

    @app.get("/api/reports")
    def reports(
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        try:
            page = page_verified_archives(
                advisor_paths.reports_dir(),
                limit=limit,
                cursor=cursor,
                start_date=start_date,
                end_date=end_date,
                cursor_secret=report_cursor_secret,
            )
        except StaleArchiveCursorError:
            raise HTTPException(status_code=409, detail="report cursor stale") from None
        except ValueError:
            raise HTTPException(status_code=503, detail="report listing unavailable") from None
        page["reports"] = [
            {
                **item,
                "href": f"/api/reports/{item['report_date']}/{item['report_type']}?run_id={item['run_id']}",
            }
            for item in page["items"]
        ]
        return page

    @app.get("/api/reports/{report_date}/{report_type}")
    def report(report_date: str, report_type: str, run_id: str = "initial") -> dict:
        if report_type not in {"premarket", "review"}:
            raise HTTPException(status_code=404, detail="report not found")
        try:
            archive = read_verified_archive(advisor_paths.reports_dir(), report_date, report_type, run_id)
        except (OSError, ValueError, RuntimeError):
            raise HTTPException(status_code=404, detail="report not found") from None
        db_path = resolved_state_dir / "advisor.sqlite"
        connection = _read_connection(db_path)
        try:
            quality = _resolve_current_run_quality(
                connection,
                _shanghai_today().isoformat(),
                database_present=_database_entry_present(db_path),
            )
        finally:
            if connection is not None:
                connection.close()
        if not quality["safe"]:
            raise HTTPException(status_code=503, detail="current report quality unavailable")
        return archive

    @app.get("/api/profiles")
    def profiles(limit: int = Query(default=50, ge=1, le=100)) -> dict:
        connection = _read_connection(resolved_state_dir / "advisor.sqlite")
        try:
            return {"profiles": _read_profile_links(connection)[:limit]}
        finally:
            if connection is not None:
                connection.close()

    @app.get("/api/profiles/{code}")
    def profile(code: str) -> dict:
        if not _CODE_RE.fullmatch(code):
            raise HTTPException(status_code=404, detail="profile not found")
        connection = _read_connection(resolved_state_dir / "advisor.sqlite")
        try:
            payload = _read_profile(connection, code)
        finally:
            if connection is not None:
                connection.close()
        if payload is None:
            raise HTTPException(status_code=404, detail="profile not found")
        return payload

    @app.get("/api/charts")
    def charts(limit: int = Query(default=50, ge=1, le=100)) -> dict:
        connection = _read_connection(resolved_state_dir / "advisor.sqlite")
        try:
            return {"charts": _read_chart_links(connection, resolved_state_dir)[:limit]}
        finally:
            if connection is not None:
                connection.close()

    @app.get("/api/charts/{asset_id}")
    def chart(asset_id: str):
        if not _ASSET_ID_RE.fullmatch(asset_id):
            raise HTTPException(status_code=404, detail="chart not found")
        connection = _read_connection(resolved_state_dir / "advisor.sqlite")
        try:
            descriptor = _open_chart_descriptor(connection, resolved_state_dir, asset_id)
        finally:
            if connection is not None:
                connection.close()
        if descriptor is None:
            raise HTTPException(status_code=404, detail="chart not found")
        return StreamingResponse(_stream_descriptor(descriptor), media_type="image/png")

    @app.get("/api/ledger/transactions")
    def ledger_transactions(limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0, le=1000)) -> dict:
        connection = _read_connection(resolved_state_dir / "advisor.sqlite")
        try:
            rows = _read_ledger_records(connection, limit=limit, offset=offset)
            try:
                ledger = _read_ledger_state(connection)
            except _LedgerCapacityError:
                raise HTTPException(status_code=503, detail="ledger history exceeds replay limit") from None
            return {"transactions": rows, "ledger": ledger}
        finally:
            if connection is not None:
                connection.close()

    @app.post("/api/ledger/transactions", status_code=201)
    def add_ledger_transaction(payload: dict) -> dict:
        try:
            account_id, transaction = _transaction_from_payload(payload)
            return _write_ledger_transactions(resolved_state_dir, [(account_id, transaction)], "manual")
        except _LedgerValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        except _LedgerCapacityError:
            raise HTTPException(status_code=503, detail="ledger history exceeds replay limit") from None
        except _LedgerConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @app.post("/api/ledger/import", status_code=201)
    def import_ledger_transactions(payload: list[dict]) -> dict:
        if len(payload) > _MAX_IMPORT_TRANSACTIONS:
            raise HTTPException(status_code=422, detail="too many transactions")
        try:
            transactions = [_transaction_from_payload(item) for item in payload]
            return _write_ledger_transactions(resolved_state_dir, transactions, "import")
        except _LedgerValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        except _LedgerCapacityError:
            raise HTTPException(status_code=503, detail="ledger history exceeds replay limit") from None
        except _LedgerConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    return app


def _health_payload(state_dir: Path) -> dict:
    components = {component: "unknown" for component in _COMPONENTS}
    components["api"] = "ok"
    snapshot = _load_health_snapshot(state_dir / "health.json")
    for component in _COMPONENTS:
        if component == "api":
            continue
        value = snapshot.get(component)
        if isinstance(value, str) and value in _HEALTH_STATUSES:
            components[component] = value
    return {"status": "ok", "service": "advisor-api", **components}


def _load_health_snapshot(path: Path) -> dict:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_HEALTH_BYTES:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    nested = payload.get("components")
    return nested if isinstance(nested, dict) else payload


app = create_app()


def _current_state(state_dir: Path, report_cursor_secret: bytes) -> dict:
    today = _shanghai_today().isoformat()
    health = _health_payload(state_dir)
    db_path = state_dir / "advisor.sqlite"
    connection = _read_connection(db_path)
    try:
        try:
            ledger = _read_ledger_state(connection)
        except _LedgerCapacityError:
            ledger = {**_empty_ledger_state(), "status": "degraded"}
        flows = _read_flows(connection)
        current_quality = _resolve_current_run_quality(
            connection,
            today,
            database_present=_database_entry_present(db_path),
        )
        profiles = _read_profile_links(connection)
        charts = _read_chart_links(connection, state_dir)
    finally:
        if connection is not None:
            connection.close()

    reports, report_list = _read_report_links(today, report_cursor_secret)
    premarket = _read_today_report(today, "premarket")
    review = _read_today_report(today, "review")
    premarket_status = _report_status(premarket)
    review_status = _report_status(review)
    quality_blocks_empty_state = current_quality["available"] and not current_quality["safe"]
    premarket_blocked = (
        premarket_status == "blocked"
        or quality_blocks_empty_state
        or (premarket is not None and not current_quality["safe"])
    )
    review_blocked = (
        review_status == "blocked"
        or quality_blocks_empty_state
        or (review is not None and not current_quality["safe"])
    )
    checks = current_quality["blocking_checks"]
    return {
        "today": today,
        "advice": [] if premarket_blocked else premarket["json"].get("advice", []) if premarket else [],
        "advice_status": "blocked" if premarket_blocked else premarket_status,
        "review": {
            "status": "blocked" if review_blocked else review_status,
            "items": [] if review_blocked else review["json"].get("reviews", []) if review else [],
        },
        "ledger": ledger,
        "flows": flows,
        "blocking_quality_checks": checks,
        "reports": reports,
        "report_list": report_list,
        "profiles": profiles,
        "charts": charts,
        "health": health,
    }


def _read_connection(db_path: Path) -> sqlite3.Connection | None:
    try:
        if db_path.is_symlink() or not db_path.is_file():
            return None
        connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error:
        return None


def _database_entry_present(db_path: Path) -> bool:
    try:
        db_path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _read_ledger_state(connection: sqlite3.Connection | None) -> dict:
    if connection is None:
        return _empty_ledger_state()
    try:
        rows = _read_capped_ledger_history(connection)
    except sqlite3.Error:
        return _empty_ledger_state()
    return _derive_ledger_state(rows, connection)


def _read_ledger_records(connection: sqlite3.Connection | None, *, limit: int | None = None, offset: int = 0) -> list[dict]:
    if connection is None:
        return []
    try:
        query = f"""
            SELECT transaction_id, account_id, trade_date, transaction_type, code, quantity, price, amount, fees
            FROM ledger_transactions
            ORDER BY {_LEDGER_ORDER_BY}
        """
        parameters: tuple[int, int] = ()
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            parameters = (limit, offset)
        rows = connection.execute(query, parameters).fetchall()
    except sqlite3.Error:
        return []
    records = []
    for row in rows:
        try:
            if not _valid_account_id(row["account_id"]):
                raise ValueError("invalid account")
            _ledger_transaction_from_row(row)
        except ValueError:
            return []
        records.append(dict(row))
    return records


class _LedgerValidationError(ValueError):
    pass


class _LedgerConflictError(ValueError):
    pass


class _LedgerCapacityError(ValueError):
    pass


def _read_capped_ledger_history(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = connection.execute(
        f"""
        SELECT transaction_id, account_id, trade_date, transaction_type, code, quantity, price, amount, fees
        FROM ledger_transactions
        ORDER BY {_LEDGER_ORDER_BY}
        LIMIT ?
        """,
        (_MAX_LEDGER_REPLAY_ROWS + 1,),
    ).fetchall()
    if len(rows) > _MAX_LEDGER_REPLAY_ROWS:
        raise _LedgerCapacityError("ledger history exceeds replay limit")
    return rows


def _transaction_from_payload(payload: object) -> tuple[str, LedgerTransaction]:
    if not isinstance(payload, dict):
        raise _LedgerValidationError("invalid transaction")
    allowed = {"transaction_id", "account_id", "trade_date", "transaction_type", "code", "quantity", "price", "amount", "fees"}
    required = allowed - {"account_id", "code"}
    if set(payload) - allowed or not required <= set(payload):
        raise _LedgerValidationError("invalid transaction")
    account_id = payload.get("account_id", "default")
    if not isinstance(account_id, str) or not _ACCOUNT_ID_RE.fullmatch(account_id):
        raise _LedgerValidationError("invalid account")
    transaction = LedgerTransaction(
        transaction_id=payload["transaction_id"],
        trade_date=payload["trade_date"],
        transaction_type=payload["transaction_type"],
        code=payload.get("code"),
        quantity=payload["quantity"],
        price=payload["price"],
        amount=payload["amount"],
        fees=payload["fees"],
    )
    try:
        _validate_ledger_transaction(transaction)
    except ValueError as error:
        raise _LedgerValidationError("invalid transaction") from error
    return account_id, transaction


def _write_ledger_transactions(
    state_dir: Path,
    transactions: list[tuple[str, LedgerTransaction]],
    source: str,
) -> dict:
    if not transactions:
        raise _LedgerValidationError("transactions are required")
    transaction_ids = [transaction.transaction_id for _, transaction in transactions]
    if len(set(transaction_ids)) != len(transaction_ids):
        raise _LedgerValidationError("duplicate transaction id")
    db_path = state_dir / "advisor.sqlite"
    try:
        migrate_database(db_path)
        connection = sqlite3.connect(db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as error:
        raise _LedgerConflictError("ledger unavailable") from error
    try:
        existing_rows = _read_capped_ledger_history(connection)
        if len(existing_rows) + len(transactions) > _MAX_LEDGER_REPLAY_ROWS:
            raise _LedgerCapacityError("ledger history exceeds replay limit")
        existing_ids = {row["transaction_id"] for row in existing_rows}
        if existing_ids.intersection(transaction_ids):
            raise _LedgerConflictError("duplicate transaction id")
        _validate_candidate_ledger_state(existing_rows, transactions)
        for account_id, transaction in transactions:
            now = datetime.now(_SHANGHAI).isoformat()
            connection.execute(
                "INSERT OR IGNORE INTO ledger_accounts (account_id, name, currency, created_at) VALUES (?, ?, 'CNY', ?)",
                (account_id, account_id, now),
            )
            connection.execute(
                """
                INSERT INTO ledger_transactions (
                  transaction_id, account_id, trade_date, transaction_type, code, quantity, price,
                  amount, fees, source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transaction.transaction_id,
                    account_id,
                    transaction.trade_date,
                    transaction.transaction_type,
                    transaction.code,
                    transaction.quantity,
                    transaction.price,
                    transaction.amount,
                    transaction.fees,
                    source,
                    now,
                ),
            )
        connection.commit()
        rows = [
            {
                "transaction_id": transaction.transaction_id,
                "account_id": account_id,
                "trade_date": transaction.trade_date,
                "transaction_type": transaction.transaction_type,
                "code": transaction.code,
                "quantity": transaction.quantity,
                "price": transaction.price,
                "amount": transaction.amount,
                "fees": transaction.fees,
            }
            for account_id, transaction in sorted(
                transactions,
                key=lambda item: (item[0], item[1].trade_date, item[1].transaction_id),
            )
        ]
        result = {"transactions": rows, "ledger": _read_ledger_state(connection)}
    except _LedgerConflictError:
        connection.rollback()
        raise
    except _LedgerCapacityError:
        connection.rollback()
        raise
    except (sqlite3.Error, ValueError) as error:
        connection.rollback()
        raise _LedgerValidationError("invalid transaction") from error
    finally:
        connection.close()
    return result


def _validate_candidate_ledger_state(
    existing_rows: list[sqlite3.Row],
    candidates: list[tuple[str, LedgerTransaction]],
) -> None:
    grouped: dict[str, list[LedgerTransaction]] = defaultdict(list)
    for row in existing_rows:
        try:
            if not _valid_account_id(row["account_id"]):
                raise ValueError("invalid account")
            grouped[row["account_id"]].append(_ledger_transaction_from_row(row))
        except ValueError as error:
            raise _LedgerConflictError("ledger state is invalid") from error
    for account_id, transaction in candidates:
        grouped[account_id].append(transaction)
    for account_transactions in grouped.values():
        try:
            apply_transactions(sorted(account_transactions, key=lambda transaction: (transaction.trade_date, transaction.transaction_id)))
        except ValueError as error:
            raise _LedgerValidationError("invalid transaction") from error


def _derive_ledger_state(rows: list[sqlite3.Row], connection: sqlite3.Connection | None) -> dict:
    transactions_by_account: dict[str, list[LedgerTransaction]] = defaultdict(list)
    for row in rows:
        try:
            if not _valid_account_id(row["account_id"]):
                raise ValueError("invalid account")
            transaction = _ledger_transaction_from_row(row)
        except ValueError:
            return _empty_ledger_state()
        transactions_by_account[row["account_id"]].append(transaction)

    positions: dict[str, dict[str, float | int]] = {}
    accounts = []
    cash = 0.0
    realized_pnl = 0.0
    for account_id in sorted(transactions_by_account):
        try:
            state = apply_transactions(transactions_by_account[account_id])
        except ValueError:
            return _empty_ledger_state()
        if not all(_finite_number(value) for value in (state.cash, state.realized_pnl, *state.cost_basis.values())):
            return _empty_ledger_state()
        accounts.append(
            {
                "account_id": account_id,
                "cash": state.cash,
                "realized_pnl": state.realized_pnl,
            }
        )
        cash += state.cash
        realized_pnl += state.realized_pnl
        if not _finite_number(cash) or not _finite_number(realized_pnl):
            return _empty_ledger_state()
        for code, quantity in state.positions.items():
            item = positions.setdefault(code, {"code": code, "quantity": 0, "cost_basis": 0.0})
            item["quantity"] += quantity
            item["cost_basis"] += state.cost_basis[code]

    closes = _latest_closes(connection, positions)
    unrealized_pnl = 0.0
    rendered_positions = []
    for code in sorted(positions):
        item = positions[code]
        close = closes.get(code)
        try:
            market_value = float(item["quantity"]) * close if close is not None else None
        except OverflowError:
            return _empty_ledger_state()
        if market_value is not None and not _finite_number(market_value):
            return _empty_ledger_state()
        item["market_price"] = close
        item["market_value"] = market_value
        item["unrealized_pnl"] = market_value - float(item["cost_basis"]) if market_value is not None else 0.0
        unrealized_pnl += float(item["unrealized_pnl"])
        rendered_positions.append(item)
    return {
        "cash": cash,
        "positions": rendered_positions,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "accounts": accounts,
    }


def _ledger_transaction_from_row(row: sqlite3.Row) -> LedgerTransaction:
    transaction = LedgerTransaction(
        transaction_id=row["transaction_id"],
        trade_date=row["trade_date"],
        transaction_type=row["transaction_type"],
        code=row["code"],
        quantity=row["quantity"],
        price=row["price"],
        amount=row["amount"],
        fees=row["fees"],
    )
    _validate_ledger_transaction(transaction)
    return transaction


def _latest_closes(connection: sqlite3.Connection | None, positions: dict[str, dict]) -> dict[str, float]:
    if connection is None or not positions:
        return {}
    placeholders = ",".join("?" for _ in positions)
    try:
        rows = connection.execute(
            f"""
            SELECT market_daily.code, market_daily.close
            FROM market_daily
            JOIN (
                SELECT code, MAX(trade_date) AS trade_date
                FROM market_daily
                WHERE code IN ({placeholders})
                GROUP BY code
            ) latest ON latest.code = market_daily.code AND latest.trade_date = market_daily.trade_date
            """,
            tuple(positions),
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {row["code"]: float(row["close"]) for row in rows if _finite_number(row["close"])}


def _empty_ledger_state() -> dict:
    return {"cash": 0.0, "positions": [], "realized_pnl": 0.0, "unrealized_pnl": 0.0, "accounts": []}


def _read_flows(connection: sqlite3.Connection | None) -> dict:
    return {
        "information": _flow_status(connection, "events_normalized"),
        "capital": _flow_status(connection, "market_daily"),
        "analyst": _flow_status(connection, "analyst_outputs"),
    }


def _flow_status(connection: sqlite3.Connection | None, table: str) -> dict:
    if connection is None:
        return {"status": "unknown", "count": 0}
    try:
        count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return {"status": "unknown", "count": 0}
    return {"status": "ok", "count": int(count)}


def _resolve_current_run_quality(
    connection: sqlite3.Connection | None, today: str, *, database_present: bool
) -> dict:
    if connection is None:
        return {"safe": False, "available": database_present, "blocking_checks": [], "active_run": None}
    try:
        parsed_today = date.fromisoformat(today)
        prefixes = tuple((parsed_today + timedelta(days=offset)).isoformat() for offset in (-1, 0, 1))
        rows = connection.execute(
            """
            SELECT run_id, run_type, as_of, status, started_at
            FROM advisor_runs
            WHERE (substr(as_of, 1, 10) IN (?, ?, ?)
               OR substr(started_at, 1, 10) IN (?, ?, ?))
              AND run_type IN ('premarket', 'review', 'failure')
            LIMIT ?
            """,
            (*prefixes, *prefixes, _MAX_CURRENT_RUN_CANDIDATES + 1),
        ).fetchall()
    except (sqlite3.Error, ValueError):
        return {"safe": False, "available": True, "blocking_checks": [], "active_run": None}
    if len(rows) > _MAX_CURRENT_RUN_CANDIDATES:
        return {"safe": False, "available": True, "blocking_checks": [], "active_run": None}
    candidates: list[tuple[datetime, str, sqlite3.Row]] = []
    for row in rows:
        try:
            as_of = _parse_shanghai_datetime(row["as_of"])
            started = _parse_shanghai_datetime(row["started_at"])
        except (KeyError, ValueError):
            return {"safe": False, "available": True, "blocking_checks": [], "active_run": None}
        if (
            not isinstance(row["run_id"], str)
            or not row["run_id"]
            or row["run_type"] not in {"premarket", "review", "failure"}
            or row["status"] not in {"passed", "failed", "blocked", "running"}
        ):
            return {"safe": False, "available": True, "blocking_checks": [], "active_run": None}
        if as_of.date().isoformat() == today:
            candidates.append((started, row["run_id"], row))
    if not candidates:
        return {"safe": False, "available": True, "blocking_checks": [], "active_run": None}
    latest = max(candidates, key=lambda item: (item[0], item[1]))[2]
    try:
        check_rows = connection.execute(
            """
            SELECT check_name, severity, status, details_json, created_at
            FROM data_quality_checks
            WHERE run_id = ?
            ORDER BY created_at DESC, check_name ASC
            LIMIT ?
            """,
            (latest["run_id"], _MAX_CURRENT_QUALITY_CHECKS + 1),
        ).fetchall()
    except sqlite3.Error:
        return {"safe": False, "available": True, "blocking_checks": [], "active_run": dict(latest)}
    if len(check_rows) > _MAX_CURRENT_QUALITY_CHECKS:
        return {"safe": False, "available": True, "blocking_checks": [], "active_run": dict(latest)}
    checks = [dict(row) for row in check_rows]
    try:
        valid_checks = bool(checks) and all(
            isinstance(check["check_name"], str)
            and 0 < len(check["check_name"]) <= 128
            and check["severity"] in {"blocking", "warning", "info"}
            and check["status"] in {"passed", "failed"}
            and bool(_parse_shanghai_datetime(check["created_at"]))
            and _valid_quality_details(check["details_json"])
            for check in checks
        )
    except (KeyError, ValueError):
        valid_checks = False
    blocking = (
        [
            {key: check[key] for key in ("check_name", "severity", "status", "created_at")}
            for check in checks
            if check.get("severity") == "blocking" and check.get("status") == "failed"
        ]
        if valid_checks
        else []
    )
    return {
        "safe": latest["run_type"] != "failure" and latest["status"] == "passed" and valid_checks and not blocking,
        "available": True,
        "blocking_checks": blocking,
        "active_run": dict(latest),
    }


def _parse_shanghai_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("invalid run timestamp")
    if len(value) == 10:
        try:
            return datetime.combine(date.fromisoformat(value), time.min, tzinfo=_SHANGHAI)
        except ValueError as error:
            raise ValueError("invalid run timestamp") from error
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))
    except ValueError as error:
        raise ValueError("invalid run timestamp") from error
    return parsed.replace(tzinfo=_SHANGHAI) if parsed.tzinfo is None else parsed.astimezone(_SHANGHAI)


def _shanghai_today() -> date:
    return datetime.now(_SHANGHAI).date()


def _valid_quality_details(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        if len(value.encode("utf-8")) > _MAX_QUALITY_DETAILS_BYTES:
            return False
        payload = json.loads(value)
        if not isinstance(payload, dict):
            return False
        _validate_profile_json_value(payload)
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
        return False
    return True


def _read_profile_links(connection: sqlite3.Connection | None) -> list[dict]:
    if connection is None:
        return []
    try:
        rows = connection.execute(
            """
            SELECT stock_profiles.code, securities.name
            FROM stock_profiles
            LEFT JOIN securities ON securities.code = stock_profiles.code
            ORDER BY stock_profiles.code
            LIMIT 100
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    profiles = []
    for row in rows:
        if not isinstance(row["code"], str) or not _CODE_RE.fullmatch(row["code"]):
            continue
        profile = _read_profile(connection, row["code"])
        if profile is None:
            continue
        profiles.append(
            {"code": row["code"], "name": profile["name"], "href": f"/api/profiles/{row['code']}"}
        )
    return profiles


def _read_profile(connection: sqlite3.Connection | None, code: str) -> dict | None:
    if connection is None:
        return None
    try:
        row = connection.execute(
            """
            SELECT stock_profiles.code, securities.name, securities.industry,
                   thesis_json, information_flow_json, capital_flow_json, fundamentals_json,
                   analyst_flow_json, ledger_exposure_json, assets_json, stock_profiles.updated_at
            FROM stock_profiles
            LEFT JOIN securities ON securities.code = stock_profiles.code
            WHERE stock_profiles.code = ?
            """,
            (code,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        name = row["name"] or row["code"]
        if not _bounded_db_string(name, _MAX_DB_NAME_LENGTH):
            raise ValueError("invalid profile scalar")
        industry = row["industry"]
        if industry is not None and not _bounded_db_string(industry, _MAX_DB_INDUSTRY_LENGTH):
            raise ValueError("invalid profile scalar")
        if not _valid_db_timestamp(row["updated_at"]):
            raise ValueError("invalid profile scalar")
        thesis = _load_json_field(row["thesis_json"], dict)
        information_flow = _load_json_field(row["information_flow_json"], list)
        capital_flow = _load_json_field(row["capital_flow_json"], list)
        fundamentals = _load_json_field(row["fundamentals_json"], dict)
        analyst_flow = _load_json_field(row["analyst_flow_json"], list)
        ledger_exposure = _load_json_field(row["ledger_exposure_json"], dict)
        assets = _load_json_field(row["assets_json"], list)
    except ValueError:
        return None
    if not all(isinstance(item, str) for item in information_flow + capital_flow + analyst_flow + assets):
        return None
    if any(Path(asset).is_absolute() or ".." in Path(asset).parts for asset in assets):
        return None
    return {
        "code": row["code"],
        "name": name,
        "industry": industry,
        "thesis": thesis,
        "information_flow": information_flow,
        "capital_flow": capital_flow,
        "fundamentals": fundamentals,
        "analyst_flow": analyst_flow,
        "ledger_exposure": ledger_exposure,
        "assets": assets,
        "updated_at": row["updated_at"],
    }


def _load_json_field(value: object, expected_type: type) -> object:
    if not isinstance(value, str):
        raise ValueError("invalid stored json")
    try:
        if len(value.encode("utf-8")) > _MAX_PROFILE_JSON_BYTES:
            raise ValueError("invalid stored json")
        payload = json.loads(value)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("invalid stored json") from error
    if not isinstance(payload, expected_type):
        raise ValueError("invalid stored json")
    _validate_profile_json_value(payload)
    return payload


def _validate_profile_json_value(payload: object) -> None:
    item_count = 0

    def visit(value: object, depth: int) -> None:
        nonlocal item_count
        item_count += 1
        if item_count > _MAX_PROFILE_JSON_ITEMS or depth > _MAX_PROFILE_JSON_DEPTH:
            raise ValueError("invalid stored json")
        if isinstance(value, str):
            if len(value) > _MAX_PROFILE_STRING_LENGTH:
                raise ValueError("invalid stored json")
            return
        if value is None or isinstance(value, bool):
            return
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not _finite_number(value):
                raise ValueError("invalid stored json")
            return
        if isinstance(value, list):
            for item in value:
                visit(item, depth + 1)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str) or len(key) > _MAX_PROFILE_STRING_LENGTH:
                    raise ValueError("invalid stored json")
                visit(item, depth + 1)
            return
        raise ValueError("invalid stored json")

    visit(payload, 0)


def _read_chart_links(connection: sqlite3.Connection | None, state_dir: Path) -> list[dict]:
    if connection is None:
        return []
    try:
        rows = connection.execute(
            """
            SELECT asset_id, code, chart_type, as_of, path
            FROM chart_assets
            ORDER BY as_of DESC, asset_id ASC
            LIMIT 100
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    charts = []
    for row in rows:
        if not (
            isinstance(row["asset_id"], str)
            and _ASSET_ID_RE.fullmatch(row["asset_id"])
            and isinstance(row["code"], str)
            and _CODE_RE.fullmatch(row["code"])
            and isinstance(row["chart_type"], str)
            and _CHART_TYPE_RE.fullmatch(row["chart_type"])
            and _valid_db_timestamp(row["as_of"])
            and _safe_chart_path(row["path"], state_dir) is not None
        ):
            continue
        charts.append(
            {
                "asset_id": row["asset_id"],
                "code": row["code"],
                "chart_type": row["chart_type"],
                "as_of": row["as_of"],
                "href": f"/api/charts/{row['asset_id']}",
            }
        )
    return charts


def _open_chart_descriptor(connection: sqlite3.Connection | None, state_dir: Path, asset_id: str) -> int | None:
    if connection is None:
        return None
    try:
        row = connection.execute("SELECT path FROM chart_assets WHERE asset_id = ?", (asset_id,)).fetchone()
    except sqlite3.Error:
        return None
    return _open_chart_asset_fd(row["path"], state_dir) if row is not None else None


def _open_chart_asset_fd(raw_path: object, state_dir: Path) -> int | None:
    if not isinstance(raw_path, str) or len(raw_path) > 1024:
        return None
    stored_path = Path(raw_path)
    candidates = [stored_path] if stored_path.is_absolute() else [state_dir / stored_path, advisor_paths.reports_dir() / stored_path]
    for candidate in candidates:
        for root in (state_dir / "charts", advisor_paths.reports_dir()):
            try:
                relative = candidate.relative_to(root)
                if not relative.parts or ".." in relative.parts or candidate.suffix.lower() != ".png":
                    continue
                descriptor = _open_contained_regular_fd(root, relative)
            except (OSError, ValueError):
                continue
            _chart_hook("fd_opened", descriptor=descriptor)
            return descriptor
    return None


def _open_contained_regular_fd(root: Path, relative: Path) -> int:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current_fd = os.open(root, directory_flags)
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        descriptor = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current_fd)
    finally:
        os.close(current_fd)
    try:
        entry_stat = os.fstat(descriptor)
        if not stat.S_ISREG(entry_stat.st_mode) or entry_stat.st_size > _MAX_CHART_BYTES:
            raise ValueError("invalid chart")
        if os.read(descriptor, 8) != b"\x89PNG\r\n\x1a\n":
            raise ValueError("invalid chart")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


async def _stream_descriptor(descriptor: int):
    try:
        while chunk := os.read(descriptor, 64 * 1024):
            yield chunk
    finally:
        os.close(descriptor)


def _safe_chart_path(raw_path: object, state_dir: Path) -> Path | None:
    if not isinstance(raw_path, str) or len(raw_path) > 1024:
        return None
    stored_path = Path(raw_path)
    candidates = [stored_path] if stored_path.is_absolute() else [state_dir / stored_path, advisor_paths.reports_dir() / stored_path]
    roots = (state_dir / "charts", advisor_paths.reports_dir())
    for candidate in candidates:
        for root in roots:
            if _regular_file_within(candidate, root):
                return candidate
    return None


def _regular_file_within(path: Path, root: Path) -> bool:
    try:
        root_stat = root.lstat()
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            return False
        path.relative_to(root)
        current = root
        for part in path.relative_to(root).parts:
            current = current / part
            if stat.S_ISLNK(current.lstat().st_mode):
                return False
        return path.suffix.lower() == ".png" and path.lstat().st_size <= _MAX_CHART_BYTES and stat.S_ISREG(path.lstat().st_mode)
    except (OSError, ValueError):
        return False


def _read_report_links(today: str, cursor_secret: bytes) -> tuple[list[dict], dict]:
    end = date.fromisoformat(today)
    start = end - timedelta(days=_CURRENT_REPORT_LOOKBACK_DAYS)
    try:
        page = page_verified_archives(
            advisor_paths.reports_dir(),
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            limit=_MAX_CURRENT_REPORT_LINKS,
            cursor_secret=cursor_secret,
        )
    except (OSError, ValueError, RuntimeError):
        return [], {"status": "degraded", "truncated": True}
    links = [
        {
            **archive,
            "href": f"/api/reports/{archive['report_date']}/{archive['report_type']}?run_id={archive['run_id']}",
        }
        for archive in page["items"]
    ]
    return links, {"status": "ok", "truncated": page["truncated"]}


def _read_today_report(today: str, report_type: str) -> dict | None:
    try:
        return read_active_verified_archive(advisor_paths.reports_dir(), today, report_type)
    except (OSError, ValueError, RuntimeError):
        return None


def _report_status(report: dict | None) -> str:
    if report is None:
        return "missing"
    status = report["json"].get("quality_status")
    return status if status in {"passed", "blocked"} else "blocked"


def _chart_hook(_event: str, **_context) -> None:
    return None


def _finite_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    if isinstance(value, int) and not -_MAX_SQLITE_INTEGER <= value <= _MAX_SQLITE_INTEGER:
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _bounded_db_string(value: object, max_length: int) -> bool:
    if not isinstance(value, str) or not value or len(value) > max_length:
        return False
    try:
        return len(value.encode("utf-8")) <= max_length * 4
    except UnicodeError:
        return False


def _valid_db_timestamp(value: object) -> bool:
    if not _bounded_db_string(value, _MAX_DB_TIMESTAMP_LENGTH):
        return False
    try:
        _parse_shanghai_datetime(value)
    except ValueError:
        return False
    return True


def _valid_account_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ACCOUNT_ID_RE.fullmatch(value))


def _validate_ledger_transaction(transaction: LedgerTransaction) -> None:
    if not isinstance(transaction.transaction_id, str) or not _TRANSACTION_ID_RE.fullmatch(transaction.transaction_id):
        raise ValueError("invalid transaction")
    try:
        if date.fromisoformat(transaction.trade_date).isoformat() != transaction.trade_date:
            raise ValueError("invalid trade date")
    except (TypeError, ValueError) as error:
        raise ValueError("invalid trade date") from error
    if transaction.transaction_type not in {"cash_deposit", "cash_withdrawal", "buy", "sell", "fee", "tax"}:
        raise ValueError("invalid transaction type")
    if (
        not isinstance(transaction.quantity, int)
        or isinstance(transaction.quantity, bool)
        or not 0 <= transaction.quantity <= _MAX_SQLITE_INTEGER
    ):
        raise ValueError("invalid quantity")
    if not all(_finite_number(value) for value in (transaction.price, transaction.amount, transaction.fees)):
        raise ValueError("invalid numeric amount")
    if transaction.transaction_type in {"buy", "sell"}:
        if not isinstance(transaction.code, str) or not _CODE_RE.fullmatch(transaction.code):
            raise ValueError("trade requires code")
        if transaction.quantity <= 0 or transaction.price <= 0 or transaction.fees < 0:
            raise ValueError("invalid trade")
        if transaction.transaction_type == "buy" and transaction.amount >= 0:
            raise ValueError("buy amount must be negative")
        if transaction.transaction_type == "sell" and transaction.amount <= 0:
            raise ValueError("sell amount must be positive")
        if not math.isclose(abs(transaction.amount), transaction.quantity * transaction.price, rel_tol=1e-9, abs_tol=1e-6):
            raise ValueError("trade amount does not match quantity and price")
    elif transaction.code is not None or transaction.quantity != 0 or transaction.price != 0 or transaction.fees != 0:
        raise ValueError("invalid cash transaction")
    elif transaction.transaction_type == "cash_deposit" and transaction.amount <= 0:
        raise ValueError("cash deposit must be positive")
    elif transaction.transaction_type in {"cash_withdrawal", "fee", "tax"} and transaction.amount >= 0:
        raise ValueError("cash outflow must be negative")
