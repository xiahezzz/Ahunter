from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from advisor.agents.astock_adapter import (
    AnalystOutput,
    AnalystRunner,
    DataQualityBlockedError,
    run_analyst_flow,
)
from advisor.charts.kline import generate_kline_chart
from advisor.config import (
    load_advisor_config,
    resolve_chart_dir,
    resolve_profile_dir,
    resolve_state_db,
)
from advisor.db.migrate import migrate_database
from advisor.db.repository import connect
from advisor.evidence.mx_adapter import CollectorSnapshot
from advisor.evidence.service import EvidenceRecord, persist_evidence
from advisor.paths import repo_root
from advisor.profiles.service import StockProfile, render_profile_markdown, upsert_profile
from advisor.quality import (
    QualityGateResult,
    QualityRequest,
    QualityResult,
    evaluate_run_quality,
    persist_quality_results,
)
from advisor.reporting.contracts import AdviceItem, ReportPaths, ReviewItem
from advisor.reporting.failure import write_failure_report
from advisor.reporting.premarket import write_premarket_report
from advisor.reporting.review import write_review_report


QualityEvaluator = Callable[[sqlite3.Connection, QualityRequest], QualityGateResult]
EvidencePersister = Callable[..., list[EvidenceRecord]]


@dataclass(frozen=True)
class CoordinatorResult:
    run_id: str
    status: str
    report_paths: ReportPaths
    warnings: tuple[str, ...] = ()


def run_premarket(
    *,
    collector_snapshot: CollectorSnapshot,
    as_of: datetime,
    report_date: str | date,
    candidate_codes: Sequence[str],
    output_dir: Path,
    analyst_runner: AnalystRunner | None = None,
    db_path: Path | None = None,
    chart_dir: Path | None = None,
    profile_dir: Path | None = None,
    config_path: Path | None = None,
    root: Path | None = None,
    run_id: str | None = None,
    report_run_id: str | None = None,
    rerun_reason: str | None = None,
    supersedes: str | None = None,
    quality_evaluator: QualityEvaluator = evaluate_run_quality,
    evidence_persister: EvidencePersister = persist_evidence,
) -> CoordinatorResult:
    report_day = _report_date(report_date)
    codes = _candidate_codes(candidate_codes)
    db_path, chart_dir, profile_dir = _storage_paths(
        db_path, chart_dir, profile_dir, config_path=config_path, root=root
    )
    active_run_id = run_id or _default_run_id("premarket", as_of)
    migrate_database(db_path)
    connection = connect(db_path)
    _start_run(connection, active_run_id, "premarket", as_of)
    request = QualityRequest(active_run_id, "premarket", as_of, codes, collector_snapshot)

    try:
        preflight = quality_evaluator(connection, request)
        persist_quality_results(connection, request, preflight.checks)
        blocking = _blocking_failures(preflight)
        if blocking and any(item.check_name != "analyst_contract_readiness" for item in blocking):
            return _blocked_result(
                connection, active_run_id, "premarket", report_day, output_dir, blocking, as_of
            )

        connection.execute("BEGIN IMMEDIATE")
        evidence = evidence_persister(
            connection, active_run_id, collector_snapshot, as_of=as_of
        )
        evidence_payload = [_evidence_payload(item) for item in evidence]
        outputs: list[AnalystOutput] = []
        for code in codes:
            outputs.extend(
                run_analyst_flow(
                    code, report_day, _evidence_for_code(evidence_payload, code),
                    runner=analyst_runner,
                )
            )
        _persist_analyst_outputs(connection, active_run_id, as_of, outputs)

        final_gate = quality_evaluator(connection, request)
        persist_quality_results(connection, request, final_gate.checks)
        final_blocking = _blocking_failures(final_gate)
        if final_blocking:
            connection.rollback()
            return _blocked_result(
                connection, active_run_id, "premarket", report_day, output_dir,
                final_blocking, as_of,
            )

        advice_items = _advice_items(active_run_id, codes, outputs, evidence)
        _upsert_securities(connection, codes, as_of)
        _persist_advice(connection, active_run_id, as_of, advice_items)
        connection.commit()

        warnings, _ = _project_premarket_profiles(
            connection, db_path, chart_dir, profile_dir, report_day, as_of,
            active_run_id, codes, outputs, evidence,
        )
        paths = write_premarket_report(
            report_day,
            advice_items,
            output_dir,
            quality_results=final_gate.checks,
            context=_premarket_context(outputs, evidence, warnings),
            run_id=report_run_id,
            rerun_reason=rerun_reason,
            supersedes=supersedes,
        )
        _archive_report(connection, active_run_id, "premarket", report_day, paths, as_of)
        _finish_run(connection, active_run_id, "passed", as_of, _warning_message(warnings))
        return CoordinatorResult(active_run_id, "passed", paths, tuple(warnings))
    except DataQualityBlockedError:
        connection.rollback()
        failure = QualityResult(
            "analyst_contract_readiness", "blocking", False,
            "analyst flow blocked publication",
        )
        return _blocked_result(
            connection, active_run_id, "premarket", report_day, output_dir,
            [failure], as_of,
        )
    except BaseException:
        connection.rollback()
        _finish_run(connection, active_run_id, "failed", as_of, "coordinator failed")
        raise
    finally:
        connection.close()


def run_review(
    *,
    collector_snapshot: CollectorSnapshot,
    as_of: datetime,
    report_date: str | date,
    candidate_codes: Sequence[str],
    output_dir: Path,
    db_path: Path | None = None,
    chart_dir: Path | None = None,
    profile_dir: Path | None = None,
    config_path: Path | None = None,
    root: Path | None = None,
    run_id: str | None = None,
    report_run_id: str | None = None,
    rerun_reason: str | None = None,
    supersedes: str | None = None,
    premarket_run_id: str = "initial",
    quality_evaluator: QualityEvaluator = evaluate_run_quality,
) -> CoordinatorResult:
    report_day = _report_date(report_date)
    codes = _candidate_codes(candidate_codes)
    db_path, chart_dir, profile_dir = _storage_paths(
        db_path, chart_dir, profile_dir, config_path=config_path, root=root
    )
    active_run_id = run_id or _default_run_id("review", as_of)
    migrate_database(db_path)
    connection = connect(db_path)
    _start_run(connection, active_run_id, "review", as_of)
    request = QualityRequest(active_run_id, "review", as_of, codes, collector_snapshot)

    try:
        gate = quality_evaluator(connection, request)
        effective_checks = _review_quality_checks(gate.checks)
        persist_quality_results(connection, request, effective_checks)
        blocking = [item for item in effective_checks if item.blocking_failure]
        if blocking:
            return _blocked_result(
                connection, active_run_id, "review", report_day, output_dir, blocking, as_of
            )

        morning = _load_morning_advice(connection, report_day)
        if not morning:
            failure = QualityResult(
                "advice_linkage", "blocking", False, "morning advice is unavailable"
            )
            return _blocked_result(
                connection, active_run_id, "review", report_day, output_dir, [failure], as_of
            )
        reviews = [
            ReviewItem(
                _stable_id("review", active_run_id, item.advice_id),
                item.advice_id,
                "reviewed",
                f"Daily review completed for {item.code} {item.action} research advice.",
            )
            for item in morning
        ]
        _persist_reviews(connection, active_run_id, as_of, reviews)
        warnings = _project_review_profiles(
            connection, db_path, chart_dir, profile_dir, report_day, as_of,
            active_run_id, morning, reviews,
        )
        paths = write_review_report(
            report_day,
            morning,
            reviews,
            output_dir,
            quality_results=effective_checks,
            context={
                "stock_profile_updates": [item.review_text for item in reviews],
                "next_day_carryover": [f"{item.code}: reassess" for item in morning],
                "market_outcome": warnings,
            },
            run_id=report_run_id,
            rerun_reason=rerun_reason,
            supersedes=supersedes,
            premarket_run_id=premarket_run_id,
        )
        _archive_report(connection, active_run_id, "review", report_day, paths, as_of)
        _finish_run(connection, active_run_id, "passed", as_of, _warning_message(warnings))
        return CoordinatorResult(active_run_id, "passed", paths, tuple(warnings))
    except BaseException:
        connection.rollback()
        _finish_run(connection, active_run_id, "failed", as_of, "coordinator failed")
        raise
    finally:
        connection.close()


def _storage_paths(
    db_path: Path | None,
    chart_dir: Path | None,
    profile_dir: Path | None,
    *,
    config_path: Path | None,
    root: Path | None,
) -> tuple[Path, Path, Path]:
    if db_path is not None and chart_dir is not None and profile_dir is not None:
        return Path(db_path), Path(chart_dir), Path(profile_dir)
    config = load_advisor_config(config_path)
    repository = root or repo_root()
    return (
        Path(db_path) if db_path is not None else resolve_state_db(config, repository),
        Path(chart_dir) if chart_dir is not None else resolve_chart_dir(config, repository),
        Path(profile_dir) if profile_dir is not None else resolve_profile_dir(config, repository),
    )


def _start_run(
    connection: sqlite3.Connection, run_id: str, run_type: str, as_of: datetime
) -> None:
    timestamp = as_of.isoformat()
    connection.execute(
        """
        INSERT INTO advisor_runs (run_id, run_type, as_of, status, started_at)
        VALUES (?, ?, ?, 'running', ?)
        """,
        (run_id, run_type, timestamp, timestamp),
    )
    connection.commit()


def _finish_run(
    connection: sqlite3.Connection,
    run_id: str,
    status: str,
    as_of: datetime,
    message: str | None,
) -> None:
    connection.execute(
        "UPDATE advisor_runs SET status = ?, finished_at = ?, message = ? WHERE run_id = ?",
        (status, as_of.isoformat(), message, run_id),
    )
    connection.commit()


def _blocked_result(
    connection: sqlite3.Connection,
    run_id: str,
    run_type: str,
    report_date: str,
    output_dir: Path,
    failures: Sequence[QualityResult],
    as_of: datetime,
) -> CoordinatorResult:
    paths = write_failure_report(
        report_date, run_type, list(failures), output_dir, run_id=run_id
    )
    _archive_report(connection, run_id, "failure", report_date, paths, as_of)
    _finish_run(connection, run_id, "blocked", as_of, "quality gate blocked publication")
    return CoordinatorResult(run_id, "blocked", paths)


def _persist_analyst_outputs(
    connection: sqlite3.Connection,
    run_id: str,
    as_of: datetime,
    outputs: Sequence[AnalystOutput],
) -> None:
    for output in outputs:
        connection.execute(
            """
            INSERT INTO analyst_outputs (
              output_id, run_id, role, code, as_of, summary, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _stable_id("analyst", run_id, output.code, output.role),
                run_id,
                output.role,
                output.code,
                as_of.isoformat(),
                output.summary,
                json.dumps(_json_safe(output.payload), sort_keys=True, separators=(",", ":")),
            ),
        )


def _advice_items(
    run_id: str,
    codes: tuple[str, ...],
    outputs: Sequence[AnalystOutput],
    evidence: Sequence[EvidenceRecord],
) -> list[AdviceItem]:
    by_code = {(output.code, output.role): output for output in outputs}
    items = []
    for code in codes:
        summaries = [
            by_code[(code, role)].summary
            for role in ("portfolio_manager", "trader", "research_manager")
            if (code, role) in by_code and by_code[(code, role)].summary
        ]
        evidence_ids = [
            item.evidence_id for item in evidence if item.code in {None, code}
        ]
        items.append(
            AdviceItem(
                _stable_id("advice", run_id, code),
                code,
                "watch",
                0.5,
                " ".join(summaries)[:2000] or "Monitor pending additional research evidence.",
                evidence_ids,
            )
        )
    return items


def _persist_advice(
    connection: sqlite3.Connection,
    run_id: str,
    as_of: datetime,
    items: Sequence[AdviceItem],
) -> None:
    for item in items:
        connection.execute(
            """
            INSERT INTO advice (
              advice_id, run_id, code, action, confidence, rationale,
              evidence_ids_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.advice_id, run_id, item.code, item.action, item.confidence,
                item.rationale,
                json.dumps(item.evidence_ids, separators=(",", ":")),
                as_of.isoformat(),
            ),
        )


def _persist_reviews(
    connection: sqlite3.Connection,
    run_id: str,
    as_of: datetime,
    reviews: Sequence[ReviewItem],
) -> None:
    for item in reviews:
        connection.execute(
            """
            INSERT INTO reviews (review_id, run_id, advice_id, outcome, review_text, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (item.review_id, run_id, item.advice_id, item.outcome, item.review_text, as_of.isoformat()),
        )
    connection.commit()


def _upsert_securities(
    connection: sqlite3.Connection, codes: Sequence[str], as_of: datetime
) -> None:
    for code in codes:
        connection.execute(
            """
            INSERT INTO securities (code, name, exchange, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (code, code, _exchange(code), as_of.isoformat(), as_of.isoformat()),
        )


def _project_premarket_profiles(
    connection: sqlite3.Connection,
    db_path: Path,
    chart_dir: Path,
    profile_dir: Path,
    report_date: str,
    as_of: datetime,
    run_id: str,
    codes: tuple[str, ...],
    outputs: Sequence[AnalystOutput],
    evidence: Sequence[EvidenceRecord],
) -> tuple[list[str], list[StockProfile]]:
    warnings: list[str] = []
    profiles = []
    for code in codes:
        assets = _chart_assets(connection, db_path, chart_dir, report_date, as_of, code, warnings)
        code_outputs = [item for item in outputs if item.code == code]
        portfolio = next((item.summary for item in code_outputs if item.role == "portfolio_manager"), "")
        profile = StockProfile(
            code, _security_name(connection, code), _security_industry(connection, code),
            portfolio or "Research watch candidate.",
            [item.summary for item in evidence if item.code in {None, code}],
            [],
            [f"{item.role}: {item.summary}" for item in code_outputs],
            [item.summary for item in code_outputs if item.role.endswith("_risk")],
            assets,
        )
        _write_profile(
            connection, profile_dir, profile, as_of, run_id, "premarket projection"
        )
        profiles.append(profile)
    return warnings, profiles


def _project_review_profiles(
    connection: sqlite3.Connection,
    db_path: Path,
    chart_dir: Path,
    profile_dir: Path,
    report_date: str,
    as_of: datetime,
    run_id: str,
    morning: Sequence[AdviceItem],
    reviews: Sequence[ReviewItem],
) -> list[str]:
    warnings: list[str] = []
    review_by_advice = {item.advice_id: item for item in reviews}
    for advice in morning:
        existing = connection.execute(
            """
            SELECT thesis_json, information_flow_json, capital_flow_json,
                   analyst_flow_json, assets_json FROM stock_profiles WHERE code = ?
            """,
            (advice.code,),
        ).fetchone()
        assets = _chart_assets(
            connection, db_path, chart_dir, report_date, as_of, advice.code, warnings
        )
        if existing:
            thesis = json.loads(existing[0])
            information = json.loads(existing[1])
            capital = json.loads(existing[2])
            analyst = json.loads(existing[3])
            assets = list(dict.fromkeys(json.loads(existing[4]) + assets))
        else:
            thesis = {"name": advice.code, "industry": "", "thesis": advice.rationale}
            information, capital, analyst = [], [], []
        analyst.append(review_by_advice[advice.advice_id].review_text)
        profile = StockProfile(
            advice.code,
            str(thesis.get("name", advice.code)),
            str(thesis.get("industry", "")),
            str(thesis.get("thesis", advice.rationale)),
            information,
            capital,
            analyst,
            [],
            assets,
        )
        _write_profile(
            connection, profile_dir, profile, as_of, run_id, "daily review projection"
        )
    return warnings


def _chart_assets(
    connection: sqlite3.Connection,
    db_path: Path,
    chart_dir: Path,
    report_date: str,
    as_of: datetime,
    code: str,
    warnings: list[str],
) -> list[str]:
    output = chart_dir / report_date / f"{code}-kline.png"
    try:
        path = generate_kline_chart(db_path, code, output)
    except (OSError, ValueError, sqlite3.Error) as error:
        warnings.append(f"{code} chart omitted: {type(error).__name__}")
        return []
    asset_id = _stable_id("chart", code, report_date, "kline")
    connection.execute(
        """
        INSERT INTO chart_assets (asset_id, code, chart_type, as_of, path, created_at)
        VALUES (?, ?, 'kline', ?, ?, ?)
        ON CONFLICT(asset_id) DO UPDATE SET path = excluded.path, as_of = excluded.as_of
        """,
        (asset_id, code, as_of.isoformat(), str(path), as_of.isoformat()),
    )
    connection.commit()
    return [str(path)]


def _write_profile(
    connection: sqlite3.Connection,
    profile_dir: Path,
    profile: StockProfile,
    as_of: datetime,
    run_id: str,
    change_summary: str,
) -> None:
    upsert_profile(connection, profile)
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / f"{profile.code}.md").write_text(
        render_profile_markdown(profile), encoding="utf-8"
    )
    snapshot = _json_safe(dataclasses.asdict(profile))
    connection.execute(
        """
        INSERT INTO stock_profile_history (
          history_id, code, run_id, change_summary, snapshot_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            _stable_id("profile-history", profile.code, as_of.isoformat(), change_summary),
            profile.code,
            run_id,
            change_summary,
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            as_of.isoformat(),
        ),
    )
    connection.commit()


def _archive_report(
    connection: sqlite3.Connection,
    run_id: str,
    report_type: str,
    report_date: str,
    paths: ReportPaths,
    as_of: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO report_archive (
          report_id, run_id, report_type, report_date, markdown_path, json_path, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _stable_id("report", run_id, report_type), run_id, report_type, report_date,
            str(paths.markdown_path), str(paths.json_path), as_of.isoformat(),
        ),
    )
    connection.commit()


def _load_morning_advice(
    connection: sqlite3.Connection, report_date: str
) -> list[AdviceItem]:
    rows = connection.execute(
        """
        SELECT advice.advice_id, advice.code, advice.action, advice.confidence,
               advice.rationale, advice.evidence_ids_json
        FROM advice JOIN advisor_runs ON advisor_runs.run_id = advice.run_id
        WHERE advisor_runs.run_type = 'premarket' AND advisor_runs.status = 'passed'
          AND date(advisor_runs.as_of) = ?
        ORDER BY advice.advice_id
        """,
        (report_date,),
    ).fetchall()
    return [AdviceItem(row[0], row[1], row[2], row[3], row[4], json.loads(row[5])) for row in rows]


def _review_quality_checks(checks: tuple[QualityResult, ...]) -> tuple[QualityResult, ...]:
    return tuple(
        QualityResult(
            item.check_name,
            "info",
            True,
            "analyst outputs are not required for review",
        )
        if item.check_name == "analyst_contract_readiness" and item.blocking_failure
        else item
        for item in checks
    )


def _blocking_failures(gate: QualityGateResult) -> list[QualityResult]:
    return [item for item in gate.checks if item.blocking_failure]


def _evidence_payload(record: EvidenceRecord) -> dict[str, Any]:
    return dataclasses.asdict(record) | {"quality_status": "passed"}


def _evidence_for_code(items: list[dict[str, Any]], code: str) -> list[dict[str, Any]]:
    return [item for item in items if item.get("code") in {None, code}]


def _premarket_context(
    outputs: Sequence[AnalystOutput],
    evidence: Sequence[EvidenceRecord],
    warnings: Sequence[str],
) -> dict[str, list[str]]:
    return {
        "information_flow": [item.summary for item in evidence],
        "analyst_flow": [f"{item.role}: {item.summary}" for item in outputs],
        "evidence": [item.evidence_id for item in evidence],
        "risk_controls": list(warnings),
    }


def _json_safe(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _json_safe(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (datetime, date, Path)):
        return str(value)
    return str(value)


def _candidate_codes(values: Sequence[str]) -> tuple[str, ...]:
    codes = tuple(values)
    if not codes or len(set(codes)) != len(codes) or any(
        not isinstance(code, str) or len(code) != 6 or not code.isdigit()
        for code in codes
    ):
        raise ValueError("candidate_codes are missing or invalid")
    return codes


def _report_date(value: str | date) -> str:
    if isinstance(value, datetime):
        raise ValueError("report_date must be a date")
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as error:
        raise ValueError("report_date is invalid") from error


def _default_run_id(run_type: str, as_of: datetime) -> str:
    return f"{run_type}-{as_of:%Y%m%d-%H%M%S}"


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _exchange(code: str) -> str:
    return "SSE" if code.startswith("6") else "SZSE"


def _security_name(connection: sqlite3.Connection, code: str) -> str:
    row = connection.execute("SELECT name FROM securities WHERE code = ?", (code,)).fetchone()
    return row[0] if row else code


def _security_industry(connection: sqlite3.Connection, code: str) -> str:
    row = connection.execute("SELECT industry FROM securities WHERE code = ?", (code,)).fetchone()
    return row[0] or "" if row else ""


def _warning_message(warnings: Sequence[str]) -> str | None:
    return "; ".join(warnings) if warnings else None
