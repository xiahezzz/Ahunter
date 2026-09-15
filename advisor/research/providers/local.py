"""Read-only, scope-aware Market Daily products for the Research Engine."""

from __future__ import annotations

import gzip
import hashlib
import os
import sqlite3
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from advisor.market_daily.adjustments import AdjustmentError, derive_forward_adjusted_prices
from advisor.market_daily.contracts import AdjustmentFactor, CanonicalDailyBar, MarketAbsence, MarketContractError
from advisor.research.contracts import canonical_json
from advisor.research.data_products.engine import ProductRequest, ProviderObservation, QueryBacking
from advisor.research.market_time import a_share_date


class LocalMarketProvider:
    """Never writes or fetches network data while building a research Snapshot."""

    provider_id = "local-market"

    def __init__(
        self,
        database_path: Path | str,
        *,
        stream_threshold_rows: int = 50_000,
        staging_dir: Path | str | None = None,
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        if type(stream_threshold_rows) is not int or stream_threshold_rows < 0:
            raise ValueError("stream_threshold_rows must be a non-negative integer")
        self.stream_threshold_rows = stream_threshold_rows
        self.staging_dir = Path(staging_dir).expanduser().resolve() if staging_dir is not None else None
        if self.staging_dir is not None and (
            not self.staging_dir.is_dir() or self.staging_dir.is_symlink()
        ):
            raise ValueError("staging_dir must be an existing regular directory")

    def _new_query_writer(self) -> _NdjsonBackingWriter:
        if self.staging_dir is None:
            raise ValueError("全市场历史需要 Artifact Store 管理的 staging 目录")
        return _NdjsonBackingWriter(self.staging_dir)

    def preflight(self) -> str:
        if not self.database_path.is_file() or self.database_path.is_symlink():
            raise RuntimeError("本地 Market Daily 数据库不可用")
        connection = sqlite3.connect(f"file:{self.database_path}?mode=ro", uri=True)
        try:
            connection.execute("SELECT 1 FROM market_daily LIMIT 1").fetchone()
            connection.execute("SELECT 1 FROM market_adjustment_factors LIMIT 1").fetchone()
            connection.execute("SELECT 1 FROM trading_sessions LIMIT 1").fetchone()
        finally:
            connection.close()
        return str(self.database_path)

    def fetch(self, request: ProductRequest, *, dependencies: dict[str, Any] | None = None) -> ProviderObservation:
        if request.product.id == "whole_market_daily_history":
            try:
                payload, fetched_at, query_backing = self._read_whole_market_history(request)
            except (OSError, sqlite3.Error, ValueError, MarketContractError, AdjustmentError) as error:
                return _warning(request, str(error) or type(error).__name__)
            return ProviderObservation(
                provider=self.provider_id,
                payload=payload,
                observed_at=request.boundary.as_of,
                source_locator=f"sqlite://{self.database_path.name}/market_daily",
                fetched_at=fetched_at,
                schema_version="whole-market-daily-history@1",
                query_backing=query_backing,
            )
        if request.product.id != "market_daily_bars":
            raise ValueError(f"local-market does not provide {request.product}")
        try:
            payload, fetched_at = self._read_complete_security_window(request)
        except (OSError, sqlite3.Error, ValueError, MarketContractError, AdjustmentError) as error:
            return _warning(request, str(error) or type(error).__name__)
        return ProviderObservation(
            provider=self.provider_id,
            payload=payload,
            observed_at=request.boundary.as_of,
            source_locator=f"sqlite://{self.database_path.name}/market_daily",
            fetched_at=fetched_at,
            schema_version="market-daily-research@2",
        )

    def _read_whole_market_history(
        self,
        request: ProductRequest,
    ) -> tuple[dict[str, object], datetime, QueryBacking | None]:
        if not request.subject.is_market:
            raise ValueError("全市场历史只接受 Market Subject")
        boundary = request.boundary.as_of
        if boundary.tzinfo is None or boundary.utcoffset() is None:
            raise ValueError("研究边界时间必须带时区")
        connection = sqlite3.connect(f"file:{self.database_path}?mode=ro", uri=True)
        writer: _NdjsonBackingWriter | None = None
        try:
            visible_sessions = _visible_sessions(connection, date(1900, 1, 1), boundary)
            if not visible_sessions:
                raise ValueError("没有对研究边界可见的交易日")
            latest_run_id, baseline_run_id, history_start = _require_complete_market_run(
                connection,
                target_session=visible_sessions[-1][0],
                boundary=boundary,
            )
            sessions = tuple(item for item in visible_sessions if item[0] >= history_start)
            if not sessions:
                raise ValueError("全市场运行事实覆盖不完整：基线窗口没有可见交易日")
            history_end = sessions[-1][0]
            securities_rows = connection.execute(
                """
                SELECT code, name, list_date, delist_date
                FROM securities
                WHERE security_type = 'a_share' AND list_date IS NOT NULL AND list_date <= ?
                  AND (delist_date IS NULL OR delist_date >= ?)
                ORDER BY code
                """,
                (history_end.isoformat(), history_start.isoformat()),
            ).fetchall()
            if not securities_rows:
                raise ValueError("全市场历史窗口没有可见的 A 股证券池")
            securities = {
                str(row[0]): {
                    "code": str(row[0]),
                    "name": str(row[1]) if row[1] is not None else "",
                    "list_date": str(row[2]),
                    "delist_date": str(row[3]) if row[3] is not None else None,
                }
                for row in securities_rows
            }
            rows = connection.execute(
                """
                SELECT m.code, m.trade_date, m.open, m.high, m.low, m.close, m.volume, m.amount,
                       m.source, m.source_at, m.fetched_at, m.as_of_date, m.content_hash
                FROM market_daily AS m
                JOIN securities AS s ON s.code = m.code
                JOIN trading_sessions AS t ON t.trade_date = m.trade_date
                  AND julianday(t.fetched_at) <= julianday(?)
                WHERE m.quality_status = 'passed'
                  AND s.security_type = 'a_share'
                  AND s.list_date IS NOT NULL
                  AND m.trade_date >= s.list_date
                  AND (s.delist_date IS NULL OR m.trade_date <= s.delist_date)
                  AND m.trade_date BETWEEN ? AND ?
                  AND julianday(m.source_at) <= julianday(?)
                  AND julianday(m.fetched_at) <= julianday(?)
                ORDER BY m.trade_date, m.code
                """,
                (
                    boundary.isoformat(), history_start.isoformat(), history_end.isoformat(),
                    boundary.isoformat(), boundary.isoformat(),
                ),
            )
            bars: list[dict[str, object]] = []
            row_count = 0
            amount_available_count = 0
            bar_set = _OrderedSetHash()
            latest_fetched = max(item[2] for item in sessions)
            for row in rows:
                code = str(row[0])
                if code not in securities:
                    continue
                source_at = _timestamp(row[9], "日线来源时间")
                fetched_at = _timestamp(row[10], "日线抓取时间")
                if source_at > boundary or fetched_at > boundary:
                    continue
                bar = CanonicalDailyBar(
                    code=code,
                    trade_date=date.fromisoformat(str(row[1])),
                    open=row[2], high=row[3], low=row[4], close=row[5], volume=int(row[6]), amount=row[7],
                    source=str(row[8]), source_at=source_at, fetched_at=fetched_at,
                    as_of_date=date.fromisoformat(str(row[11])),
                )
                if bar.content_hash != row[12]:
                    raise ValueError("全市场原始日线完整性哈希不匹配")
                raw_row = _raw_row(bar)
                row_count += 1
                amount_available_count += bar.amount is not None
                bar_set.add(f"{bar.code}:{bar.trade_date.isoformat()}", bar.content_hash)
                latest_fetched = max(latest_fetched, fetched_at)
                if writer is None and len(bars) < self.stream_threshold_rows:
                    bars.append(raw_row)
                else:
                    if writer is None:
                        writer = self._new_query_writer()
                        for buffered in bars:
                            writer.write("bar", buffered)
                        bars.clear()
                    writer.write("bar", raw_row)
            absence_rows = connection.execute(
                """
                SELECT a.code, a.trade_date, a.reason, a.source, a.source_at, a.fetched_at, a.content_hash
                FROM market_daily_absences AS a
                JOIN securities AS s ON s.code = a.code
                JOIN trading_sessions AS t ON t.trade_date = a.trade_date
                  AND julianday(t.fetched_at) <= julianday(?)
                WHERE s.security_type = 'a_share'
                  AND s.list_date IS NOT NULL
                  AND a.trade_date >= s.list_date
                  AND (s.delist_date IS NULL OR a.trade_date <= s.delist_date)
                  AND a.trade_date BETWEEN ? AND ?
                  AND julianday(a.source_at) <= julianday(?)
                  AND julianday(a.fetched_at) <= julianday(?)
                ORDER BY a.trade_date, a.code
                """,
                (
                    boundary.isoformat(), history_start.isoformat(), history_end.isoformat(),
                    boundary.isoformat(), boundary.isoformat(),
                ),
            )
            absences: list[dict[str, object]] = []
            absence_count = 0
            absence_set = _OrderedSetHash()
            for row in absence_rows:
                code = str(row[0])
                if code not in securities:
                    continue
                source_at = _timestamp(row[4], "停牌来源时间")
                fetched_at = _timestamp(row[5], "停牌抓取时间")
                if source_at > boundary or fetched_at > boundary:
                    continue
                absence = MarketAbsence(code, date.fromisoformat(str(row[1])), str(row[2]), str(row[3]), source_at, fetched_at)
                if absence.content_hash != row[6]:
                    raise ValueError("全市场停牌事实完整性哈希不匹配")
                absence_row: dict[str, object] = {
                    "code": code, "trade_date": absence.trade_date.isoformat(), "reason": absence.reason,
                    "source": absence.source, "source_at": source_at.isoformat(), "fetched_at": fetched_at.isoformat(),
                    "content_hash": absence.content_hash,
                }
                absence_count += 1
                absence_set.add(f"{code}:{absence.trade_date.isoformat()}", absence.content_hash)
                latest_fetched = max(latest_fetched, fetched_at)
                if writer is None and len(bars) + len(absences) < self.stream_threshold_rows:
                    absences.append(absence_row)
                else:
                    if writer is None:
                        writer = self._new_query_writer()
                        for buffered in bars:
                            writer.write("bar", buffered)
                        bars.clear()
                        for buffered in absences:
                            writer.write("absence", buffered)
                        absences.clear()
                    writer.write("absence", absence_row)
            security_payloads = [securities[code] for code in sorted(securities)]
            session_payloads = [
                {"trade_date": item[0].isoformat(), "content_hash": item[1]}
                for item in sessions
            ]
            if writer is not None:
                for security_payload in security_payloads:
                    writer.write("security", security_payload)
                for session_payload in session_payloads:
                    writer.write("session", session_payload)
            payload: dict[str, object] = {
                "status": "passed",
                "rows": bars,
                "absences": absences,
                "row_count": row_count,
                "field_coverage": {
                    "amount": {"numeric_count": amount_available_count,
                               "missing_count": row_count - amount_available_count},
                },
                "absence_count": absence_count,
                "securities": security_payloads,
                "sessions": session_payloads,
                "snapshot_proof": {
                    "bar_set_hash": bar_set.hexdigest(),
                    "absence_set_hash": absence_set.hexdigest(),
                    "baseline_run_id": baseline_run_id,
                    "latest_run_id": latest_run_id,
                    "security_count": len(securities),
                    "session_set_hash": _set_hash((item[0].isoformat(), item[1]) for item in sessions),
                    "as_of": boundary.isoformat(),
                },
            }
            backing = QueryBacking(writer.finish()) if writer is not None else None
            return payload, latest_fetched, backing
        except Exception:
            if writer is not None:
                writer.abort()
            raise
        finally:
            connection.close()

    def _read_complete_security_window(self, request: ProductRequest) -> tuple[dict[str, object], datetime]:
        boundary = request.boundary.as_of
        if boundary.tzinfo is None or boundary.utcoffset() is None:
            raise ValueError("研究边界时间必须带时区")
        connection = sqlite3.connect(f"file:{self.database_path}?mode=ro", uri=True)
        try:
            baseline = connection.execute(
                """
                SELECT start_date FROM market_daily_runs
                WHERE run_type = 'cold_start' AND status IN ('running', 'partial', 'complete')
                ORDER BY created_at DESC, run_id DESC LIMIT 1
                """
            ).fetchone()
            if baseline is None:
                raise ValueError("没有可用于研究的 Market Daily 冷启动基线")
            start = date.fromisoformat(baseline[0])
            security = connection.execute(
                "SELECT list_date, delist_date FROM securities WHERE code = ?", (request.subject.code,)
            ).fetchone()
            if security is None or not security[0]:
                raise ValueError("证券主数据或上市日期不可用")
            list_date = date.fromisoformat(security[0])
            delist_date = date.fromisoformat(security[1]) if security[1] else None
            sessions = _visible_sessions(connection, max(start, list_date), boundary)
            if not sessions:
                raise ValueError("没有对研究边界可见的已证明交易日")
            target = sessions[-1][0]
            if delist_date is not None:
                sessions = tuple(item for item in sessions if item[0] <= delist_date)
            if not sessions:
                raise ValueError("该证券在研究窗口内没有应覆盖交易日")
            expected_dates = tuple(item[0] for item in sessions)
            raw_bars = _visible_bars(connection, request.subject.code, expected_dates[0], expected_dates[-1], boundary)
            absences = _visible_absences(connection, request.subject.code, expected_dates[0], expected_dates[-1], boundary)
            bar_dates = {bar.trade_date for bar in raw_bars}
            absence_dates = {absence.trade_date for absence in absences}
            if bar_dates.intersection(absence_dates):
                raise ValueError("本地日线与停牌事实冲突")
            missing = tuple(item for item in expected_dates if item not in bar_dates and item not in absence_dates)
            if missing:
                raise ValueError(f"单证券交易日覆盖不完整，缺少 {len(missing)} 天")
            if not raw_bars:
                raise ValueError("该证券只有停牌事实，无法形成研究价格序列")
            factors = _visible_factors(connection, request.subject.code, raw_bars, boundary)
            series = derive_forward_adjusted_prices(raw_bars, factors)
        finally:
            connection.close()

        raw_rows = [_raw_row(item) for item in raw_bars]
        research_rows = [_research_row(item) for item in series.bars]
        session_hash = _set_hash((item[0].isoformat(), item[1]) for item in sessions)
        raw_set_hash = _set_hash((item.trade_date.isoformat(), item.content_hash) for item in raw_bars)
        latest_fetch = max(
            [item.fetched_at for item in raw_bars]
            + [item.fetched_at for item in factors]
            + [item[2] for item in sessions],
        )
        return (
            {
                "status": "passed",
                # `rows` remains the actual, unadjusted price view used by
                # reports and charts.  New consumers should name it raw_rows.
                "rows": raw_rows,
                "raw_rows": raw_rows,
                "research_price_series": {
                    "algorithm_version": series.algorithm_version,
                    "content_hash": series.content_hash,
                    "factor_set_hash": series.factor_set_hash,
                    "bars": research_rows,
                },
                "observed_sessions": [
                    {"trade_date": item[0].isoformat(), "content_hash": item[1]}
                    for item in sessions
                ],
                "snapshot_proof": {
                    "latest_observed_session": target.isoformat(),
                    "session_set_hash": session_hash,
                    "raw_set_hash": raw_set_hash,
                    "factor_set_hash": series.factor_set_hash,
                },
            },
            latest_fetch,
        )


def _visible_sessions(
    connection: sqlite3.Connection, start: date, boundary: datetime
) -> tuple[tuple[date, str, datetime], ...]:
    rows = connection.execute(
        """
        SELECT trade_date, content_hash, fetched_at FROM trading_sessions
        WHERE trade_date >= ? AND trade_date <= ? ORDER BY trade_date
        """,
        (start.isoformat(), a_share_date(boundary).isoformat()),
    ).fetchall()
    visible: list[tuple[date, str, datetime]] = []
    for trade_date_raw, content_hash, fetched_at_raw in rows:
        fetched_at = _timestamp(fetched_at_raw, "交易日观察时间")
        trade_date = date.fromisoformat(trade_date_raw)
        if fetched_at <= boundary:
            visible.append((trade_date, content_hash, fetched_at))
    return tuple(visible)


def _require_complete_market_run(
    connection: sqlite3.Connection,
    *,
    target_session: date,
    boundary: datetime,
) -> tuple[str, str, date]:
    row = connection.execute(
        """
        SELECT run_id, status, total_items, completed_items, failed_items, finished_at
        FROM market_daily_runs
        WHERE target_session = ? AND run_type IN ('cold_start', 'catch_up')
          AND julianday(created_at) <= julianday(?)
        ORDER BY created_at DESC, run_id DESC LIMIT 1
        """,
        (target_session.isoformat(), boundary.isoformat()),
    ).fetchone()
    if row is None:
        raise ValueError("全市场运行尚未完整：目标交易日没有可见运行")
    finished_at = _timestamp(row[5], "全市场运行完成时间") if row[5] else None
    if (
        row[1] != "complete"
        or int(row[2]) != int(row[3])
        or int(row[4]) != 0
        or finished_at is None
        or finished_at > boundary
    ):
        raise ValueError("全市场运行尚未完整")
    baseline = connection.execute(
        """
        SELECT run_id, start_date, total_items, completed_items, failed_items, finished_at
        FROM market_daily_runs
        WHERE run_type = 'cold_start' AND status = 'complete'
          AND start_date <= ? AND julianday(created_at) <= julianday(?)
        ORDER BY created_at DESC, run_id DESC LIMIT 1
        """,
        (target_session.isoformat(), boundary.isoformat()),
    ).fetchone()
    baseline_finished = _timestamp(baseline[5], "全市场基线完成时间") if baseline is not None and baseline[5] else None
    if (
        baseline is None
        or int(baseline[2]) != int(baseline[3])
        or int(baseline[4]) != 0
        or baseline_finished is None
        or baseline_finished > boundary
    ):
        raise ValueError("全市场运行尚未完整：没有完整冷启动基线")
    history_start = date.fromisoformat(str(baseline[1]))
    proof = connection.execute(
        """
        WITH expected AS (
          SELECT s.code, t.trade_date
          FROM securities AS s
          JOIN trading_sessions AS t
            ON t.trade_date BETWEEN ? AND ?
           AND t.trade_date >= s.list_date
           AND (s.delist_date IS NULL OR t.trade_date <= s.delist_date)
          WHERE s.security_type = 'a_share' AND s.list_date IS NOT NULL
            AND s.list_date <= ? AND (s.delist_date IS NULL OR s.delist_date >= ?)
            AND julianday(t.fetched_at) <= julianday(?)
        )
        SELECT COUNT(*) AS expected_count,
               SUM(CASE WHEN b.code IS NULL AND a.code IS NULL THEN 1 ELSE 0 END) AS missing_count,
               SUM(CASE WHEN b.code IS NOT NULL AND a.code IS NOT NULL THEN 1 ELSE 0 END) AS conflict_count
        FROM expected AS e
        LEFT JOIN market_daily AS b
          ON b.code = e.code AND b.trade_date = e.trade_date AND b.quality_status = 'passed'
         AND julianday(b.source_at) <= julianday(?) AND julianday(b.fetched_at) <= julianday(?)
        LEFT JOIN market_daily_absences AS a
          ON a.code = e.code AND a.trade_date = e.trade_date
         AND julianday(a.source_at) <= julianday(?) AND julianday(a.fetched_at) <= julianday(?)
        """,
        (
            history_start.isoformat(), target_session.isoformat(),
            target_session.isoformat(), history_start.isoformat(), boundary.isoformat(),
            boundary.isoformat(), boundary.isoformat(), boundary.isoformat(), boundary.isoformat(),
        ),
    ).fetchone()
    if (
        proof is None
        or int(proof[0] or 0) <= 0
        or int(proof[1] or 0) != 0
        or int(proof[2] or 0) != 0
    ):
        raise ValueError("全市场运行事实覆盖不完整")
    extra = connection.execute(
        """
        WITH actual AS (
          SELECT code, trade_date, source_at, fetched_at
          FROM market_daily WHERE quality_status = 'passed'
          UNION ALL
          SELECT code, trade_date, source_at, fetched_at
          FROM market_daily_absences
        )
        SELECT 1
        FROM actual AS f
        JOIN securities AS s ON s.code = f.code
        WHERE s.security_type = 'a_share' AND s.list_date IS NOT NULL
          AND s.list_date <= ? AND (s.delist_date IS NULL OR s.delist_date >= ?)
          AND f.trade_date BETWEEN ? AND ?
          AND julianday(f.source_at) <= julianday(?)
          AND julianday(f.fetched_at) <= julianday(?)
          AND NOT EXISTS (
            SELECT 1 FROM trading_sessions AS t
            WHERE t.trade_date = f.trade_date
              AND julianday(t.fetched_at) <= julianday(?)
              AND f.trade_date >= s.list_date
              AND (s.delist_date IS NULL OR f.trade_date <= s.delist_date)
          )
        LIMIT 1
        """,
        (
            target_session.isoformat(), history_start.isoformat(),
            history_start.isoformat(), target_session.isoformat(),
            boundary.isoformat(), boundary.isoformat(), boundary.isoformat(),
        ),
    ).fetchone()
    if extra is not None:
        raise ValueError("全市场运行事实集合不精确")
    return str(row[0]), str(baseline[0]), history_start


def _visible_bars(
    connection: sqlite3.Connection,
    code: str,
    start: date,
    end: date,
    boundary: datetime,
) -> tuple[CanonicalDailyBar, ...]:
    rows = connection.execute(
        """
        SELECT code, trade_date, open, high, low, close, volume, amount,
               source, source_at, fetched_at, as_of_date, content_hash
        FROM market_daily
        WHERE code = ? AND trade_date BETWEEN ? AND ? AND quality_status = 'passed'
        ORDER BY trade_date
        """,
        (code, start.isoformat(), end.isoformat()),
    ).fetchall()
    bars: list[CanonicalDailyBar] = []
    for row in rows:
        source_at = _timestamp(row[9], "日线来源时间")
        fetched_at = _timestamp(row[10], "日线抓取时间")
        if source_at > boundary or fetched_at > boundary:
            continue
        bar = CanonicalDailyBar(
            code=row[0],
            trade_date=date.fromisoformat(row[1]),
            open=row[2],
            high=row[3],
            low=row[4],
            close=row[5],
            volume=int(row[6]),
            amount=row[7],
            source=row[8],
            source_at=source_at,
            fetched_at=fetched_at,
            as_of_date=date.fromisoformat(row[11]),
        )
        if bar.content_hash != row[12]:
            raise ValueError("原始日线完整性哈希不匹配")
        bars.append(bar)
    return tuple(bars)


def _visible_absences(
    connection: sqlite3.Connection,
    code: str,
    start: date,
    end: date,
    boundary: datetime,
) -> tuple[MarketAbsence, ...]:
    rows = connection.execute(
        """
        SELECT code, trade_date, reason, source, source_at, fetched_at, content_hash
        FROM market_daily_absences WHERE code = ? AND trade_date BETWEEN ? AND ? ORDER BY trade_date
        """,
        (code, start.isoformat(), end.isoformat()),
    ).fetchall()
    result: list[MarketAbsence] = []
    for row in rows:
        source_at = _timestamp(row[4], "停牌来源时间")
        fetched_at = _timestamp(row[5], "停牌抓取时间")
        if source_at > boundary or fetched_at > boundary:
            continue
        absence = MarketAbsence(row[0], date.fromisoformat(row[1]), row[2], row[3], source_at, fetched_at)
        if absence.content_hash != row[6]:
            raise ValueError("停牌事实完整性哈希不匹配")
        result.append(absence)
    return tuple(result)


def _visible_factors(
    connection: sqlite3.Connection,
    code: str,
    bars: tuple[CanonicalDailyBar, ...],
    boundary: datetime,
) -> tuple[AdjustmentFactor, ...]:
    first, last = bars[0].trade_date, bars[-1].trade_date
    rows = connection.execute(
        """
        SELECT code, trade_date, factor, source, source_at, fetched_at, algorithm_version, content_hash
        FROM market_adjustment_factors
        WHERE code = ? AND trade_date BETWEEN ? AND ? ORDER BY trade_date
        """,
        (code, first.isoformat(), last.isoformat()),
    ).fetchall()
    factors: dict[date, AdjustmentFactor] = {}
    for row in rows:
        source_at = _timestamp(row[4], "复权来源时间")
        fetched_at = _timestamp(row[5], "复权抓取时间")
        if source_at > boundary or fetched_at > boundary:
            continue
        factor = AdjustmentFactor(
            row[0], date.fromisoformat(row[1]), row[2], row[3], source_at, fetched_at, row[6]
        )
        if factor.content_hash != row[7]:
            raise ValueError("复权因子完整性哈希不匹配")
        factors[factor.trade_date] = factor
    missing = [bar.trade_date for bar in bars if bar.trade_date not in factors]
    if missing:
        raise ValueError(f"研究价格序列缺少 {len(missing)} 个复权因子")
    return tuple(factors[bar.trade_date] for bar in bars)


def _timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name}无效")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field_name}无效") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{field_name}必须带时区")
    return result


def _raw_row(bar: CanonicalDailyBar) -> dict[str, object]:
    row: dict[str, object] = {
        "code": bar.code,
        "trade_date": bar.trade_date.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "adjustment": "unadjusted",
        "price_unit": "CNY/share",
        "volume_unit": "share",
        "source": bar.source,
        "source_at": bar.source_at.isoformat(),
        "fetched_at": bar.fetched_at.isoformat(),
        "content_hash": bar.content_hash,
    }
    if bar.amount is not None:
        row["amount"] = bar.amount
        row["amount_unit"] = "CNY"
    return row


def _research_row(bar: object) -> dict[str, object]:
    return {
        "code": bar.code,
        "trade_date": bar.trade_date.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "factor": bar.factor,
        "raw_content_hash": bar.raw_content_hash,
        "factor_content_hash": bar.factor_content_hash,
        "price_unit": "CNY/share",
    }


class _NdjsonBackingWriter:
    """Deterministic, bounded-memory writer for the immutable query dataset."""

    def __init__(self, directory: Path) -> None:
        fd, raw_path = tempfile.mkstemp(
            prefix=".whole-market-query-",
            suffix=".jsonl.gz",
            dir=directory,
        )
        self.path = Path(raw_path)
        self._raw = os.fdopen(fd, "wb")
        self._gzip = gzip.GzipFile(
            filename="",
            mode="wb",
            compresslevel=6,
            fileobj=self._raw,
            mtime=0,
        )
        self._finished = False

    def write(self, kind: str, row: dict[str, object]) -> None:
        if self._finished:
            raise RuntimeError("query backing is already finished")
        self._gzip.write(canonical_json({"kind": kind, "row": row}) + b"\n")

    def finish(self) -> Path:
        if not self._finished:
            self._gzip.close()
            self._raw.flush()
            os.fsync(self._raw.fileno())
            self._raw.close()
            self._finished = True
        return self.path

    def abort(self) -> None:
        if not self._finished:
            try:
                self._gzip.close()
            except OSError:
                pass
            try:
                self._raw.close()
            except OSError:
                pass
            self._finished = True
        self.path.unlink(missing_ok=True)


class _OrderedSetHash:
    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._first = True

    def add(self, key: str, content_hash: str) -> None:
        if not self._first:
            self._digest.update(b"\n")
        self._digest.update(f"{key}:{content_hash}".encode("utf-8"))
        self._first = False

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def _set_hash(items: object) -> str:
    digest = _OrderedSetHash()
    for key, content_hash in items:
        digest.add(str(key), str(content_hash))
    return digest.hexdigest()


def _warning(request: ProductRequest, message: str) -> ProviderObservation:
    return ProviderObservation(
        provider=LocalMarketProvider.provider_id,
        payload={"status": "unavailable", "rows": [], "raw_rows": [], "message": message[:320]},
        observed_at=request.boundary.as_of,
        source_locator=f"sqlite://{request.subject.code}/market_daily",
        quality_status="warning",
        quality_message=message[:320],
        coverage=0.0,
        fetched_at=datetime.now(timezone.utc),
        schema_version="market-daily-research@2",
    )
