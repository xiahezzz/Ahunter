from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import re
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
from advisor.reporting.contracts import (
    AdviceItem,
    ReportPaths,
    ReviewItem,
    read_verified_archive,
)
from advisor.reporting.failure import write_failure_report
from advisor.reporting.premarket import write_premarket_report
from advisor.reporting.review import write_review_report


QualityEvaluator = Callable[[sqlite3.Connection, QualityRequest], QualityGateResult]
EvidencePersister = Callable[..., list[EvidenceRecord]]
_RESEARCH_ACTIONS = frozenset({"buy", "watch", "hold", "reduce", "exit", "avoid"})
_CODE_PATTERN = re.compile(r"(?<!\d)([03468]\d{5})(?!\d)")


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
    db_path, chart_dir, profile_dir = _storage_paths(
        db_path, chart_dir, profile_dir, config_path=config_path, root=root
    )
    active_run_id = run_id or _default_run_id("premarket", as_of)
    migrate_database(db_path)
    connection = connect(db_path)
    codes = _expanded_candidate_codes(connection, candidate_codes, collector_snapshot)
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
        if any(output.code not in codes for output in outputs):
            raise DataQualityBlockedError("analyst output referenced an unchecked security")
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

        warnings, _ = _project_premarket_profiles(
            connection, db_path, chart_dir, profile_dir, report_day, as_of,
            active_run_id, codes, outputs, evidence,
        )
        paths = write_premarket_report(
            report_day,
            advice_items,
            output_dir,
            quality_results=final_gate.checks,
            context=_premarket_context(outputs, evidence, advice_items, report_day, warnings),
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
    db_path, chart_dir, profile_dir = _storage_paths(
        db_path, chart_dir, profile_dir, config_path=config_path, root=root
    )
    active_run_id = run_id or _default_run_id("review", as_of)
    migrate_database(db_path)
    connection = connect(db_path)
    codes = _expanded_candidate_codes(connection, candidate_codes, collector_snapshot)
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

        connection.execute("BEGIN IMMEDIATE")
        morning = _load_morning_advice(
            connection, output_dir, report_day, premarket_run_id
        )
        if not morning:
            failure = QualityResult(
                "advice_linkage", "blocking", False, "morning advice is unavailable"
            )
            return _blocked_result(
                connection, active_run_id, "review", report_day, output_dir, [failure], as_of
            )
        morning_codes = tuple(item.code for item in morning)
        if len(morning_codes) != len(set(morning_codes)) or set(morning_codes) != set(codes):
            failure = QualityResult(
                "advice_scope", "blocking", False,
                "selected morning advice does not match quality-checked candidates",
            )
            return _blocked_result(
                connection, active_run_id, "review", report_day, output_dir, [failure], as_of
            )
        reviews = [
            _evaluate_review_item(connection, active_run_id, report_day, as_of, item)
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
        role_summaries = [
            f"{role}: {by_code[(code, role)].summary}"
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
                *_analyst_decision(code, outputs, bool(evidence_ids)),
                " ".join(role_summaries)[:2000]
                or "research_manager: Monitor pending additional research evidence.",
                evidence_ids,
            )
        )
    return items


def _analyst_decision(
    code: str, outputs: Sequence[AnalystOutput], has_evidence: bool
) -> tuple[str, float]:
    relevant = [
        item for role in ("portfolio_manager", "trader", "research_manager")
        for item in outputs if item.code == code and item.role == role
    ]
    action: str | None = None
    confidence: float | None = None
    for output in relevant:
        for value in _bounded_payload_dicts(output.payload):
            candidate = value.get("action", value.get("decision"))
            if action is None and isinstance(candidate, str):
                normalized = candidate.strip().lower()
                if normalized in _RESEARCH_ACTIONS:
                    action = normalized
            if confidence is None:
                confidence = _valid_confidence(value.get("confidence"))
            if action is not None and confidence is not None:
                break
        if action is not None and confidence is not None:
            break
    if action is None:
        for output in relevant:
            match = re.search(
                r"\b(?:action|decision)\s*[:=]\s*(buy|watch|hold|reduce|exit|avoid)\b",
                output.summary[:2000], re.IGNORECASE,
            )
            if match:
                action = match.group(1).lower()
                break
    if confidence is None:
        for output in relevant:
            match = re.search(
                r"\bconfidence\s*[:=]\s*(0(?:\.\d+)?|1(?:\.0+)?)\b",
                output.summary[:2000], re.IGNORECASE,
            )
            if match:
                confidence = _valid_confidence(match.group(1))
                if confidence is not None:
                    break
    resolved_action = action or "watch"
    if confidence is None:
        confidence = 0.6 if action is not None and has_evidence else 0.5
    return resolved_action, confidence


def _bounded_payload_dicts(payload: object) -> list[dict[str, object]]:
    pending: list[tuple[object, int]] = [(payload, 0)]
    dictionaries: list[dict[str, object]] = []
    while pending and len(dictionaries) < 32:
        value, depth = pending.pop(0)
        if not isinstance(value, dict):
            continue
        dictionaries.append(value)
        if depth < 3:
            pending.extend((item, depth + 1) for item in list(value.values())[:32])
    return dictionaries


def _valid_confidence(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return confidence if math.isfinite(confidence) and 0 <= confidence <= 1 else None


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
        existing = connection.execute(
            "SELECT information_flow_json, analyst_flow_json, assets_json "
            "FROM stock_profiles WHERE code = ?", (code,),
        ).fetchone()
        prior_information, prior_analyst, prior_assets = (
            (json.loads(existing[0]), json.loads(existing[1]), json.loads(existing[2]))
            if existing else ([], [], [])
        )
        information = list(dict.fromkeys(
            prior_information + [item.summary for item in evidence if item.code in {None, code}]
        ))
        analyst = list(dict.fromkeys(
            prior_analyst + [f"{item.role}: {item.summary}" for item in code_outputs]
        ))
        assets = list(dict.fromkeys(prior_assets + assets))
        profile = StockProfile(
            code, _security_name(connection, code), _security_industry(connection, code),
            portfolio or "Research watch candidate.",
            information,
            [],
            analyst,
            [item.summary for item in code_outputs if item.role.endswith("_risk")],
            assets,
        )
        _write_profile(
            connection, profile_dir, profile, as_of, run_id, "premarket projection",
            transactional=True,
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
            connection, profile_dir, profile, as_of, run_id, "daily review projection",
            transactional=True,
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
        path = generate_kline_chart(db_path, code, output, as_of=as_of, report_date=report_date)
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
    return [str(path)]


def _write_profile(
    connection: sqlite3.Connection,
    profile_dir: Path,
    profile: StockProfile,
    as_of: datetime,
    run_id: str,
    change_summary: str,
    *,
    transactional: bool = False,
) -> None:
    if transactional:
        _upsert_profile_without_commit(connection, profile, as_of)
    else:
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


def _upsert_profile_without_commit(
    connection: sqlite3.Connection, profile: StockProfile, as_of: datetime
) -> None:
    connection.execute(
        """
        INSERT INTO stock_profiles (
          code, thesis_json, information_flow_json, capital_flow_json,
          fundamentals_json, analyst_flow_json, ledger_exposure_json,
          assets_json, updated_at
        ) VALUES (?, ?, ?, ?, '{}', ?, '{}', ?, ?)
        ON CONFLICT(code) DO UPDATE SET
          thesis_json = excluded.thesis_json,
          information_flow_json = excluded.information_flow_json,
          capital_flow_json = excluded.capital_flow_json,
          analyst_flow_json = excluded.analyst_flow_json,
          assets_json = excluded.assets_json,
          updated_at = excluded.updated_at
        """,
        (
            profile.code,
            json.dumps(
                {
                    "name": profile.name,
                    "industry": profile.industry,
                    "thesis": profile.thesis,
                },
                ensure_ascii=False,
            ),
            json.dumps(profile.information_flow, ensure_ascii=False),
            json.dumps(profile.capital_flow, ensure_ascii=False),
            json.dumps(profile.analyst_flow, ensure_ascii=False),
            json.dumps(profile.assets, ensure_ascii=False),
            as_of.isoformat(),
        ),
    )


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


def _load_morning_advice(
    connection: sqlite3.Connection,
    output_dir: Path,
    report_date: str,
    premarket_run_id: str,
) -> list[AdviceItem]:
    try:
        archive = read_verified_archive(
            output_dir, report_date, "premarket", premarket_run_id
        )
    except ValueError as error:
        raise ValueError("premarket archive not found") from error
    suffix = "" if premarket_run_id == "initial" else f".{premarket_run_id}"
    archive_directory = Path(os.path.abspath(output_dir)) / report_date
    expected_markdown = archive_directory / f"premarket{suffix}.md"
    expected_json = archive_directory / f"premarket{suffix}.json"
    archive_rows = connection.execute(
        """
        SELECT report_archive.run_id, report_archive.markdown_path,
               report_archive.json_path
        FROM report_archive
        JOIN advisor_runs ON advisor_runs.run_id = report_archive.run_id
        WHERE report_archive.report_type = 'premarket'
          AND report_archive.report_date = ?
          AND advisor_runs.run_type = 'premarket'
          AND advisor_runs.status = 'passed'
        """,
        (report_date,),
    ).fetchall()
    linked_rows = [
        row for row in archive_rows
        if _normalized_absolute_path(row[1]) == expected_markdown
        and _normalized_absolute_path(row[2]) == expected_json
    ]
    if len(linked_rows) != 1:
        raise ValueError("premarket archive not found")
    database_run_id = linked_rows[0][0]
    payload = archive["json"]
    archived_advice = payload.get("advice") if isinstance(payload, dict) else None
    archived_ids = payload.get("advice_ids") if isinstance(payload, dict) else None
    if not isinstance(archived_advice, list) or not isinstance(archived_ids, list):
        raise ValueError("invalid premarket archive")

    try:
        items = [
            AdviceItem(
                archived["advice_id"], archived["code"], archived["action"],
                archived["confidence"], archived["rationale"], archived["evidence_ids"],
            )
            for archived in archived_advice
            if isinstance(archived, dict)
        ]
        if len(items) != len(archived_advice):
            raise ValueError
        stored_items = [
            AdviceItem(row[0], row[1], row[2], row[3], row[4], json.loads(row[5]))
            for row in connection.execute(
                """
                SELECT advice_id, code, action, confidence, rationale, evidence_ids_json
                FROM advice
                WHERE run_id = ?
                ORDER BY rowid
                """,
                (database_run_id,),
            ).fetchall()
        ]
        if stored_items != items:
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("morning advice does not match premarket archive") from error
    if archived_ids != [item.advice_id for item in items]:
        raise ValueError("morning advice does not match premarket archive")
    return items


def _normalized_absolute_path(value: object) -> Path | None:
    if not isinstance(value, (str, os.PathLike)):
        return None
    return Path(os.path.abspath(value))


def _evaluate_review_item(
    connection: sqlite3.Connection,
    run_id: str,
    report_date: str,
    as_of: datetime,
    advice: AdviceItem,
) -> ReviewItem:
    market_rows = connection.execute(
        """
        SELECT trade_date, close FROM market_daily
        WHERE code = ? AND quality_status = 'passed'
          AND date(trade_date) <= date(?) AND date(trade_date) <= date(?)
        ORDER BY trade_date DESC LIMIT 2
        """,
        (advice.code, report_date, as_of.date().isoformat()),
    ).fetchall()
    ledger_rows = connection.execute(
        """
        SELECT transaction_type, COUNT(*) FROM ledger_transactions
        WHERE code = ? AND date(trade_date) = date(?)
          AND julianday(created_at) <= julianday(?)
        GROUP BY transaction_type ORDER BY transaction_type
        """,
        (advice.code, report_date, as_of.isoformat()),
    ).fetchall()
    ledger_count = sum(row[1] for row in ledger_rows)
    ledger_types = ", ".join(f"{row[0]}={row[1]}" for row in ledger_rows)
    ledger_text = (
        f"{ledger_count} ledger transaction{'s' if ledger_count != 1 else ''} recorded"
        + (f" ({ledger_types})" if ledger_types else "")
        if ledger_count
        else "no ledger transactions recorded"
    )

    valid_market = len(market_rows) == 2
    if valid_market:
        try:
            latest_close = float(market_rows[0][1])
            prior_close = float(market_rows[1][1])
            valid_market = math.isfinite(latest_close) and math.isfinite(prior_close)
        except (TypeError, ValueError, OverflowError):
            valid_market = False
    if not valid_market:
        outcome = "no_market_data"
        review_text = (
            f"No two valid closes were available through {report_date}; {ledger_text} "
            f"for {advice.code} on {report_date}."
        )
    else:
        change = latest_close - prior_close
        favorable = change > 0 if advice.action in {"buy", "watch", "hold"} else change <= 0
        if favorable:
            outcome = "followed_strength"
        elif ledger_count:
            outcome = "risk_review"
        else:
            outcome = "missed_or_flat"
        direction = "above" if change > 0 else "below" if change < 0 else "equal to"
        review_text = (
            f"Latest close {latest_close:.4f} on {market_rows[0][0]} was {direction} "
            f"the prior close {prior_close:.4f} on {market_rows[1][0]}; {ledger_text} "
            f"for {advice.code} on {report_date}."
        )
    return ReviewItem(
        _stable_id("review", run_id, advice.advice_id),
        advice.advice_id,
        outcome,
        review_text,
    )


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
    advice: Sequence[AdviceItem],
    report_date: str,
    warnings: Sequence[str],
) -> dict[str, list[str]]:
    return {
        "information_flow": [item.summary for item in evidence],
        "analyst_flow": [f"{item.role}: {item.summary}" for item in outputs],
        "evidence": [item.evidence_id for item in evidence],
        "chart_markers": [
            f"{item.code}: advice={item.action} at {report_date}" for item in advice
        ],
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
    if len(set(codes)) != len(codes) or any(
        not isinstance(code, str) or len(code) != 6 or not code.isdigit()
        for code in codes
    ):
        raise ValueError("candidate_codes are missing or invalid")
    return codes


def _expanded_candidate_codes(
    connection: sqlite3.Connection,
    values: Sequence[str],
    collector_snapshot: CollectorSnapshot,
) -> tuple[str, ...]:
    codes = list(_candidate_codes(values))
    for event in collector_snapshot.events[:1000]:
        summary = getattr(event, "summary", "")
        if isinstance(summary, str):
            codes.extend(_CODE_PATTERN.findall(summary[:800]))
    codes.extend(
        row[0] for row in connection.execute(
            "SELECT DISTINCT code FROM positions WHERE quantity != 0 ORDER BY code LIMIT 1000"
        ).fetchall()
        if isinstance(row[0], str)
    )
    codes.extend(
        row[0] for row in connection.execute(
            """
            SELECT code FROM ledger_transactions
            WHERE code IS NOT NULL AND transaction_type IN ('buy', 'sell')
            GROUP BY code
            HAVING SUM(CASE WHEN transaction_type = 'buy' THEN quantity ELSE -quantity END) > 0
            ORDER BY code LIMIT 1000
            """
        ).fetchall()
        if isinstance(row[0], str)
    )
    expanded = tuple(dict.fromkeys(codes))
    if not expanded:
        raise ValueError("candidate_codes are missing or invalid")
    return _candidate_codes(expanded)


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
