import datetime as dt
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class QualityResult:
    check_name: str
    severity: str
    passed: bool
    details: str

    @property
    def blocking_failure(self) -> bool:
        return self.severity == "blocking" and not self.passed


def _three_year_cutoff(as_of: str) -> dt.date:
    as_of_date = dt.datetime.fromisoformat(as_of).date()
    try:
        return as_of_date.replace(year=as_of_date.year - 3)
    except ValueError:
        return as_of_date.replace(year=as_of_date.year - 3, day=28)


def evaluate_quality(
    connection: sqlite3.Connection,
    required_codes: list[str],
    as_of: str,
) -> list[QualityResult]:
    as_of_date = dt.datetime.fromisoformat(as_of).date()
    cutoff_date = _three_year_cutoff(as_of)
    results: list[QualityResult] = []
    for code in required_codes:
        has_recent_row = connection.execute(
            """
            SELECT 1
            FROM market_daily
            WHERE code = ? AND trade_date <= ?
            LIMIT 1
            """,
            (code, as_of_date.isoformat()),
        ).fetchone() is not None
        has_cutoff_row = connection.execute(
            """
            SELECT 1
            FROM market_daily
            WHERE code = ? AND trade_date <= ?
            LIMIT 1
            """,
            (code, cutoff_date.isoformat()),
        ).fetchone() is not None
        passed = has_recent_row and has_cutoff_row
        count = connection.execute(
            """
            SELECT count(*)
            FROM market_daily
            WHERE code = ? AND trade_date <= ?
            """,
            (code, as_of_date.isoformat()),
        ).fetchone()[0]
        results.append(
            QualityResult(
                check_name=f"three_year_history:{code}",
                severity="blocking",
                passed=passed,
                details=(
                    f"{count} market_daily rows available through {as_of_date.isoformat()}; "
                    f"cutoff {cutoff_date.isoformat()} satisfied={has_cutoff_row}"
                ),
            )
        )
    return results


def has_blocking_failure(
    connection: sqlite3.Connection,
    required_codes: list[str],
    as_of: str,
) -> bool:
    return any(
        result.blocking_failure
        for result in evaluate_quality(connection, required_codes, as_of)
    )
