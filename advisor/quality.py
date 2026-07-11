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


def evaluate_quality(
    connection: sqlite3.Connection,
    required_codes: list[str],
    as_of: str,
) -> list[QualityResult]:
    results: list[QualityResult] = []
    for code in required_codes:
        count = connection.execute(
            "SELECT count(*) FROM market_daily WHERE code = ?",
            (code,),
        ).fetchone()[0]
        results.append(
            QualityResult(
                check_name=f"three_year_history:{code}",
                severity="blocking",
                passed=count > 0,
                details=f"{count} market_daily rows available as of {as_of}",
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
