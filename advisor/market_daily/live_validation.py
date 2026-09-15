"""Read-only acceptance checks for a completed Market Daily cold-start run."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_A_SHARE_CODE = re.compile(r"[036]\d{5}\Z")


@dataclass(frozen=True)
class LiveValidationCheck:
    """One bounded, operator-facing acceptance result."""

    name: str
    passed: bool
    message: str
    details: dict[str, Any]

    def as_payload(self) -> dict[str, object]:
        return {
            "项目": self.name,
            "状态": "通过" if self.passed else "失败",
            "说明": self.message,
            "明细": self.details,
        }


@dataclass(frozen=True)
class LiveValidationReport:
    """The complete result of validating one immutable cold-start baseline."""

    run_id: str | None
    checks: tuple[LiveValidationCheck, ...]

    @property
    def passed(self) -> bool:
        return bool(self.run_id) and bool(self.checks) and all(check.passed for check in self.checks)

    def as_payload(self) -> dict[str, object]:
        return {
            "运行编号": self.run_id,
            "状态": "通过" if self.passed else "未通过",
            "检查": [check.as_payload() for check in self.checks],
        }


class MarketDailyLiveValidator:
    """Validate persisted Market Daily facts without fetching or writing anything."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path).expanduser().resolve()

    def validate(self, run_id: str | None = None) -> LiveValidationReport:
        if self.database_path.is_symlink() or not self.database_path.is_file():
            return LiveValidationReport(
                run_id,
                (LiveValidationCheck("数据库", False, "本地 Market Daily 数据库不可读取", {}),),
            )
        try:
            connection = sqlite3.connect(f"{self.database_path.as_uri()}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
        except sqlite3.Error:
            return LiveValidationReport(
                run_id,
                (LiveValidationCheck("数据库", False, "本地 Market Daily 数据库不可读取", {}),),
            )
        try:
            run = self._load_run(connection, run_id)
            if run is None:
                return LiveValidationReport(
                    run_id,
                    (LiveValidationCheck("运行状态", False, "没有可验收的 Market Daily 冷启动 Run", {}),),
                )
            resolved_run_id = str(run["run_id"])
            checks = (
                self._run_state_check(connection, run),
                self._universe_check(connection, run),
                self._coverage_check(connection, run),
                self._bar_contract_check(connection, run),
                self._factor_check(connection, run),
            )
            return LiveValidationReport(resolved_run_id, checks)
        except sqlite3.Error:
            return LiveValidationReport(
                run_id,
                (LiveValidationCheck("数据库", False, "本地 Market Daily 验收查询失败", {}),),
            )
        finally:
            connection.close()

    @staticmethod
    def _load_run(connection: sqlite3.Connection, run_id: str | None) -> sqlite3.Row | None:
        if run_id is not None:
            return connection.execute(
                """
                SELECT r.*, q.status AS request_status
                FROM market_daily_runs AS r
                JOIN market_daily_requests AS q ON q.request_id = r.request_id
                WHERE r.run_id = ? AND r.run_type = 'cold_start'
                """,
                (run_id,),
            ).fetchone()
        return connection.execute(
            """
            SELECT r.*, q.status AS request_status
            FROM market_daily_runs AS r
            JOIN market_daily_requests AS q ON q.request_id = r.request_id
            WHERE r.run_type = 'cold_start'
            ORDER BY r.created_at DESC, r.run_id DESC LIMIT 1
            """
        ).fetchone()

    @staticmethod
    def _run_state_check(connection: sqlite3.Connection, run: sqlite3.Row) -> LiveValidationCheck:
        counts = {
            str(row["status"]): int(row["count"])
            for row in connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM market_daily_run_items WHERE run_id = ? GROUP BY status
                """,
                (run["run_id"],),
            )
        }
        item_count = sum(counts.values())
        succeeded = counts.get("completed", 0) + counts.get("skipped", 0)
        failed = counts.get("source_missing", 0) + counts.get("conflicted", 0)
        unfinished = counts.get("pending", 0) + counts.get("running", 0)
        passed = (
            run["status"] == "complete"
            and run["request_status"] == "completed"
            and int(run["total_items"]) == item_count
            and int(run["completed_items"]) == succeeded
            and int(run["failed_items"]) == failed
            and failed == 0
            and unfinished == 0
        )
        details = {
            "运行状态": str(run["status"]),
            "请求状态": str(run["request_status"]),
            "总证券数": int(run["total_items"]),
            "已完成": succeeded,
            "失败": failed,
            "未完成": unfinished,
        }
        return LiveValidationCheck(
            "运行状态",
            passed,
            "Run 已完整封印且不存在失败或未完成项目" if passed else "Run 尚未达到完整封印条件",
            details,
        )

    @staticmethod
    def _universe_check(connection: sqlite3.Connection, run: sqlite3.Row) -> LiveValidationCheck:
        rows = connection.execute(
            """
            SELECT rs.code, rs.exchange, rs.list_date, rs.delist_date, rs.status,
                   s.security_type, s.is_st
            FROM market_daily_run_securities AS rs
            LEFT JOIN securities AS s ON s.code = rs.code
            WHERE rs.run_id = ? ORDER BY rs.code
            """,
            (run["run_id"],),
        ).fetchall()
        invalid = 0
        st_count = 0
        suspended_count = 0
        delisted_count = 0
        for row in rows:
            code = str(row["code"])
            exchange = str(row["exchange"])
            status = str(row["status"])
            is_valid_code = bool(_A_SHARE_CODE.fullmatch(code)) and not code.startswith("689")
            expected_exchange = "SH" if code.startswith("6") else "SZ"
            interval_valid = str(row["list_date"]) <= str(run["target_session"])
            if row["delist_date"] is not None:
                interval_valid = interval_valid and str(row["delist_date"]) >= str(run["start_date"])
            if (
                not is_valid_code
                or exchange != expected_exchange
                or row["security_type"] != "a_share"
                or status not in {"active", "suspended", "delisted"}
                or not interval_valid
            ):
                invalid += 1
            st_count += int(row["is_st"] or 0)
            suspended_count += int(status == "suspended")
            delisted_count += int(status == "delisted")
        passed = bool(rows) and invalid == 0 and len(rows) == int(run["total_items"])
        return LiveValidationCheck(
            "股票池范围",
            passed,
            "冻结股票池仅包含窗口内沪深普通 A 股" if passed else "冻结股票池存在无效证券或元数据缺失",
            {
                "证券数": len(rows),
                "无效证券": invalid,
                "ST数量": st_count,
                "停牌数量": suspended_count,
                "退市数量": delisted_count,
            },
        )

    @staticmethod
    def _coverage_check(connection: sqlite3.Connection, run: sqlite3.Row) -> LiveValidationCheck:
        row = connection.execute(
            _EXPECTED_COVERAGE_SQL,
            (run["run_id"],),
        ).fetchone()
        session = connection.execute(
            """
            SELECT COUNT(*) AS count, MIN(trade_date) AS first_date, MAX(trade_date) AS last_date
            FROM trading_sessions WHERE trade_date BETWEEN ? AND ?
            """,
            (run["start_date"], run["target_session"]),
        ).fetchone()
        expected = int(row["expected"] or 0)
        missing = int(row["missing"] or 0)
        conflicts = int(row["conflicts"] or 0)
        session_bounds = (
            session["first_date"] == run["start_date"] and session["last_date"] == run["target_session"]
        )
        passed = expected > 0 and missing == 0 and conflicts == 0 and session_bounds
        return LiveValidationCheck(
            "交易日覆盖",
            passed,
            "所有应覆盖交易日都有日线或有证据的停牌事实" if passed else "交易日覆盖、边界或事实一致性不完整",
            {
                "交易日数量": int(session["count"] or 0),
                "应覆盖交易日": expected,
                "缺失覆盖": missing,
                "事实冲突": conflicts,
                "起止边界匹配": session_bounds,
            },
        )

    @staticmethod
    def _bar_contract_check(connection: sqlite3.Connection, run: sqlite3.Row) -> LiveValidationCheck:
        row = connection.execute(_BAR_CONTRACT_SQL, (run["run_id"],)).fetchone()
        outside = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM market_daily AS bar
            JOIN market_daily_run_securities AS security
              ON security.run_id = ? AND security.code = bar.code
            WHERE bar.trade_date < ? OR bar.trade_date > ?
               OR bar.trade_date < security.list_date
               OR (security.delist_date IS NOT NULL AND bar.trade_date > security.delist_date)
            """,
            (run["run_id"], run["start_date"], run["target_session"]),
        ).fetchone()
        rows = int(row["rows"] or 0)
        invalid = int(row["invalid"] or 0)
        outside_count = int(outside["count"] or 0)
        passed = rows > 0 and invalid == 0 and outside_count == 0
        return LiveValidationCheck(
            "K线契约",
            passed,
            "原始 K 线均在窗口和上市区间内，且 OHLC、volume 与可选 amount 合法" if passed else "存在越界或不符合 K 线契约的数据",
            {"K线数": rows, "非法K线": invalid, "窗口或上市区间外K线": outside_count},
        )

    @staticmethod
    def _factor_check(connection: sqlite3.Connection, run: sqlite3.Row) -> LiveValidationCheck:
        row = connection.execute(_FACTOR_SQL, (run["run_id"],)).fetchone()
        bars = int(row["bars"] or 0)
        missing = int(row["missing"] or 0)
        invalid = int(row["invalid"] or 0)
        passed = bars > 0 and missing == 0 and invalid == 0
        return LiveValidationCheck(
            "复权因子",
            passed,
            "每根原始 K 线都有合法的独立复权因子" if passed else "存在缺失或非法复权因子",
            {"需因子K线": bars, "缺失因子": missing, "非法因子": invalid},
        )


_EXPECTED_COVERAGE_SQL = """
WITH expected AS (
  SELECT item.code, session.trade_date
  FROM market_daily_run_items AS item
  JOIN market_daily_run_securities AS security
    ON security.run_id = item.run_id AND security.code = item.code
  JOIN trading_sessions AS session
    ON session.trade_date BETWEEN item.start_date AND item.end_date
   AND session.trade_date >= security.list_date
   AND (security.delist_date IS NULL OR session.trade_date <= security.delist_date)
  WHERE item.run_id = ?
)
SELECT
  COUNT(*) AS expected,
  SUM(CASE WHEN bar.code IS NULL AND absence.code IS NULL THEN 1 ELSE 0 END) AS missing,
  SUM(CASE WHEN bar.code IS NOT NULL AND absence.code IS NOT NULL THEN 1 ELSE 0 END) AS conflicts
FROM expected
LEFT JOIN market_daily AS bar
  ON bar.code = expected.code AND bar.trade_date = expected.trade_date AND bar.quality_status = 'passed'
LEFT JOIN market_daily_absences AS absence
  ON absence.code = expected.code AND absence.trade_date = expected.trade_date
"""


_BAR_CONTRACT_SQL = """
WITH expected AS (
  SELECT item.code, session.trade_date
  FROM market_daily_run_items AS item
  JOIN market_daily_run_securities AS security
    ON security.run_id = item.run_id AND security.code = item.code
  JOIN trading_sessions AS session
    ON session.trade_date BETWEEN item.start_date AND item.end_date
   AND session.trade_date >= security.list_date
   AND (security.delist_date IS NULL OR session.trade_date <= security.delist_date)
  WHERE item.run_id = ?
)
SELECT
  COUNT(bar.code) AS rows,
  SUM(CASE WHEN bar.code IS NOT NULL AND (
    typeof(bar.volume) <> 'integer' OR bar.volume < 0 OR
    (bar.amount IS NOT NULL AND (typeof(bar.amount) NOT IN ('integer', 'real') OR bar.amount < 0)) OR
    typeof(bar.open) NOT IN ('integer', 'real') OR typeof(bar.high) NOT IN ('integer', 'real') OR
    typeof(bar.low) NOT IN ('integer', 'real') OR typeof(bar.close) NOT IN ('integer', 'real') OR
    bar.open <= 0 OR bar.high <= 0 OR bar.low <= 0 OR bar.close <= 0 OR
    bar.low > bar.open OR bar.open > bar.high OR bar.low > bar.close OR bar.close > bar.high OR
    bar.as_of_date < bar.trade_date OR date(bar.source_at) < bar.trade_date
  ) THEN 1 ELSE 0 END) AS invalid
FROM expected
LEFT JOIN market_daily AS bar
  ON bar.code = expected.code AND bar.trade_date = expected.trade_date AND bar.quality_status = 'passed'
"""


_FACTOR_SQL = """
WITH expected_bars AS (
  SELECT bar.code, bar.trade_date
  FROM market_daily_run_items AS item
  JOIN market_daily_run_securities AS security
    ON security.run_id = item.run_id AND security.code = item.code
  JOIN trading_sessions AS session
    ON session.trade_date BETWEEN item.start_date AND item.end_date
   AND session.trade_date >= security.list_date
   AND (security.delist_date IS NULL OR session.trade_date <= security.delist_date)
  JOIN market_daily AS bar
    ON bar.code = item.code AND bar.trade_date = session.trade_date AND bar.quality_status = 'passed'
  WHERE item.run_id = ?
)
SELECT
  COUNT(*) AS bars,
  SUM(CASE WHEN factor.code IS NULL THEN 1 ELSE 0 END) AS missing,
  SUM(CASE WHEN factor.code IS NOT NULL AND (
    typeof(factor.factor) NOT IN ('integer', 'real') OR factor.factor <= 0
  ) THEN 1 ELSE 0 END) AS invalid
FROM expected_bars
LEFT JOIN market_adjustment_factors AS factor
  ON factor.code = expected_bars.code AND factor.trade_date = expected_bars.trade_date
"""
