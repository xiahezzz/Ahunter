"""Durable control plane for Market Daily work.

The data plane may take hours to fetch a whole market.  This module keeps the
small, transactional state needed to resume that work after a process restart;
it deliberately does not know about providers, HTTP, or scheduler details.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Mapping

from advisor.db.migrate import migrate_database
from advisor.db.repository import connect
from advisor.market_daily.contracts import exchange_for_code


_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_SENSITIVE_ERROR_RE = re.compile(
    r"(?:"
    r"(?:debug|trace|request)[ _-]?id\s*[:=]|"
    r"(?:response(?:[_ -]?(?:body|text|json))?|body|payload)\s*[:=]|"
    r"(?:authorization|cookie|token)\s*[:=]|"
    r"(?:调试|跟踪|请求)[ _-]?(?:编号|标识)\s*[:：=]|"
    r"(?:响应(?:正文|体)?|载荷)\s*[:：=]"
    r")",
    re.IGNORECASE,
)
_MAX_PERSISTED_ERROR_LENGTH = 280
_REQUEST_TYPES = frozenset({"cold_start", "catch_up"})
_REQUEST_STATES = frozenset({"pending", "claimed", "completed", "failed", "cancelled"})
_RUN_STATES = frozenset({"pending", "running", "partial", "complete", "failed", "cancelled"})
_ITEM_STATES = frozenset({"pending", "running", "completed", "source_missing", "conflicted", "skipped"})
_TERMINAL_ITEM_STATES = frozenset({"completed", "source_missing", "conflicted", "skipped"})
_LEASE_NAME = "market-daily"


class MarketRunError(RuntimeError):
    """A caller attempted an invalid Market Daily state transition."""


def _require_date(value: object, field_name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise MarketRunError(f"{field_name} 必须是日期")
    return value


def _require_time(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MarketRunError(f"{field_name} 必须带时区")
    return value


def _require_text(value: object, field_name: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise MarketRunError(f"{field_name} 必须是非空文本")
    return value.strip()


def bounded_error_message(value: object, *, fallback: str = "数据源处理失败") -> str:
    """Return a bounded operational error without retaining response details.

    Provider exceptions are untrusted transport text.  The control plane may
    retain a short explanation for operators, but must never turn the database
    into a store for response bodies, debug IDs, or credentials.
    """

    fallback = _require_text(fallback, "错误回退信息", _MAX_PERSISTED_ERROR_LENGTH)
    if not isinstance(value, str):
        return fallback
    raw = value.strip()
    if not raw:
        return fallback
    if (
        "\n" in raw
        or "\r" in raw
        or any(marker in raw for marker in ("<", ">", "{", "}"))
        or _SENSITIVE_ERROR_RE.search(raw)
    ):
        return f"{fallback}（敏感详情已省略）"
    return " ".join(raw.split())[:_MAX_PERSISTED_ERROR_LENGTH]


def _timestamp(value: datetime) -> str:
    return _require_time(value, "时间").isoformat()


def _from_timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class RunSecurity:
    """The frozen eligible-security snapshot attached to one run."""

    code: str
    name: str
    exchange: str
    list_date: date
    delist_date: date | None
    status: str

    def __post_init__(self) -> None:
        try:
            expected_exchange = exchange_for_code(self.code)
        except ValueError as error:
            raise MarketRunError("证券代码必须属于沪深 A 股范围") from error
        exchange = _require_text(self.exchange, "交易所", 2).upper()
        if exchange != expected_exchange:
            raise MarketRunError("证券代码与交易所不匹配")
        list_date = _require_date(self.list_date, "上市日期")
        delist_date = self.delist_date
        if delist_date is not None:
            _require_date(delist_date, "退市日期")
            if delist_date < list_date:
                raise MarketRunError("退市日期不能早于上市日期")
        status = _require_text(self.status, "证券状态", 32).lower()
        if status not in {"active", "delisted", "suspended"}:
            raise MarketRunError("证券状态无效")
        object.__setattr__(self, "exchange", exchange)
        object.__setattr__(self, "name", _require_text(self.name, "证券名称", 256))
        object.__setattr__(self, "status", status)


@dataclass(frozen=True)
class MarketDailyRequest:
    request_id: str
    request_type: str
    target_session: date | None
    start_date: date | None
    end_date: date | None
    status: str
    created_at: datetime
    claimed_by: str | None
    claimed_at: datetime | None
    completed_at: datetime | None
    message: str | None


@dataclass(frozen=True)
class MarketDailyRun:
    run_id: str
    request_id: str
    run_type: str
    status: str
    target_session: date
    start_date: date
    end_date: date
    universe_hash: str
    total_items: int
    completed_items: int
    failed_items: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    message: str | None


@dataclass(frozen=True)
class MarketDailyRunItem:
    run_id: str
    code: str
    start_date: date
    end_date: date
    status: str
    attempts: int
    selected_source: str | None
    last_error: str | None
    claimed_by: str | None
    claim_expires_at: datetime | None
    updated_at: datetime
    completed_at: datetime | None


class MarketDailyControlPlane:
    """SQLite-backed queue, run ledger, and exclusive service lease."""

    def __init__(self, database_path: Path | str, *, ensure_schema: bool = True) -> None:
        self.database_path = Path(database_path)
        if ensure_schema:
            migrate_database(self.database_path)

    def submit_cold_start(
        self,
        target_session: date,
        start_date: date,
        now: datetime,
    ) -> MarketDailyRequest:
        target_session = _require_date(target_session, "目标交易日")
        start_date = _require_date(start_date, "开始日期")
        if start_date > target_session:
            raise MarketRunError("开始日期不能晚于目标交易日")
        return self._submit_request(
            request_type="cold_start",
            target_session=target_session,
            start_date=start_date,
            end_date=target_session,
            now=now,
        )

    def submit_cold_start_intent(self, now: datetime) -> MarketDailyRequest:
        """Persist one operator-requested cold start without contacting a provider.

        The service resolves the latest observed session only after its 21:00
        boundary.  Keeping this request intentionally date-less prevents a
        CLI invocation from accidentally freezing a stale or not-yet-proven
        session.
        """

        created_at = _timestamp(now)
        idempotency_key = "cold_start:operator_intent:v1"
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        request_id = f"mdreq-{digest[:24]}"
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM market_daily_requests WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO market_daily_requests (
                      request_id, request_type, idempotency_key, status, created_at
                    ) VALUES (?, 'cold_start', ?, 'pending', ?)
                    """,
                    (request_id, idempotency_key, created_at),
                )
                row = connection.execute(
                    "SELECT * FROM market_daily_requests WHERE request_id = ?", (request_id,)
                ).fetchone()
            else:
                row = existing
            connection.commit()
            return self._request_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def submit_catch_up(
        self,
        start_date: date,
        end_date: date,
        now: datetime,
        *,
        scope_hash: str | None = None,
    ) -> MarketDailyRequest:
        start_date = _require_date(start_date, "开始日期")
        end_date = _require_date(end_date, "结束日期")
        if start_date > end_date:
            raise MarketRunError("开始日期不能晚于结束日期")
        if scope_hash is not None and (not isinstance(scope_hash, str) or not _HASH_RE.fullmatch(scope_hash)):
            raise MarketRunError("补洞范围哈希必须是 64 位小写十六进制")
        return self._submit_request(
            request_type="catch_up",
            target_session=end_date,
            start_date=start_date,
            end_date=end_date,
            now=now,
            idempotency_suffix=scope_hash,
        )

    def _submit_request(
        self,
        *,
        request_type: str,
        target_session: date,
        start_date: date,
        end_date: date,
        now: datetime,
        idempotency_suffix: str | None = None,
    ) -> MarketDailyRequest:
        if request_type not in _REQUEST_TYPES:
            raise MarketRunError("请求类型无效")
        created_at = _timestamp(now)
        idempotency_parts = (request_type, target_session.isoformat(), start_date.isoformat(), end_date.isoformat())
        idempotency_key = ":".join((*idempotency_parts, idempotency_suffix)) if idempotency_suffix else ":".join(idempotency_parts)
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        request_id = f"mdreq-{digest[:24]}"
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM market_daily_requests WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO market_daily_requests (
                      request_id, request_type, target_session, start_date, end_date,
                      idempotency_key, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        request_id,
                        request_type,
                        target_session.isoformat(),
                        start_date.isoformat(),
                        end_date.isoformat(),
                        idempotency_key,
                        created_at,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM market_daily_requests WHERE request_id = ?", (request_id,)
                ).fetchone()
            else:
                row = existing
            connection.commit()
            return self._request_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def configure_claimed_request(
        self,
        request_id: str,
        target_session: date,
        start_date: date,
        end_date: date,
        now: datetime,
    ) -> MarketDailyRequest:
        """Freeze a previously claimed intent into a concrete date window."""

        request_id = _require_text(request_id, "请求编号", 128)
        target_session = _require_date(target_session, "目标交易日")
        start_date = _require_date(start_date, "开始日期")
        end_date = _require_date(end_date, "结束日期")
        _require_time(now, "当前时间")
        if start_date > target_session or target_session != end_date:
            raise MarketRunError("冷启动窗口必须以目标交易日结束")
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM market_daily_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise MarketRunError("请求不存在")
            if row["status"] != "claimed":
                raise MarketRunError("只有已认领的请求可以冻结窗口")
            existing = (row["target_session"], row["start_date"], row["end_date"])
            proposed = (target_session.isoformat(), start_date.isoformat(), end_date.isoformat())
            if any(existing) and existing != proposed:
                raise MarketRunError("已冻结的请求窗口不能变更")
            connection.execute(
                """
                UPDATE market_daily_requests
                SET target_session = ?, start_date = ?, end_date = ?
                WHERE request_id = ?
                """,
                (*proposed, request_id),
            )
            frozen = connection.execute(
                "SELECT * FROM market_daily_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            connection.commit()
            return self._request_from_row(frozen)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def pending_request_count(self) -> int:
        connection = connect(self.database_path)
        try:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM market_daily_requests WHERE status = 'pending'"
                ).fetchone()[0]
            )
        finally:
            connection.close()

    def acquire_lease(self, owner_id: str, now: datetime, *, lease_seconds: int = 60) -> bool:
        owner_id = _require_text(owner_id, "服务实例", 128)
        now = _require_time(now, "当前时间")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds <= 0:
            raise MarketRunError("租约秒数必须为正整数")
        expires_at = now + timedelta(seconds=lease_seconds)
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT owner_id, expires_at FROM market_daily_service_leases WHERE lease_name = ?",
                (_LEASE_NAME,),
            ).fetchone()
            can_acquire = current is None
            if current is not None:
                current_expiry = _from_timestamp(current["expires_at"])
                can_acquire = current["owner_id"] == owner_id or current_expiry is None or current_expiry <= now
            if not can_acquire:
                connection.commit()
                return False
            connection.execute(
                """
                INSERT INTO market_daily_service_leases (
                  lease_name, owner_id, acquired_at, heartbeat_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(lease_name) DO UPDATE SET
                  owner_id = excluded.owner_id,
                  acquired_at = excluded.acquired_at,
                  heartbeat_at = excluded.heartbeat_at,
                  expires_at = excluded.expires_at
                """,
                (_LEASE_NAME, owner_id, now.isoformat(), now.isoformat(), expires_at.isoformat()),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def renew_lease(self, owner_id: str, now: datetime, *, lease_seconds: int = 60) -> bool:
        return self.acquire_lease(owner_id, now, lease_seconds=lease_seconds)

    def release_lease(self, owner_id: str) -> bool:
        """Relinquish only the caller's own lease during graceful shutdown."""

        owner_id = _require_text(owner_id, "服务实例", 128)
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            deleted = connection.execute(
                "DELETE FROM market_daily_service_leases WHERE lease_name = ? AND owner_id = ?",
                (_LEASE_NAME, owner_id),
            ).rowcount
            connection.commit()
            return deleted == 1
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_next_request(self, owner_id: str, now: datetime) -> MarketDailyRequest | None:
        owner_id = _require_text(owner_id, "服务实例", 128)
        now = _require_time(now, "当前时间")
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            candidate = connection.execute(
                """
                SELECT * FROM market_daily_requests
                WHERE status = 'pending'
                ORDER BY CASE request_type WHEN 'cold_start' THEN 0 ELSE 1 END, created_at, request_id
                LIMIT 1
                """
            ).fetchone()
            if candidate is None:
                connection.commit()
                return None
            changed = connection.execute(
                """
                UPDATE market_daily_requests
                SET status = 'claimed', claimed_by = ?, claimed_at = ?
                WHERE request_id = ? AND status = 'pending'
                """,
                (owner_id, now.isoformat(), candidate["request_id"]),
            ).rowcount
            if changed != 1:
                connection.rollback()
                return None
            row = connection.execute(
                "SELECT * FROM market_daily_requests WHERE request_id = ?", (candidate["request_id"],)
            ).fetchone()
            connection.commit()
            return self._request_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_recovered_request(self, owner_id: str, now: datetime) -> MarketDailyRequest | None:
        """Claim durable work that had started before a crash or deferral."""

        owner_id = _require_text(owner_id, "服务实例", 128)
        now = _require_time(now, "当前时间")
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            candidate = connection.execute(
                """
                SELECT q.* FROM market_daily_requests AS q
                WHERE q.status = 'pending' AND q.claimed_at IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM market_daily_runs AS r WHERE r.request_id = q.request_id
                  )
                ORDER BY CASE q.request_type WHEN 'cold_start' THEN 0 ELSE 1 END,
                         q.claimed_at, q.created_at, q.request_id
                LIMIT 1
                """
            ).fetchone()
            if candidate is None:
                connection.commit()
                return None
            changed = connection.execute(
                """
                UPDATE market_daily_requests
                SET status = 'claimed', claimed_by = ?, claimed_at = ?
                WHERE request_id = ? AND status = 'pending' AND claimed_at IS NOT NULL
                """,
                (owner_id, now.isoformat(), candidate["request_id"]),
            ).rowcount
            if changed != 1:
                connection.rollback()
                return None
            row = connection.execute(
                "SELECT * FROM market_daily_requests WHERE request_id = ?", (candidate["request_id"],)
            ).fetchone()
            connection.commit()
            return self._request_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def release_request_claim(
        self,
        request_id: str,
        owner_id: str,
        now: datetime,
        *,
        message: str | None = None,
    ) -> MarketDailyRequest:
        """Return an unresolved claimed request to the durable queue."""

        request_id = _require_text(request_id, "请求编号", 128)
        owner_id = _require_text(owner_id, "服务实例", 128)
        now = _require_time(now, "当前时间")
        if message is not None:
            message = bounded_error_message(message, fallback="请求暂缓处理")
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM market_daily_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise MarketRunError("请求不存在")
            if row["status"] != "claimed" or row["claimed_by"] != owner_id:
                raise MarketRunError("只能释放本服务认领的请求")
            if connection.execute(
                "SELECT 1 FROM market_daily_runs WHERE request_id = ? LIMIT 1", (request_id,)
            ).fetchone() is not None:
                raise MarketRunError("已创建运行的请求不能释放")
            connection.execute(
                """
                UPDATE market_daily_requests
                SET status = 'pending', claimed_by = NULL, message = ?
                WHERE request_id = ?
                """,
                (message, request_id),
            )
            released = connection.execute(
                "SELECT * FROM market_daily_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            connection.commit()
            return self._request_from_row(released)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete_claimed_request(
        self,
        request_id: str,
        owner_id: str,
        now: datetime,
        *,
        message: str,
    ) -> MarketDailyRequest:
        """Seal a claimed no-op request without manufacturing an empty run."""

        request_id = _require_text(request_id, "请求编号", 128)
        owner_id = _require_text(owner_id, "服务实例", 128)
        now = _require_time(now, "当前时间")
        message = bounded_error_message(message, fallback="请求已完成")
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM market_daily_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise MarketRunError("请求不存在")
            if row["status"] != "claimed" or row["claimed_by"] != owner_id:
                raise MarketRunError("只能完成本服务认领的请求")
            if connection.execute(
                "SELECT 1 FROM market_daily_runs WHERE request_id = ? LIMIT 1", (request_id,)
            ).fetchone() is not None:
                raise MarketRunError("已有运行的请求不能作为空操作完成")
            connection.execute(
                """
                UPDATE market_daily_requests
                SET status = 'completed', completed_at = ?, message = ?
                WHERE request_id = ?
                """,
                (now.isoformat(), message, request_id),
            )
            completed = connection.execute(
                "SELECT * FROM market_daily_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            connection.commit()
            return self._request_from_row(completed)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_run(
        self,
        request_id: str,
        universe_hash: str,
        securities: tuple[RunSecurity, ...],
        now: datetime,
        *,
        item_ranges: Mapping[str, tuple[date, date]] | None = None,
    ) -> MarketDailyRun:
        request_id = _require_text(request_id, "请求编号", 128)
        if not isinstance(universe_hash, str) or not _HASH_RE.fullmatch(universe_hash):
            raise MarketRunError("股票池哈希必须是 64 位小写十六进制")
        now = _require_time(now, "当前时间")
        if not isinstance(securities, tuple) or not securities:
            raise MarketRunError("股票池快照不能为空且必须为元组")
        if any(not isinstance(security, RunSecurity) for security in securities):
            raise MarketRunError("股票池包含无效证券")
        codes = tuple(security.code for security in securities)
        if len(codes) != len(set(codes)):
            raise MarketRunError("股票池不能包含重复证券")
        if item_ranges is not None:
            if not isinstance(item_ranges, Mapping) or set(item_ranges) != set(codes):
                raise MarketRunError("逐证券区间必须与冻结股票池完全对应")
            normalized_ranges: dict[str, tuple[date, date]] = {}
            for code, interval in item_ranges.items():
                if not isinstance(interval, tuple) or len(interval) != 2:
                    raise MarketRunError("逐证券区间无效")
                item_start = _require_date(interval[0], "逐证券开始日期")
                item_end = _require_date(interval[1], "逐证券结束日期")
                if item_start > item_end:
                    raise MarketRunError("逐证券开始日期不能晚于结束日期")
                normalized_ranges[code] = (item_start, item_end)
        else:
            normalized_ranges = {}
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            request = connection.execute(
                "SELECT * FROM market_daily_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if request is None:
                raise MarketRunError("请求不存在")
            if request["status"] != "claimed":
                raise MarketRunError("只有已认领的请求可以创建运行")
            existing = connection.execute(
                "SELECT * FROM market_daily_runs WHERE request_id = ? ORDER BY created_at LIMIT 1", (request_id,)
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._run_from_row(existing)
            target_session = request["target_session"] or request["end_date"]
            start_date = request["start_date"]
            end_date = request["end_date"]
            if not target_session or not start_date or not end_date:
                raise MarketRunError("请求缺少日期范围")
            request_start = date.fromisoformat(start_date)
            request_end = date.fromisoformat(end_date)
            for code, (item_start, item_end) in normalized_ranges.items():
                if item_start < request_start or item_end > request_end:
                    raise MarketRunError(f"{code} 的逐证券区间超出请求窗口")
            run_id = f"mdrun-{uuid.uuid4().hex}"
            connection.execute(
                """
                INSERT INTO market_daily_runs (
                  run_id, request_id, run_type, status, target_session, start_date, end_date,
                  universe_hash, total_items, completed_items, failed_items, created_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, 0, 0, ?)
                """,
                (
                    run_id,
                    request_id,
                    request["request_type"],
                    target_session,
                    start_date,
                    end_date,
                    universe_hash,
                    len(securities),
                    now.isoformat(),
                ),
            )
            connection.executemany(
                """
                INSERT INTO market_daily_run_securities (
                  run_id, code, name, exchange, list_date, delist_date, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        security.code,
                        security.name,
                        security.exchange,
                        security.list_date.isoformat(),
                        security.delist_date.isoformat() if security.delist_date else None,
                        security.status,
                    )
                    for security in securities
                ],
            )
            connection.executemany(
                """
                INSERT INTO market_daily_run_items (
                  run_id, code, start_date, end_date, status, attempts, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, ?)
                """,
                [
                    (
                        run_id,
                        security.code,
                        normalized_ranges.get(security.code, (request_start, request_end))[0].isoformat(),
                        normalized_ranges.get(security.code, (request_start, request_end))[1].isoformat(),
                        now.isoformat(),
                    )
                    for security in securities
                ],
            )
            row = connection.execute("SELECT * FROM market_daily_runs WHERE run_id = ?", (run_id,)).fetchone()
            connection.commit()
            return self._run_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def start_run(self, run_id: str, now: datetime) -> MarketDailyRun:
        run_id = _require_text(run_id, "运行编号", 128)
        now = _require_time(now, "当前时间")
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT * FROM market_daily_runs WHERE run_id = ?", (run_id,)).fetchone()
            if current is None:
                raise MarketRunError("运行不存在")
            if current["status"] != "pending":
                raise MarketRunError("只有待运行任务可以启动")
            connection.execute(
                "UPDATE market_daily_runs SET status = 'running', started_at = ? WHERE run_id = ?",
                (now.isoformat(), run_id),
            )
            row = connection.execute("SELECT * FROM market_daily_runs WHERE run_id = ?", (run_id,)).fetchone()
            connection.commit()
            return self._run_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def resume_partial_run(self, run_id: str, now: datetime) -> MarketDailyRun:
        """Retry only source-missing items from a sealed partial run.

        Conflicts are deliberately left terminal: replacing a canonical bar
        requires the separately audited repair operation.
        """

        run_id = _require_text(run_id, "运行编号", 128)
        now = _require_time(now, "当前时间")
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM market_daily_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if current is None:
                raise MarketRunError("运行不存在")
            if current["status"] != "partial":
                raise MarketRunError("只有部分完成的任务可以恢复")
            connection.execute(
                """
                UPDATE market_daily_run_items
                SET status = 'pending', claimed_by = NULL, claim_expires_at = NULL,
                    updated_at = ?, completed_at = NULL
                WHERE run_id = ? AND status = 'source_missing'
                """,
                (now.isoformat(), run_id),
            )
            connection.execute(
                """
                UPDATE market_daily_runs
                SET status = 'running', started_at = ?, finished_at = NULL
                WHERE run_id = ?
                """,
                (now.isoformat(), run_id),
            )
            self._refresh_run_progress(connection, run_id)
            row = connection.execute("SELECT * FROM market_daily_runs WHERE run_id = ?", (run_id,)).fetchone()
            connection.commit()
            return self._run_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_item(
        self,
        run_id: str,
        code: str,
        status: str,
        now: datetime,
        *,
        selected_source: str | None = None,
        error: str | None = None,
    ) -> MarketDailyRunItem:
        run_id = _require_text(run_id, "运行编号", 128)
        try:
            exchange_for_code(code)
        except ValueError as exc:
            raise MarketRunError("证券代码必须属于沪深 A 股范围") from exc
        if status not in _ITEM_STATES or status == "pending":
            raise MarketRunError("逐证券状态无效")
        now = _require_time(now, "当前时间")
        if selected_source is not None:
            selected_source = _require_text(selected_source, "数据源", 128)
        if error is not None:
            error = bounded_error_message(error)
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute("SELECT status FROM market_daily_runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                raise MarketRunError("运行不存在")
            item = connection.execute(
                "SELECT * FROM market_daily_run_items WHERE run_id = ? AND code = ?", (run_id, code)
            ).fetchone()
            if item is None:
                raise MarketRunError("证券不属于该运行的冻结股票池")
            if run["status"] != "running":
                raise MarketRunError("只有运行中的任务可以更新逐证券状态")
            if item["status"] in _TERMINAL_ITEM_STATES and item["status"] != status:
                raise MarketRunError("终态逐证券任务不能再次转换")
            attempts = int(item["attempts"]) + (1 if status == "running" and item["status"] != "running" else 0)
            completed_at = now.isoformat() if status in _TERMINAL_ITEM_STATES else None
            connection.execute(
                """
                UPDATE market_daily_run_items
                SET status = ?, attempts = ?, selected_source = COALESCE(?, selected_source),
                    last_error = COALESCE(?, last_error),
                    claimed_by = CASE WHEN ? IN ('completed', 'source_missing', 'conflicted', 'skipped') THEN NULL ELSE claimed_by END,
                    claim_expires_at = CASE WHEN ? IN ('completed', 'source_missing', 'conflicted', 'skipped') THEN NULL ELSE claim_expires_at END,
                    updated_at = ?, completed_at = ?
                WHERE run_id = ? AND code = ?
                """,
                (
                    status,
                    attempts,
                    selected_source,
                    error,
                    status,
                    status,
                    now.isoformat(),
                    completed_at,
                    run_id,
                    code,
                ),
            )
            if status in _TERMINAL_ITEM_STATES:
                self._refresh_run_progress(connection, run_id)
            row = connection.execute(
                "SELECT * FROM market_daily_run_items WHERE run_id = ? AND code = ?", (run_id, code)
            ).fetchone()
            connection.commit()
            return self._item_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _refresh_run_progress(connection: sqlite3.Connection, run_id: str) -> None:
        # Derive from durable items in the same transaction. This also repairs
        # stale counters left by older workers without double-counting retries.
        connection.execute(
            """
            UPDATE market_daily_runs
            SET (completed_items, failed_items) = (
                SELECT
                    COALESCE(SUM(status IN ('completed', 'skipped')), 0),
                    COALESCE(SUM(status IN ('source_missing', 'conflicted')), 0)
                FROM market_daily_run_items WHERE run_id = ?
            )
            WHERE run_id = ?
            """,
            (run_id, run_id),
        )

    def claim_item(
        self,
        run_id: str,
        code: str,
        owner_id: str,
        now: datetime,
        *,
        lease_seconds: int = 60,
    ) -> bool:
        """Atomically reserve one non-terminal item for a single worker."""

        run_id = _require_text(run_id, "运行编号", 128)
        owner_id = _require_text(owner_id, "服务实例", 128)
        try:
            exchange_for_code(code)
        except ValueError as exc:
            raise MarketRunError("证券代码必须属于沪深 A 股范围") from exc
        now = _require_time(now, "当前时间")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds <= 0:
            raise MarketRunError("租约秒数必须为正整数")
        expires_at = now + timedelta(seconds=lease_seconds)
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute("SELECT status FROM market_daily_runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                raise MarketRunError("运行不存在")
            if run["status"] != "running":
                raise MarketRunError("只有运行中的任务可以认领逐证券项目")
            item = connection.execute(
                "SELECT * FROM market_daily_run_items WHERE run_id = ? AND code = ?", (run_id, code)
            ).fetchone()
            if item is None:
                raise MarketRunError("证券不属于该运行的冻结股票池")
            if item["status"] in _TERMINAL_ITEM_STATES:
                connection.commit()
                return False
            expired = item["claim_expires_at"] is None or (_from_timestamp(item["claim_expires_at"]) or now) <= now
            if item["status"] == "running" and item["claimed_by"] not in (None, owner_id) and not expired:
                connection.commit()
                return False
            attempts = int(item["attempts"]) + (1 if item["status"] != "running" or expired else 0)
            connection.execute(
                """
                UPDATE market_daily_run_items
                SET status = 'running', attempts = ?, claimed_by = ?, claim_expires_at = ?,
                    updated_at = ?, completed_at = NULL
                WHERE run_id = ? AND code = ?
                """,
                (attempts, owner_id, expires_at.isoformat(), now.isoformat(), run_id, code),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def renew_item_claim(
        self,
        run_id: str,
        code: str,
        owner_id: str,
        now: datetime,
        *,
        lease_seconds: int = 60,
    ) -> bool:
        """Extend one running item only while its current owner still holds it."""

        run_id = _require_text(run_id, "运行编号", 128)
        owner_id = _require_text(owner_id, "服务实例", 128)
        try:
            exchange_for_code(code)
        except ValueError as exc:
            raise MarketRunError("证券代码必须属于沪深 A 股范围") from exc
        now = _require_time(now, "当前时间")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds <= 0:
            raise MarketRunError("租约秒数必须为正整数")
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                """
                SELECT status, claimed_by, claim_expires_at
                FROM market_daily_run_items WHERE run_id = ? AND code = ?
                """,
                (run_id, code),
            ).fetchone()
            expires_at = _from_timestamp(item["claim_expires_at"]) if item is not None else None
            if (
                item is None
                or item["status"] != "running"
                or item["claimed_by"] != owner_id
                or expires_at is None
                or expires_at <= now
            ):
                connection.commit()
                return False
            connection.execute(
                """
                UPDATE market_daily_run_items
                SET claim_expires_at = ?, updated_at = ?
                WHERE run_id = ? AND code = ? AND status = 'running' AND claimed_by = ?
                """,
                (
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    now.isoformat(),
                    run_id,
                    code,
                    owner_id,
                ),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def finalize_run(self, run_id: str, now: datetime) -> MarketDailyRun:
        run_id = _require_text(run_id, "运行编号", 128)
        now = _require_time(now, "当前时间")
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute("SELECT * FROM market_daily_runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                raise MarketRunError("运行不存在")
            if run["status"] != "running":
                raise MarketRunError("只有运行中的任务可以完成")
            counts = connection.execute(
                """
                SELECT
                  SUM(CASE WHEN status IN ('pending', 'running') THEN 1 ELSE 0 END) AS unfinished,
                  SUM(CASE WHEN status IN ('completed', 'skipped') THEN 1 ELSE 0 END) AS completed,
                  SUM(CASE WHEN status IN ('source_missing', 'conflicted') THEN 1 ELSE 0 END) AS failed
                FROM market_daily_run_items WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if int(counts["unfinished"] or 0) != 0:
                raise MarketRunError("仍有逐证券任务未完成")
            completed_items = int(counts["completed"] or 0)
            failed_items = int(counts["failed"] or 0)
            status = "partial" if failed_items else "complete"
            connection.execute(
                """
                UPDATE market_daily_runs
                SET status = ?, completed_items = ?, failed_items = ?, finished_at = ?
                WHERE run_id = ?
                """,
                (status, completed_items, failed_items, now.isoformat(), run_id),
            )
            connection.execute(
                """
                UPDATE market_daily_requests
                SET status = 'completed', completed_at = ?
                WHERE request_id = ?
                """,
                (now.isoformat(), run["request_id"]),
            )
            row = connection.execute("SELECT * FROM market_daily_runs WHERE run_id = ?", (run_id,)).fetchone()
            connection.commit()
            return self._run_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def recover_expired_work(self, now: datetime, *, lease_seconds: int = 60) -> int:
        """Requeue only work owned by an absent or expired service instance."""

        now = _require_time(now, "当前时间")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds <= 0:
            raise MarketRunError("租约秒数必须为正整数")
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            active_owners = {
                row["owner_id"]
                for row in connection.execute(
                    "SELECT owner_id, expires_at FROM market_daily_service_leases WHERE lease_name = ?",
                    (_LEASE_NAME,),
                ).fetchall()
                if (_from_timestamp(row["expires_at"]) or now) > now
            }
            stale_runs = connection.execute(
                """
                SELECT r.run_id, r.request_id, q.claimed_by
                FROM market_daily_runs AS r
                JOIN market_daily_requests AS q ON q.request_id = r.request_id
                WHERE r.status = 'running'
                """
            ).fetchall()
            recovered = 0
            for row in stale_runs:
                if row["claimed_by"] in active_owners:
                    continue
                connection.execute(
                    """
                    UPDATE market_daily_run_items
                    SET status = 'pending', claimed_by = NULL, claim_expires_at = NULL,
                        updated_at = ?, completed_at = NULL
                    WHERE run_id = ? AND status = 'running'
                    """,
                    (now.isoformat(), row["run_id"]),
                )
                connection.execute(
                    """
                    UPDATE market_daily_runs
                    SET status = 'pending', started_at = NULL
                    WHERE run_id = ?
                    """,
                    (row["run_id"],),
                )
                connection.execute(
                    """
                    UPDATE market_daily_requests
                    SET status = 'pending', claimed_by = NULL
                    WHERE request_id = ? AND status = 'claimed'
                    """,
                    (row["request_id"],),
                )
                recovered += 1
            stale_claims = connection.execute(
                """
                SELECT q.request_id, q.claimed_by
                FROM market_daily_requests AS q
                WHERE q.status = 'claimed'
                  AND NOT EXISTS (
                    SELECT 1 FROM market_daily_runs AS r WHERE r.request_id = q.request_id
                  )
                """
            ).fetchall()
            for row in stale_claims:
                if row["claimed_by"] in active_owners:
                    continue
                connection.execute(
                    """
                    UPDATE market_daily_requests
                    SET status = 'pending', claimed_by = NULL
                    WHERE request_id = ? AND status = 'claimed'
                    """,
                    (row["request_id"],),
                )
                recovered += 1
            connection.commit()
            return recovered
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def request(self, request_id: str) -> MarketDailyRequest:
        connection = connect(self.database_path)
        try:
            row = connection.execute(
                "SELECT * FROM market_daily_requests WHERE request_id = ?", (_require_text(request_id, "请求编号", 128),)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KeyError(request_id)
        return self._request_from_row(row)

    def run(self, run_id: str) -> MarketDailyRun:
        connection = connect(self.database_path)
        try:
            row = connection.execute(
                "SELECT * FROM market_daily_runs WHERE run_id = ?", (_require_text(run_id, "运行编号", 128),)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KeyError(run_id)
        return self._run_from_row(row)

    def run_for_request(self, request_id: str) -> MarketDailyRun | None:
        connection = connect(self.database_path)
        try:
            row = connection.execute(
                "SELECT * FROM market_daily_runs WHERE request_id = ? ORDER BY created_at LIMIT 1",
                (_require_text(request_id, "请求编号", 128),),
            ).fetchone()
        finally:
            connection.close()
        return self._run_from_row(row) if row is not None else None

    def latest_run(
        self,
        *,
        run_type: str | None = None,
        statuses: tuple[str, ...] | None = None,
    ) -> MarketDailyRun | None:
        if run_type is not None and run_type not in _REQUEST_TYPES:
            raise MarketRunError("运行类型无效")
        if statuses is not None:
            if not statuses or any(status not in _RUN_STATES for status in statuses):
                raise MarketRunError("运行状态无效")
        conditions: list[str] = []
        params: list[str] = []
        if run_type is not None:
            conditions.append("run_type = ?")
            params.append(run_type)
        if statuses is not None:
            conditions.append("status IN (%s)" % ", ".join("?" for _ in statuses))
            params.extend(statuses)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        connection = connect(self.database_path)
        try:
            row = connection.execute(
                f"SELECT * FROM market_daily_runs {where} ORDER BY created_at DESC, run_id DESC LIMIT 1",
                tuple(params),
            ).fetchone()
        finally:
            connection.close()
        return self._run_from_row(row) if row is not None else None

    def latest_run_for_target(self, target_session: date) -> MarketDailyRun | None:
        target_session = _require_date(target_session, "目标交易日")
        connection = connect(self.database_path)
        try:
            row = connection.execute(
                """
                SELECT * FROM market_daily_runs
                WHERE target_session = ?
                ORDER BY created_at DESC, run_id DESC
                LIMIT 1
                """,
                (target_session.isoformat(),),
            ).fetchone()
        finally:
            connection.close()
        return self._run_from_row(row) if row is not None else None

    def lease(self) -> tuple[str, datetime, datetime] | None:
        connection = connect(self.database_path)
        try:
            row = connection.execute(
                "SELECT owner_id, heartbeat_at, expires_at FROM market_daily_service_leases WHERE lease_name = ?",
                (_LEASE_NAME,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        heartbeat = _from_timestamp(row["heartbeat_at"])
        expires = _from_timestamp(row["expires_at"])
        if heartbeat is None or expires is None:
            return None
        return (row["owner_id"], heartbeat, expires)

    def item(self, run_id: str, code: str) -> MarketDailyRunItem:
        connection = connect(self.database_path)
        try:
            row = connection.execute(
                "SELECT * FROM market_daily_run_items WHERE run_id = ? AND code = ?",
                (_require_text(run_id, "运行编号", 128), code),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KeyError((run_id, code))
        return self._item_from_row(row)

    def run_securities(self, run_id: str) -> tuple[RunSecurity, ...]:
        connection = connect(self.database_path)
        try:
            rows = connection.execute(
                """
                SELECT code, name, exchange, list_date, delist_date, status
                FROM market_daily_run_securities WHERE run_id = ? ORDER BY code
                """,
                (_require_text(run_id, "运行编号", 128),),
            ).fetchall()
        finally:
            connection.close()
        return tuple(
            RunSecurity(
                code=row["code"],
                name=row["name"],
                exchange=row["exchange"],
                list_date=date.fromisoformat(row["list_date"]),
                delist_date=date.fromisoformat(row["delist_date"]) if row["delist_date"] else None,
                status=row["status"],
            )
            for row in rows
        )

    def pending_items(self, run_id: str) -> tuple[MarketDailyRunItem, ...]:
        connection = connect(self.database_path)
        try:
            rows = connection.execute(
                """
                SELECT * FROM market_daily_run_items
                WHERE run_id = ? AND status IN ('pending', 'running') ORDER BY code
                """,
                (_require_text(run_id, "运行编号", 128),),
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._item_from_row(row) for row in rows)

    @staticmethod
    def _request_from_row(row: sqlite3.Row) -> MarketDailyRequest:
        return MarketDailyRequest(
            request_id=row["request_id"],
            request_type=row["request_type"],
            target_session=date.fromisoformat(row["target_session"]) if row["target_session"] else None,
            start_date=date.fromisoformat(row["start_date"]) if row["start_date"] else None,
            end_date=date.fromisoformat(row["end_date"]) if row["end_date"] else None,
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            claimed_by=row["claimed_by"],
            claimed_at=_from_timestamp(row["claimed_at"]),
            completed_at=_from_timestamp(row["completed_at"]),
            message=row["message"],
        )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> MarketDailyRun:
        return MarketDailyRun(
            run_id=row["run_id"],
            request_id=row["request_id"],
            run_type=row["run_type"],
            status=row["status"],
            target_session=date.fromisoformat(row["target_session"]),
            start_date=date.fromisoformat(row["start_date"]),
            end_date=date.fromisoformat(row["end_date"]),
            universe_hash=row["universe_hash"],
            total_items=int(row["total_items"]),
            completed_items=int(row["completed_items"]),
            failed_items=int(row["failed_items"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            started_at=_from_timestamp(row["started_at"]),
            finished_at=_from_timestamp(row["finished_at"]),
            message=row["message"],
        )

    @staticmethod
    def _item_from_row(row: sqlite3.Row) -> MarketDailyRunItem:
        return MarketDailyRunItem(
            run_id=row["run_id"],
            code=row["code"],
            start_date=date.fromisoformat(row["start_date"]),
            end_date=date.fromisoformat(row["end_date"]),
            status=row["status"],
            attempts=int(row["attempts"]),
            selected_source=row["selected_source"],
            last_error=row["last_error"],
            claimed_by=row["claimed_by"],
            claim_expires_at=_from_timestamp(row["claim_expires_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            completed_at=_from_timestamp(row["completed_at"]),
        )
