"""Read-local-state FastAPI endpoints for the advisor dashboard."""

import base64
import hashlib
import hmac
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

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from advisor import paths as advisor_paths
from advisor.db.migrate import migrate_database
from advisor.ledger.importer import (
    LedgerCapacityError,
    LedgerConflictError,
)
from advisor.ledger.model import (
    LedgerTransaction,
    apply_transactions,
    ledger_transaction_sort_key,
    validate_ledger_transaction,
)
from advisor.ledger.store import LedgerStore
from advisor.reporting.contracts import (
    StaleArchiveCursorError,
    read_active_verified_archive,
    read_verified_archive,
)


_COMPONENTS = ("collector", "market_updater", "advisor_scheduler", "frontend", "api")
_HEALTH_STATUSES = frozenset({"ok", "healthy", "running", "degraded", "failed", "stopped", "unknown"})
_MAX_HEALTH_BYTES = 64 * 1024
_CODE_RE = re.compile(r"(?:[0368]\d{5}|(?:SH|SZ|BJ)\d{6})\Z")
_DASHBOARD_CODE_RE = re.compile(r"[0368]\d{5}\Z")
_ASSET_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_ACCOUNT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_CHART_TYPE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_LEDGER_ORDER_BY = "account_id, trade_date, transaction_id"
_MAX_LEDGER_REPLAY_ROWS = 10_000
_MAX_IMPORT_TRANSACTIONS = _MAX_LEDGER_REPLAY_ROWS
_MAX_LEDGER_REQUEST_BYTES = 512 * 1024
_MAX_LEDGER_FIELD_LENGTH = 4096
_LEDGER_PAYLOAD_KEYS = frozenset({
    "transaction_id", "account_id", "trade_date", "transaction_type", "code",
    "quantity", "price", "amount", "fees",
})
_REQUIRED_LEDGER_PAYLOAD_KEYS = _LEDGER_PAYLOAD_KEYS - {"account_id", "code"}
_MAX_LEDGER_PAYLOAD_KEYS = len(_LEDGER_PAYLOAD_KEYS)
_MAX_CHART_BYTES = 5 * 1024 * 1024
_MAX_CURRENT_RUN_CANDIDATES = 100
_MAX_CURRENT_QUALITY_CHECKS = 100
_MAX_QUALITY_DETAILS_BYTES = 64 * 1024
_MAX_CURRENT_REPORT_LINKS = 20
_MAX_REPORT_ARCHIVE_ROWS = 500
_MAX_CURRENT_PROFILE_LINKS = 100
_MAX_CURRENT_CHART_LINKS = 100
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


def create_app(state_dir: Path | None = None, db_path: Path | None = None) -> FastAPI:
    """Create the local-only advisor API without creating state on read paths."""
    resolved_state_dir = Path(state_dir) if state_dir is not None else advisor_paths.advisor_data_dir()
    resolved_db_path = Path(db_path) if db_path is not None else resolved_state_dir / "advisor.sqlite"
    report_cursor_secret = secrets.token_bytes(32)
    app = FastAPI(title="A Hunter Advisor")

    @app.get("/api/health")
    def health() -> dict:
        return _health_payload(resolved_state_dir)

    @app.get("/api/current-state")
    def current_state() -> dict:
        return _current_state(resolved_state_dir, resolved_db_path, report_cursor_secret)

    @app.get("/api/reports")
    def reports(
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        connection = _read_connection(resolved_db_path)
        try:
            page = _page_db_backed_archives(
                connection,
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
        finally:
            if connection is not None:
                connection.close()
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
        connection = _read_connection(resolved_db_path)
        try:
            quality = _resolve_current_run_quality(
                connection,
                _shanghai_today().isoformat(),
                database_present=_database_entry_present(resolved_db_path),
            )
            eligible = _eligible_report_keys(
                connection,
                advisor_paths.reports_dir(),
                candidate_keys={(report_date, report_type, run_id)},
            )
        finally:
            if connection is not None:
                connection.close()
        if not quality["safe"]:
            raise HTTPException(status_code=503, detail="current report quality unavailable")
        if (report_date, report_type, run_id) not in eligible:
            raise HTTPException(status_code=404, detail="report not found")
        try:
            archive = read_verified_archive(
                advisor_paths.reports_dir(), report_date, report_type, run_id
            )
        except (OSError, ValueError, RuntimeError):
            raise HTTPException(status_code=404, detail="report not found") from None
        return archive

    @app.get("/api/profiles")
    def profiles(limit: int = Query(default=50, ge=1, le=100)) -> dict:
        connection = _read_connection(resolved_db_path)
        try:
            return {"profiles": _read_profile_links(connection)[:limit]}
        finally:
            if connection is not None:
                connection.close()

    @app.get("/api/profiles/{code}")
    def profile(code: str) -> dict:
        if not _CODE_RE.fullmatch(code):
            raise HTTPException(status_code=404, detail="profile not found")
        connection = _read_connection(resolved_db_path)
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
        connection = _read_connection(resolved_db_path)
        try:
            return {"charts": _read_chart_links(connection, resolved_state_dir)[:limit]}
        finally:
            if connection is not None:
                connection.close()

    @app.get("/api/charts/{asset_id}")
    def chart(asset_id: str):
        if not _ASSET_ID_RE.fullmatch(asset_id):
            raise HTTPException(status_code=404, detail="chart not found")
        connection = _read_connection(resolved_db_path)
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
        connection = _read_connection(resolved_db_path)
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
    async def add_ledger_transaction(request: Request) -> dict:
        try:
            payload = await _ledger_request_payload(request)
            account_id, transaction = _transaction_from_payload(payload)
            return _write_ledger_transactions(resolved_db_path, [(account_id, transaction)], "manual")
        except _LedgerValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        except _LedgerCapacityError:
            raise HTTPException(status_code=503, detail="ledger history exceeds replay limit") from None
        except _LedgerConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @app.post("/api/ledger/import", status_code=201)
    async def import_ledger_transactions(request: Request) -> dict:
        try:
            payload = await _ledger_request_payload(request)
            rows = _validate_ledger_import_shape(payload)
            transactions = [_transaction_from_payload(item) for item in rows]
            return _write_ledger_transactions(resolved_db_path, transactions, "import")
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


def _current_state(state_dir: Path, db_path: Path, report_cursor_secret: bytes) -> dict:
    today = _shanghai_today().isoformat()
    health = _health_payload(state_dir)
    database_present = _database_entry_present(db_path)
    connection = _read_connection(db_path)
    try:
        try:
            ledger = _read_current_ledger_state(connection)
        except _LedgerCapacityError:
            ledger = {**_empty_ledger_state(), "status": "degraded"}
        flows = _read_flows(connection)
        last_successful_data_update = _last_successful_data_update(connection)
        current_quality = _resolve_current_run_quality(
            connection,
            today,
            database_present=database_present,
        )
        profiles, profile_list = _read_profile_links_with_status(connection)
        charts, chart_list = _read_chart_links_with_status(connection, state_dir)
        report_start = (
            date.fromisoformat(today) - timedelta(days=_CURRENT_REPORT_LOOKBACK_DAYS)
        ).isoformat()
        report_keys = _eligible_report_keys(
            connection,
            advisor_paths.reports_dir(),
            start_date=report_start,
            end_date=today,
        )
        try:
            eligible_reports = {
                _report_key(item)
                for item in _read_verified_report_items(advisor_paths.reports_dir(), report_keys)
            }
            report_verification_failed = False
        except (OSError, ValueError, RuntimeError):
            eligible_reports = set()
            report_verification_failed = True
    finally:
        if connection is not None:
            connection.close()

    reports, report_list = _read_report_links(today, report_cursor_secret, eligible_reports)
    if report_verification_failed:
        reports, report_list = [], {"status": "degraded", "truncated": True}
    premarket = _read_today_report(today, "premarket", eligible_reports)
    review = _read_today_report(today, "review", eligible_reports)
    premarket_status = _report_status(premarket)
    review_status = _report_status(review)
    quality_blocks_empty_state = current_quality["available"] and not current_quality["safe"]
    unbacked_premarket = premarket is None and _has_today_verified_report(today, "premarket")
    unbacked_review = review is None and _has_today_verified_report(today, "review")
    premarket_blocked = (
        premarket_status == "blocked"
        or quality_blocks_empty_state
        or (unbacked_premarket and not current_quality["safe"])
        or (premarket is not None and not current_quality["safe"])
    )
    review_blocked = (
        review_status == "blocked"
        or quality_blocks_empty_state
        or (unbacked_review and not current_quality["safe"])
        or (review is not None and not current_quality["safe"])
    )
    checks = current_quality["blocking_checks"]
    return {
        "today": today,
        "last_successful_data_update": last_successful_data_update,
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
        "profile_list": profile_list,
        "charts": charts,
        "chart_list": chart_list,
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


def _read_current_ledger_state(connection: sqlite3.Connection | None) -> dict:
    if connection is None:
        return {**_empty_ledger_state(), "status": "unknown"}
    try:
        rows = _read_capped_ledger_history(connection)
    except sqlite3.Error:
        return {**_empty_ledger_state(), "status": "degraded"}
    state = _derive_ledger_state(rows, connection)
    if rows and state == _empty_ledger_state():
        return {**state, "status": "degraded"}
    return {**state, "status": "ok"}


def _last_successful_data_update(connection: sqlite3.Connection | None) -> str | None:
    if connection is None:
        return None
    queries = (
        "SELECT MAX(fetched_at) FROM market_daily WHERE quality_status = 'passed'",
        "SELECT MAX(as_of) FROM events_normalized WHERE quality_status = 'passed'",
        """
        SELECT MAX(analyst_outputs.as_of)
        FROM analyst_outputs
        JOIN advisor_runs ON advisor_runs.run_id = analyst_outputs.run_id
        WHERE advisor_runs.status = 'passed'
        """,
        "SELECT MAX(created_at) FROM ledger_transactions",
    )
    candidates = []
    for query in queries:
        try:
            value = connection.execute(query).fetchone()[0]
            if value is not None:
                candidates.append(_parse_shanghai_datetime(value))
        except (sqlite3.Error, ValueError, TypeError):
            continue
    return max(candidates).isoformat() if candidates else None


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
    _validate_ledger_row_shape(payload)
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
        validate_ledger_transaction(transaction)
    except ValueError as error:
        raise _LedgerValidationError("invalid transaction") from error
    return account_id, transaction


async def _ledger_request_payload(request: Request) -> object:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_LEDGER_REQUEST_BYTES:
                raise HTTPException(
                    status_code=413, detail="ledger request body is too large"
                )
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid ledger request body") from None
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_LEDGER_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="ledger request body is too large")
        body.extend(chunk)
    try:
        return json.loads(bytes(body))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=422, detail="invalid ledger request body") from None


def _validate_ledger_import_shape(payload: object) -> list[dict]:
    if not isinstance(payload, list):
        raise _LedgerValidationError("invalid ledger request shape")
    if len(payload) > _MAX_IMPORT_TRANSACTIONS:
        raise _LedgerValidationError("too many transactions")
    for row in payload:
        _validate_ledger_row_shape(row)
    return payload


def _validate_ledger_row_shape(payload: object) -> None:
    if not isinstance(payload, dict) or len(payload) > _MAX_LEDGER_PAYLOAD_KEYS:
        raise _LedgerValidationError("invalid ledger request shape")
    for key, value in payload.items():
        if not isinstance(key, str):
            raise _LedgerValidationError("invalid ledger request shape")
        if len(key) > _MAX_LEDGER_FIELD_LENGTH or (
            isinstance(value, str) and len(value) > _MAX_LEDGER_FIELD_LENGTH
        ):
            raise _LedgerValidationError("ledger field is too long")
        if isinstance(value, (dict, list)):
            raise _LedgerValidationError("invalid ledger request shape")
    keys = set(payload)
    if keys - _LEDGER_PAYLOAD_KEYS or not _REQUIRED_LEDGER_PAYLOAD_KEYS <= keys:
        raise _LedgerValidationError("invalid ledger request shape")


def _write_ledger_transactions(
    db_path: Path,
    transactions: list[tuple[str, LedgerTransaction]],
    source: str,
) -> dict:
    if not transactions:
        raise _LedgerValidationError("transactions are required")
    try:
        imports = LedgerStore(db_path).import_many(
            transactions, source=source, as_of=datetime.now(_SHANGHAI),
            max_rows=_MAX_LEDGER_REPLAY_ROWS
        )
    except LedgerCapacityError as error:
        raise _LedgerCapacityError(str(error)) from error
    except LedgerConflictError as error:
        raise _LedgerConflictError(str(error)) from error
    except ValueError as error:
        raise _LedgerValidationError(str(error)) from error
    try:
        connection = sqlite3.connect(db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
    except sqlite3.Error as error:
        raise _LedgerConflictError("ledger unavailable") from error
    try:
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
                key=lambda item: (item[0], ledger_transaction_sort_key(item[1])),
            )
        ]
        result = {
            "transactions": rows,
            "ledger": _read_ledger_state(connection),
            "quality_flags": [flag for item in imports for flag in item.quality_flags],
        }
    finally:
        connection.close()
    return result


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
            state = apply_transactions(
                sorted(transactions_by_account[account_id], key=ledger_transaction_sort_key)
            )
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
    validate_ledger_transaction(transaction)
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
    normalized_checks = []
    try:
        valid_checks = bool(checks)
        for check in checks:
            created_at = _parse_shanghai_datetime(check["created_at"])
            if not (
                isinstance(check["check_name"], str)
                and 0 < len(check["check_name"]) <= 128
                and check["severity"] in {"blocking", "warning", "info"}
                and check["status"] in {"passed", "failed"}
                and _valid_quality_details(check["details_json"])
            ):
                valid_checks = False
                break
            normalized_checks.append(
                {
                    "check_name": check["check_name"],
                    "severity": check["severity"],
                    "status": check["status"],
                    "created_at": created_at.isoformat(),
                }
            )
    except (KeyError, ValueError):
        valid_checks = False
    blocking = (
        [
            check
            for check in normalized_checks
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
    if not _bounded_timestamp_string(value):
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
    return _read_profile_links_with_status(connection, dashboard_contract=False)[0]


def _read_profile_links_with_status(
    connection: sqlite3.Connection | None, *, dashboard_contract: bool = True
) -> tuple[list[dict], dict]:
    if connection is None:
        return [], {"status": "degraded"}
    try:
        rows = connection.execute(
            """
            SELECT stock_profiles.code, securities.name
            FROM stock_profiles
            LEFT JOIN securities ON securities.code = stock_profiles.code
            ORDER BY stock_profiles.code
            LIMIT ?
            """,
            (_MAX_CURRENT_PROFILE_LINKS + 1 if dashboard_contract else _MAX_CURRENT_PROFILE_LINKS,),
        ).fetchall()
    except sqlite3.Error:
        return [], {"status": "degraded"}
    if dashboard_contract and len(rows) > _MAX_CURRENT_PROFILE_LINKS:
        return [], {"status": "degraded"}
    profiles = []
    for row in rows:
        code_pattern = _DASHBOARD_CODE_RE if dashboard_contract else _CODE_RE
        if not isinstance(row["code"], str) or not code_pattern.fullmatch(row["code"]):
            return [], {"status": "degraded"}
        profile = _read_profile(connection, row["code"])
        if profile is None:
            return [], {"status": "degraded"}
        profiles.append(
            {"code": row["code"], "name": profile["name"], "href": f"/api/profiles/{row['code']}"}
        )
    return profiles, {"status": "ok"}


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
    return _read_chart_links_with_status(connection, state_dir, dashboard_contract=False)[0]


def _read_chart_links_with_status(
    connection: sqlite3.Connection | None,
    state_dir: Path,
    *,
    dashboard_contract: bool = True,
) -> tuple[list[dict], dict]:
    if connection is None:
        return [], {"status": "degraded"}
    try:
        rows = connection.execute(
            """
            SELECT asset_id, code, chart_type, as_of, path
            FROM chart_assets
            ORDER BY as_of DESC, asset_id ASC
            LIMIT ?
            """,
            (_MAX_CURRENT_CHART_LINKS + 1 if dashboard_contract else _MAX_CURRENT_CHART_LINKS,),
        ).fetchall()
    except sqlite3.Error:
        return [], {"status": "degraded"}
    if dashboard_contract and len(rows) > _MAX_CURRENT_CHART_LINKS:
        return [], {"status": "degraded"}
    charts = []
    for row in rows:
        code_pattern = _DASHBOARD_CODE_RE if dashboard_contract else _CODE_RE
        valid_chart_type = row["chart_type"] == "kline" if dashboard_contract else (
            isinstance(row["chart_type"], str) and _CHART_TYPE_RE.fullmatch(row["chart_type"])
        )
        if not (
            isinstance(row["asset_id"], str)
            and _ASSET_ID_RE.fullmatch(row["asset_id"])
            and isinstance(row["code"], str)
            and code_pattern.fullmatch(row["code"])
            and valid_chart_type
            and (
                _valid_dashboard_date(row["as_of"])
                if dashboard_contract
                else _valid_db_timestamp(row["as_of"])
            )
            and _safe_chart_path(row["path"], state_dir) is not None
        ):
            return [], {"status": "degraded"}
        charts.append(
            {
                "asset_id": row["asset_id"],
                "code": row["code"],
                "chart_type": row["chart_type"],
                "as_of": row["as_of"],
                "href": f"/api/charts/{row['asset_id']}",
            }
        )
    return charts, {"status": "ok"}


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


def _eligible_report_keys(
    connection: sqlite3.Connection | None,
    reports_root: Path,
    *,
    candidate_keys: set[tuple[str, str, str]] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> set[tuple[str, str, str]]:
    if connection is None or (candidate_keys is not None and not candidate_keys):
        return set()
    root = reports_root.resolve(strict=False)
    filters = []
    parameters: list[object] = []
    if candidate_keys is not None:
        candidate_filters = []
        for report_date, report_type in sorted(
            {(key[0], key[1]) for key in candidate_keys}
        ):
            candidate_filters.append(
                "(report_archive.report_date = ? AND report_archive.report_type = ?)"
            )
            parameters.extend((report_date, report_type))
        filters.append(f"({' OR '.join(candidate_filters)})")
    if start_date is not None:
        filters.append("report_archive.report_date >= ?")
        parameters.append(start_date)
    if end_date is not None:
        filters.append("report_archive.report_date <= ?")
        parameters.append(end_date)
    bounded_filter = f" AND {' AND '.join(filters)}" if filters else ""
    row_order = (
        "ORDER BY report_archive.report_date DESC, report_archive.created_at DESC"
        if candidate_keys is None
        else ""
    )
    try:
        rows = connection.execute(
            f"""
            SELECT report_archive.report_type, report_archive.report_date,
                   report_archive.markdown_path, report_archive.json_path
            FROM report_archive
            JOIN advisor_runs ON advisor_runs.run_id = report_archive.run_id
            WHERE ((report_archive.report_type IN ('premarket', 'review')
                    AND advisor_runs.status = 'passed')
               OR (report_archive.report_type = 'failure'
                   AND advisor_runs.status = 'blocked'))
              {bounded_filter}
            {row_order}
            """,
            parameters,
        )
    except sqlite3.Error:
        return set()

    keys: set[tuple[str, str, str]] = set()
    try:
        for row in rows:
            report_type = row["report_type"]
            report_date = row["report_date"]
            if report_type not in {"premarket", "review", "failure"}:
                continue
            try:
                if date.fromisoformat(report_date).isoformat() != report_date:
                    continue
            except (TypeError, ValueError):
                continue
            try:
                json_path = Path(row["json_path"])
                markdown_path = Path(row["markdown_path"])
            except TypeError:
                continue
            run_id = _report_run_id(report_type, json_path.name)
            if run_id is None:
                continue
            key = (report_date, report_type, run_id)
            if candidate_keys is not None and key not in candidate_keys:
                continue
            suffix = "" if run_id == "initial" else f".{run_id}"
            expected_directory = root / report_date
            if (
                json_path.resolve(strict=False)
                != expected_directory / f"{report_type}{suffix}.json"
                or markdown_path.resolve(strict=False)
                != expected_directory / f"{report_type}{suffix}.md"
            ):
                continue
            keys.add(key)
            if candidate_keys is not None and keys == candidate_keys:
                break
    except sqlite3.Error:
        return set()
    return keys


def _report_run_id(report_type: str, filename: str) -> str | None:
    if filename == f"{report_type}.json":
        return "initial"
    match = re.fullmatch(
        rf"{re.escape(report_type)}\.([A-Za-z0-9][A-Za-z0-9_-]{{0,63}})\.json",
        filename,
    )
    return match.group(1) if match is not None else None


def _report_key(report: dict) -> tuple[str, str, str]:
    return report["report_date"], report["report_type"], report["run_id"]


def _page_db_backed_archives(
    connection: sqlite3.Connection | None,
    reports_root: Path,
    *,
    limit: int,
    cursor: str | None,
    start_date: str | None,
    end_date: str | None,
    cursor_secret: bytes,
) -> dict:
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("invalid report page limit")
    today = _shanghai_today()
    start = date.fromisoformat(start_date) if start_date else today - timedelta(days=365)
    end = date.fromisoformat(end_date) if end_date else today
    if start > end or (end - start).days > 365:
        raise ValueError("invalid report date window")
    requested_start = start.isoformat()
    requested_end = end.isoformat()
    after, expected_snapshot = _decode_report_cursor(
        cursor, cursor_secret, requested_start, requested_end
    ) if cursor else (None, None)
    snapshot_digest = _report_archive_snapshot_digest(
        connection, requested_start, requested_end
    )
    if expected_snapshot is not None and not hmac.compare_digest(
        expected_snapshot, snapshot_digest
    ):
        raise StaleArchiveCursorError("stale report cursor")
    page_items, truncated, verified_count = _read_report_page_candidates(
        connection,
        reports_root,
        start_date=requested_start,
        end_date=requested_end,
        after=after,
        limit=limit,
    )
    next_cursor = (
        _encode_report_cursor(
            page_items[-1], cursor_secret, requested_start, requested_end, snapshot_digest
        )
        if truncated and page_items
        else None
    )
    return {
        "items": page_items,
        "next_cursor": next_cursor,
        "truncated": truncated,
        "requested_start_date": requested_start,
        "requested_end_date": requested_end,
        "verified_candidate_count": verified_count,
    }


def _report_archive_snapshot_digest(
    connection: sqlite3.Connection | None, start_date: str, end_date: str
) -> str:
    if connection is None:
        values = (0, None, None, 0)
    else:
        try:
            row = connection.execute(
                """
                SELECT COUNT(*), MAX(report_archive.rowid), MAX(report_archive.created_at),
                       COALESCE(SUM(LENGTH(report_archive.json_path) +
                                    LENGTH(report_archive.markdown_path)), 0)
                FROM report_archive
                JOIN advisor_runs ON advisor_runs.run_id = report_archive.run_id
                WHERE report_archive.report_type IN ('premarket', 'review')
                  AND advisor_runs.status = 'passed'
                  AND report_archive.report_date >= ?
                  AND report_archive.report_date <= ?
                """,
                (start_date, end_date),
            ).fetchone()
            values = tuple(row) if row is not None else (0, None, None, 0)
        except sqlite3.Error as error:
            raise ValueError("report archive query failed") from error
    return hashlib.sha256(
        json.dumps(values, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _read_report_page_candidates(
    connection: sqlite3.Connection | None,
    reports_root: Path,
    *,
    start_date: str,
    end_date: str,
    after: tuple[str, str, str] | None,
    limit: int,
) -> tuple[list[dict], bool, int]:
    if connection is None:
        return [], False, 0
    root = reports_root.resolve(strict=False)
    batch_size = min(100, max(20, limit * 2 + 1))
    scanned = 0
    verified = 0
    items: list[dict] = []
    last_key: tuple[str, str, str, int] | None = None
    after_path = None
    if after is not None:
        report_date, report_type, run_id = after
        suffix = "" if run_id == "initial" else f".{run_id}"
        after_path = str(root / report_date / f"{report_type}{suffix}.json")
    while scanned < _MAX_REPORT_ARCHIVE_ROWS and len(items) <= limit:
        filters = [
            "report_archive.report_date >= ?",
            "report_archive.report_date <= ?",
        ]
        parameters: list[object] = [start_date, end_date]
        if after is not None and after_path is not None:
            filters.append(
                "(report_archive.report_date, report_archive.report_type, "
                "report_archive.json_path) < (?, ?, ?)"
            )
            parameters.extend((after[0], after[1], after_path))
        if last_key is not None:
            filters.append(
                "(report_archive.report_date, report_archive.report_type, "
                "report_archive.json_path, report_archive.rowid) < (?, ?, ?, ?)"
            )
            parameters.extend(last_key)
        parameters.append(min(batch_size, _MAX_REPORT_ARCHIVE_ROWS - scanned))
        try:
            rows = connection.execute(
                f"""
                SELECT report_archive.report_type, report_archive.report_date,
                       report_archive.markdown_path, report_archive.json_path,
                       report_archive.rowid
                FROM report_archive
                JOIN advisor_runs ON advisor_runs.run_id = report_archive.run_id
                WHERE report_archive.report_type IN ('premarket', 'review')
                  AND advisor_runs.status = 'passed'
                  AND {' AND '.join(filters)}
                ORDER BY report_archive.report_date DESC,
                         report_archive.report_type DESC,
                         report_archive.json_path DESC,
                         report_archive.rowid DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        except sqlite3.Error as error:
            raise ValueError("report archive query failed") from error
        if not rows:
            break
        scanned += len(rows)
        tail = rows[-1]
        last_key = (tail["report_date"], tail["report_type"], tail["json_path"], tail["rowid"])
        for row in rows:
            item = _verified_report_row(root, reports_root, row)
            if item is None:
                continue
            verified += 1
            items.append(item)
            if len(items) > limit:
                break
        if len(rows) < batch_size:
            break
    return items[:limit], len(items) > limit or scanned >= _MAX_REPORT_ARCHIVE_ROWS, verified


def _verified_report_row(root: Path, reports_root: Path, row: sqlite3.Row) -> dict | None:
    report_type = row["report_type"]
    report_date = row["report_date"]
    try:
        if report_type not in {"premarket", "review"}:
            return None
        if date.fromisoformat(report_date).isoformat() != report_date:
            return None
        json_path = Path(row["json_path"])
        markdown_path = Path(row["markdown_path"])
    except (TypeError, ValueError):
        return None
    run_id = _report_run_id(report_type, json_path.name)
    if run_id is None:
        return None
    suffix = "" if run_id == "initial" else f".{run_id}"
    expected_directory = root / report_date
    if (
        json_path.resolve(strict=False) != expected_directory / f"{report_type}{suffix}.json"
        or markdown_path.resolve(strict=False) != expected_directory / f"{report_type}{suffix}.md"
    ):
        return None
    try:
        archive = read_verified_archive(reports_root, report_date, report_type, run_id)
    except (OSError, ValueError, RuntimeError):
        return None
    return {
        "report_date": report_date,
        "report_type": report_type,
        "run_id": run_id,
        "quality_status": archive["json"].get("quality_status", "unknown"),
    }


def _read_verified_report_items(
    reports_root: Path,
    eligible: set[tuple[str, str, str]],
) -> list[dict]:
    items = []
    for report_date, report_type, run_id in eligible:
        if report_type not in {"premarket", "review"}:
            continue
        try:
            archive = read_verified_archive(reports_root, report_date, report_type, run_id)
        except (OSError, ValueError, RuntimeError):
            continue
        items.append(
            {
                "report_date": report_date,
                "report_type": report_type,
                "run_id": run_id,
                "quality_status": archive["json"].get("quality_status", "unknown"),
            }
        )
    return sorted(items, key=_report_key, reverse=True)


def _encode_report_cursor(
    item: dict,
    secret: bytes,
    start_date: str,
    end_date: str,
    snapshot_digest: str,
) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "s": start_date,
            "e": end_date,
            "k": list(_report_key(item)),
            "g": snapshot_digest,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.digest(secret, payload, "sha256")
    return base64.urlsafe_b64encode(payload + signature).decode("ascii").rstrip("=")


def _decode_report_cursor(
    cursor: str,
    secret: bytes,
    start_date: str,
    end_date: str,
) -> tuple[tuple[str, str, str], str]:
    if not isinstance(cursor, str) or not cursor or len(cursor) > 512:
        raise ValueError("invalid report cursor")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload, signature = raw[:-32], raw[-32:]
        if not hmac.compare_digest(signature, hmac.digest(secret, payload, "sha256")):
            raise ValueError("invalid report cursor")
        values = json.loads(payload)
        report_date, report_type, run_id = values["k"]
        if (
            values.get("v") != 1
            or values.get("s") != start_date
            or values.get("e") != end_date
            or report_type not in {"premarket", "review"}
            or not isinstance(run_id, str)
            or _report_run_id(report_type, f"{report_type}.{run_id}.json") != run_id
            or date.fromisoformat(report_date).isoformat() != report_date
            or not isinstance(values.get("g"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", values["g"])
        ):
            raise ValueError("invalid report cursor")
        return (report_date, report_type, run_id), values["g"]
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid report cursor") from error


def _read_report_links(
    today: str,
    cursor_secret: bytes,
    eligible: set[tuple[str, str, str]],
) -> tuple[list[dict], dict]:
    del today, cursor_secret
    try:
        items = _read_verified_report_items(advisor_paths.reports_dir(), eligible)
    except (OSError, ValueError, RuntimeError):
        return [], {"status": "degraded", "truncated": True}
    links = [
        {
            **archive,
            "href": f"/api/reports/{archive['report_date']}/{archive['report_type']}?run_id={archive['run_id']}",
        }
        for archive in items[:_MAX_CURRENT_REPORT_LINKS]
    ]
    return links, {"status": "ok", "truncated": len(items) > _MAX_CURRENT_REPORT_LINKS}


def _read_today_report(
    today: str,
    report_type: str,
    eligible: set[tuple[str, str, str]],
) -> dict | None:
    run_ids = {
        run_id
        for report_date, candidate_type, run_id in eligible
        if report_date == today and candidate_type == report_type
    }
    if not run_ids:
        return None
    try:
        archives = {
            run_id: read_verified_archive(
                advisor_paths.reports_dir(), today, report_type, run_id
            )
            for run_id in run_ids
        }
    except (OSError, ValueError, RuntimeError):
        return None
    predecessors: set[str] = set()
    for run_id, archive in archives.items():
        supersession = archive["json"].get("supersession")
        if run_id == "initial":
            if supersession is not None:
                return None
            continue
        if (
            not isinstance(supersession, dict)
            or set(supersession) != {"reason", "supersedes"}
            or not isinstance(supersession["reason"], str)
            or supersession["supersedes"] not in archives
        ):
            return None
        predecessors.add(supersession["supersedes"])
    heads = set(archives) - predecessors
    if len(heads) != 1:
        return None
    head = heads.pop()
    seen: set[str] = set()
    current = head
    while current != "initial":
        if current in seen:
            return None
        seen.add(current)
        current = archives[current]["json"]["supersession"]["supersedes"]
    if set(archives) != seen | {"initial"}:
        return None
    return archives[head]


def _has_today_verified_report(today: str, report_type: str) -> bool:
    try:
        read_active_verified_archive(advisor_paths.reports_dir(), today, report_type)
    except (OSError, ValueError, RuntimeError):
        return False
    return True


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


def _bounded_timestamp_string(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > _MAX_DB_TIMESTAMP_LENGTH:
        return False
    try:
        return len(value.encode("utf-8")) <= _MAX_DB_TIMESTAMP_LENGTH
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


def _valid_dashboard_date(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _valid_account_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ACCOUNT_ID_RE.fullmatch(value))
