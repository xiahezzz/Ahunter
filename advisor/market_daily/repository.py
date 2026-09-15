"""Small SQLite repository for immutable Market Daily facts."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from advisor.db.migrate import migrate_database
from advisor.db.repository import connect
from advisor.market_daily.contracts import (
    AdjustmentFactor,
    CanonicalDailyBar,
    MarketSecurity,
    MarketAbsence,
    ObservedTradingSession,
    SessionObservationReceipt,
)


class MarketDailyRepositoryConflict(RuntimeError):
    """A later source tried to rewrite an immutable market fact."""


@dataclass(frozen=True)
class SecurityCommitResult:
    inserted_bars: int
    unchanged_bars: int
    conflicted_bars: int
    inserted_absences: int
    unchanged_absences: int
    conflicted_absences: int
    inserted_factors: int
    updated_factors: int
    unchanged_factors: int


class MarketDailyRepository:
    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        migrate_database(self.database_path)

    def security_listing_dates(self) -> dict[str, date]:
        connection = connect(self.database_path)
        try:
            rows = connection.execute(
                """
                SELECT code, list_date FROM securities
                WHERE security_type = 'a_share' AND list_date IS NOT NULL
                """
            ).fetchall()
        finally:
            connection.close()
        return {row["code"]: date.fromisoformat(row["list_date"]) for row in rows}

    def upsert_security(self, security: MarketSecurity) -> str:
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT list_date, delist_date, status, source_content_hash
                FROM securities WHERE code = ?
                """,
                (security.code,),
            ).fetchone()
            if existing is not None and existing["source_content_hash"] == security.content_hash:
                connection.commit()
                return "unchanged"
            if existing is not None:
                old_list_date = existing["list_date"]
                old_delist_date = existing["delist_date"]
                if old_list_date and old_list_date != security.list_date.isoformat():
                    raise MarketDailyRepositoryConflict(
                        f"{security.code} 的上市日期与已保存事实冲突"
                    )
                if old_delist_date and security.delist_date and old_delist_date != security.delist_date.isoformat():
                    raise MarketDailyRepositoryConflict(
                        f"{security.code} 的退市日期与已保存事实冲突"
                    )
                # A current-list response must never erase an already observed
                # delisting interval.  Keep the historical fact intact.
                if old_delist_date and security.delist_date is None:
                    connection.commit()
                    return "retained"
            now = security.fetched_at.isoformat()
            connection.execute(
                """
                INSERT INTO securities (
                  code, name, exchange, board, industry, concepts_json,
                  security_type, list_date, delist_date, status, is_st, source,
                  source_at, source_content_hash, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, NULL, '[]', 'a_share', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                  name = excluded.name,
                  exchange = excluded.exchange,
                  security_type = excluded.security_type,
                  list_date = excluded.list_date,
                  delist_date = excluded.delist_date,
                  status = excluded.status,
                  is_st = excluded.is_st,
                  source = excluded.source,
                  source_at = excluded.source_at,
                  source_content_hash = excluded.source_content_hash,
                  updated_at = excluded.updated_at
                """,
                (
                    security.code,
                    security.name,
                    security.exchange,
                    security.list_date.isoformat(),
                    security.delist_date.isoformat() if security.delist_date else None,
                    security.status,
                    int(security.is_st),
                    security.source,
                    security.source_at.isoformat(),
                    security.content_hash,
                    now,
                    now,
                ),
            )
            connection.commit()
            return "inserted" if existing is None else "updated"
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def insert_bar(self, bar: CanonicalDailyBar) -> str:
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT content_hash FROM market_daily WHERE code = ? AND trade_date = ?",
                (bar.code, bar.trade_date.isoformat()),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return "unchanged" if existing[0] == bar.content_hash else "conflicted"
            connection.execute(
                """
                INSERT INTO market_daily (
                  code, trade_date, open, high, low, close, volume, amount,
                  source, source_at, fetched_at,
                  as_of_date, schema_version, content_hash, quality_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 2, ?, 'passed')
                """,
                (
                    bar.code,
                    bar.trade_date.isoformat(),
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                    bar.amount,
                    bar.source,
                    bar.source_at.isoformat(),
                    bar.fetched_at.isoformat(),
                    bar.as_of_date.isoformat(),
                    bar.content_hash,
                ),
            )
            connection.commit()
            return "inserted"
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_absence(self, absence: MarketAbsence) -> str:
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = self._insert_absence(connection, absence)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def commit_security_observations(
        self,
        bars: tuple[CanonicalDailyBar, ...],
        factors: tuple[AdjustmentFactor, ...],
        absences: tuple[MarketAbsence, ...],
    ) -> SecurityCommitResult:
        """Commit one security's facts in a short transaction without overwriting conflicts."""

        if not bars and not absences and not factors:
            raise ValueError("a security batch cannot be empty")
        codes = {fact.code for fact in (*bars, *factors, *absences)}
        if len(codes) != 1:
            raise ValueError("a security batch must contain exactly one code")
        bar_keys = [(bar.code, bar.trade_date) for bar in bars]
        absence_keys = [(absence.code, absence.trade_date) for absence in absences]
        if len(bar_keys) != len(set(bar_keys)) or len(absence_keys) != len(set(absence_keys)):
            raise ValueError("a security batch cannot contain duplicate dates")
        if set(bar_keys).intersection(absence_keys):
            raise ValueError("one date cannot be both traded and suspended")
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            bar_results = [self._insert_bar(connection, bar) for bar in bars]
            absence_results = [self._insert_absence(connection, absence) for absence in absences]
            factor_results = [self._upsert_factor(connection, factor) for factor in factors]
            connection.commit()
            return SecurityCommitResult(
                inserted_bars=bar_results.count("inserted"),
                unchanged_bars=bar_results.count("unchanged"),
                conflicted_bars=bar_results.count("conflicted"),
                inserted_absences=absence_results.count("inserted"),
                unchanged_absences=absence_results.count("unchanged"),
                conflicted_absences=absence_results.count("conflicted"),
                inserted_factors=factor_results.count("inserted"),
                updated_factors=factor_results.count("updated"),
                unchanged_factors=factor_results.count("unchanged"),
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def covered_dates(self, code: str, start: date, end: date) -> tuple[date, ...]:
        connection = connect(self.database_path)
        try:
            bars = connection.execute(
                """
                SELECT trade_date FROM market_daily
                WHERE code = ? AND trade_date BETWEEN ? AND ? AND quality_status = 'passed'
                """,
                (code, start.isoformat(), end.isoformat()),
            ).fetchall()
            absences = connection.execute(
                """
                SELECT trade_date FROM market_daily_absences
                WHERE code = ? AND trade_date BETWEEN ? AND ?
                """,
                (code, start.isoformat(), end.isoformat()),
            ).fetchall()
        finally:
            connection.close()
        return tuple(sorted({date.fromisoformat(row["trade_date"]) for row in (*bars, *absences)}))

    def bars_for(self, code: str, start: date, end: date) -> tuple[CanonicalDailyBar, ...]:
        connection = connect(self.database_path)
        try:
            rows = connection.execute(
                """
                SELECT code, trade_date, open, high, low, close, volume, amount,
                       source, source_at, fetched_at, as_of_date
                FROM market_daily
                WHERE code = ? AND trade_date BETWEEN ? AND ? AND quality_status = 'passed'
                ORDER BY trade_date
                """,
                (code, start.isoformat(), end.isoformat()),
            ).fetchall()
        finally:
            connection.close()
        return tuple(
            CanonicalDailyBar(
                code=row["code"],
                trade_date=date.fromisoformat(row["trade_date"]),
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=int(row["volume"]),
                amount=row["amount"],
                source=row["source"],
                source_at=datetime.fromisoformat(row["source_at"]),
                fetched_at=datetime.fromisoformat(row["fetched_at"]),
                as_of_date=date.fromisoformat(row["as_of_date"]),
            )
            for row in rows
        )

    def factor_dates(self, code: str, start: date, end: date) -> tuple[date, ...]:
        connection = connect(self.database_path)
        try:
            rows = connection.execute(
                """
                SELECT trade_date FROM market_adjustment_factors
                WHERE code = ? AND trade_date BETWEEN ? AND ? ORDER BY trade_date
                """,
                (code, start.isoformat(), end.isoformat()),
            ).fetchall()
        finally:
            connection.close()
        return tuple(date.fromisoformat(row["trade_date"]) for row in rows)

    def repair_bar(self, bar: CanonicalDailyBar, reason: str) -> str:
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 512:
            raise ValueError("repair reason must be non-empty")
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT content_hash FROM market_daily WHERE code = ? AND trade_date = ?",
                (bar.code, bar.trade_date.isoformat()),
            ).fetchone()
            if existing is None:
                raise KeyError((bar.code, bar.trade_date))
            if existing["content_hash"] == bar.content_hash:
                connection.commit()
                return "unchanged"
            connection.execute(
                """
                UPDATE market_daily
                SET open = ?, high = ?, low = ?, close = ?, volume = ?, amount = ?,
                    source = ?, source_at = ?, fetched_at = ?, as_of_date = ?,
                    schema_version = 2, content_hash = ?, quality_status = 'passed'
                WHERE code = ? AND trade_date = ?
                """,
                (
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                    bar.amount,
                    bar.source,
                    bar.source_at.isoformat(),
                    bar.fetched_at.isoformat(),
                    bar.as_of_date.isoformat(),
                    bar.content_hash,
                    bar.code,
                    bar.trade_date.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO market_daily_repairs (
                  repair_id, code, trade_date, previous_content_hash,
                  replacement_content_hash, reason, repaired_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"mdrepair-{uuid.uuid4().hex}",
                    bar.code,
                    bar.trade_date.isoformat(),
                    existing["content_hash"],
                    bar.content_hash,
                    reason.strip(),
                    bar.fetched_at.isoformat(),
                ),
            )
            connection.commit()
            return "repaired"
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def bar_for(self, code: str, trade_date: date) -> CanonicalDailyBar:
        connection = connect(self.database_path)
        try:
            row = connection.execute(
                """
                SELECT code, trade_date, open, high, low, close, volume, amount,
                       source, source_at, fetched_at, as_of_date
                FROM market_daily WHERE code = ? AND trade_date = ?
                """,
                (code, trade_date.isoformat()),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KeyError((code, trade_date))
        return CanonicalDailyBar(
            code=row["code"],
            trade_date=date.fromisoformat(row["trade_date"]),
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=int(row["volume"]),
            amount=row["amount"],
            source=row["source"],
            source_at=datetime.fromisoformat(row["source_at"]),
            fetched_at=datetime.fromisoformat(row["fetched_at"]),
            as_of_date=date.fromisoformat(row["as_of_date"]),
        )

    def upsert_factor(self, factor: AdjustmentFactor) -> str:
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = self._upsert_factor(connection, factor)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _insert_bar(connection: sqlite3.Connection, bar: CanonicalDailyBar) -> str:
        existing = connection.execute(
            "SELECT content_hash FROM market_daily WHERE code = ? AND trade_date = ?",
            (bar.code, bar.trade_date.isoformat()),
        ).fetchone()
        if existing is not None:
            return "unchanged" if existing["content_hash"] == bar.content_hash else "conflicted"
        connection.execute(
            """
            INSERT INTO market_daily (
              code, trade_date, open, high, low, close, volume, amount,
              source, source_at, fetched_at,
              as_of_date, schema_version, content_hash, quality_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 2, ?, 'passed')
            """,
            (
                bar.code,
                bar.trade_date.isoformat(),
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.amount,
                bar.source,
                bar.source_at.isoformat(),
                bar.fetched_at.isoformat(),
                bar.as_of_date.isoformat(),
                bar.content_hash,
            ),
        )
        return "inserted"

    @staticmethod
    def _insert_absence(connection: sqlite3.Connection, absence: MarketAbsence) -> str:
        existing = connection.execute(
            "SELECT content_hash FROM market_daily_absences WHERE code = ? AND trade_date = ?",
            (absence.code, absence.trade_date.isoformat()),
        ).fetchone()
        if existing is not None:
            return "unchanged" if existing["content_hash"] == absence.content_hash else "conflicted"
        connection.execute(
            """
            INSERT INTO market_daily_absences (
              code, trade_date, reason, source, source_at, fetched_at, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                absence.code,
                absence.trade_date.isoformat(),
                absence.reason,
                absence.source,
                absence.source_at.isoformat(),
                absence.fetched_at.isoformat(),
                absence.content_hash,
            ),
        )
        return "inserted"

    @staticmethod
    def _upsert_factor(connection: sqlite3.Connection, factor: AdjustmentFactor) -> str:
        existing = connection.execute(
            "SELECT content_hash FROM market_adjustment_factors WHERE code = ? AND trade_date = ?",
            (factor.code, factor.trade_date.isoformat()),
        ).fetchone()
        if existing is not None and existing["content_hash"] == factor.content_hash:
            return "unchanged"
        connection.execute(
            """
            INSERT INTO market_adjustment_factors (
              code, trade_date, factor, source, source_at, fetched_at,
              algorithm_version, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code, trade_date) DO UPDATE SET
              factor = excluded.factor,
              source = excluded.source,
              source_at = excluded.source_at,
              fetched_at = excluded.fetched_at,
              algorithm_version = excluded.algorithm_version,
              content_hash = excluded.content_hash
            """,
            (
                factor.code,
                factor.trade_date.isoformat(),
                factor.factor,
                factor.source,
                factor.source_at.isoformat(),
                factor.fetched_at.isoformat(),
                factor.algorithm_version,
                factor.content_hash,
            ),
        )
        return "inserted" if existing is None else "updated"

    def factor_for(self, code: str, trade_date: date) -> AdjustmentFactor:
        connection = connect(self.database_path)
        try:
            row = connection.execute(
                """
                SELECT code, trade_date, factor, source, source_at, fetched_at, algorithm_version
                FROM market_adjustment_factors WHERE code = ? AND trade_date = ?
                """,
                (code, trade_date.isoformat()),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KeyError((code, trade_date))
        return AdjustmentFactor(
            code=row["code"],
            trade_date=date.fromisoformat(row["trade_date"]),
            factor=row["factor"],
            source=row["source"],
            source_at=datetime.fromisoformat(row["source_at"]),
            fetched_at=datetime.fromisoformat(row["fetched_at"]),
            algorithm_version=row["algorithm_version"],
        )

    def upsert_session(self, session: ObservedTradingSession) -> str:
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM trading_sessions WHERE trade_date = ?", (session.trade_date.isoformat(),)
            ).fetchone()
            if existing is not None:
                same_sources = (
                    existing["primary_source"] == session.primary_source
                    and existing["fallback_source"] == session.fallback_source
                )
                if not same_sources:
                    raise MarketDailyRepositoryConflict(
                        f"{session.trade_date.isoformat()} 的交易日来源冲突"
                    )
                connection.commit()
                return "unchanged"
            connection.execute(
                """
                INSERT INTO trading_sessions (
                  trade_date, primary_source, fallback_source, primary_observed_at,
                  fallback_observed_at, fetched_at, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.trade_date.isoformat(),
                    session.primary_source,
                    session.fallback_source,
                    session.primary_observed_at.isoformat(),
                    session.fallback_observed_at.isoformat(),
                    session.fetched_at.isoformat(),
                    session.content_hash,
                ),
            )
            connection.commit()
            return "inserted"
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def sessions_for(self, start: date, end: date) -> tuple[date, ...]:
        if start > end:
            raise ValueError("start cannot be after end")
        connection = connect(self.database_path)
        try:
            rows = connection.execute(
                """
                SELECT trade_date FROM trading_sessions
                WHERE trade_date BETWEEN ? AND ?
                ORDER BY trade_date
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        finally:
            connection.close()
        return tuple(date.fromisoformat(row["trade_date"]) for row in rows)

    def latest_session_on_or_before(self, end: date) -> date | None:
        connection = connect(self.database_path)
        try:
            row = connection.execute(
                "SELECT MAX(trade_date) AS trade_date FROM trading_sessions WHERE trade_date <= ?",
                (end.isoformat(),),
            ).fetchone()
        finally:
            connection.close()
        return date.fromisoformat(row["trade_date"]) if row and row["trade_date"] else None

    def record_session_observation(self, receipt: SessionObservationReceipt) -> str:
        """Persist one dual-source session observation without rewriting history."""
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT content_hash FROM trading_session_observations WHERE content_hash = ?",
                (receipt.content_hash,),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return "unchanged"
            connection.execute(
                """
                INSERT INTO trading_session_observations (
                  observation_id, observed_at, primary_source, fallback_source, latest_session,
                  session_set_hash, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.content_hash,
                    receipt.observed_at.isoformat(),
                    receipt.primary_source,
                    receipt.fallback_source,
                    receipt.latest_session.isoformat() if receipt.latest_session else None,
                    receipt.session_set_hash,
                    receipt.content_hash,
                ),
            )
            connection.commit()
            return "inserted"
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def latest_session_observation_on_or_before(
        self, as_of: datetime
    ) -> SessionObservationReceipt | None:
        connection = connect(self.database_path)
        try:
            row = connection.execute(
                """
                SELECT observed_at, primary_source, fallback_source, latest_session, session_set_hash, content_hash
                FROM trading_session_observations
                WHERE julianday(observed_at) <= julianday(?)
                ORDER BY julianday(observed_at) DESC, rowid DESC
                LIMIT 1
                """,
                (as_of.isoformat(),),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        receipt = SessionObservationReceipt(
            observed_at=datetime.fromisoformat(row["observed_at"]),
            primary_source=row["primary_source"],
            fallback_source=row["fallback_source"],
            latest_session=date.fromisoformat(row["latest_session"]) if row["latest_session"] else None,
            session_set_hash=row["session_set_hash"],
        )
        if receipt.content_hash != row["content_hash"]:
            raise MarketDailyRepositoryConflict("交易日观测记录完整性冲突")
        return receipt

    def session_set_hash(self, start: date, end: date) -> str:
        import hashlib

        connection = connect(self.database_path)
        try:
            rows = connection.execute(
                """
                SELECT trade_date, content_hash FROM trading_sessions
                WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        finally:
            connection.close()
        payload = "\n".join(f"{row['trade_date']}:{row['content_hash']}" for row in rows)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def factor_set_hash(self, code: str, start: date, end: date) -> str:
        import hashlib

        connection = connect(self.database_path)
        try:
            rows = connection.execute(
                """
                SELECT trade_date, content_hash FROM market_adjustment_factors
                WHERE code = ? AND trade_date BETWEEN ? AND ? ORDER BY trade_date
                """,
                (code, start.isoformat(), end.isoformat()),
            ).fetchall()
        finally:
            connection.close()
        payload = "\n".join(f"{row['trade_date']}:{row['content_hash']}" for row in rows)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
