from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date, datetime, time
import hashlib
import json
import signal
from pathlib import Path
import re
import time as wall_time
from typing import Any, Sequence
from uuid import uuid4
from zoneinfo import ZoneInfo

from advisor.config import (
    AdvisorConfig,
    load_advisor_config,
    resolve_research_artifact_dir,
    resolve_research_catalog,
    resolve_state_db,
)
from advisor.evidence.mx_adapter import read_collector_snapshot
from advisor.paths import repo_root
from advisor.research.agents.runner import AgentRunner
from advisor.research.artifacts import ArtifactStore
from advisor.research.catalog import ManifestCatalog, load_catalog, load_catalog_from_directory
from advisor.research.codex.executor import CodexExecutor
from advisor.research.contracts import ExecutionPolicy, ResearchBoundary, ResearchScope, ResearchSubject, RunStatus, VersionRef
from advisor.research.data_products.engine import DataProductEngine, ProviderRegistry
from advisor.research.decision.pipeline import DecisionPipeline
from advisor.research.providers.public import build_default_provider_registry
from advisor.research.reporting.cycle import CyclePublication, CycleReporter
from advisor.research.reporting.daily import DailyBriefRenderer
from advisor.research.batch import Candidate, DailyBatchResult, DailyResearchBatch, select_candidates
from advisor.research.state_machine import ResearchCycleCancelled, ResearchCycleEngine, ResearchCycleResult, TeamRunResult
from advisor.research.contracts import content_hash
from advisor.research.repository import ResearchRepository
from advisor.research.service import ResearchService, ResearchServiceCancelled, ResearchServiceStatus, ServiceExecutionResult


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _resolve_runtime_path(root: Path, value: Path | None) -> Path | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


@dataclass
class ResearchRuntime:
    root: Path
    config: AdvisorConfig
    catalog: ManifestCatalog
    policy: ExecutionPolicy
    repository: ResearchRepository
    artifact_store: ArtifactStore
    executor: Any
    product_engine: DataProductEngine
    agent_runner: AgentRunner
    decision_pipeline: DecisionPipeline
    cycle_engine: ResearchCycleEngine
    catalog_path: Path | None = None
    database_path: Path | None = None
    artifact_store_path: Path | None = None
    config_path: Path | None = None

    def close(self) -> None:
        self.repository.close()
        self.artifact_store.close()


@dataclass
class ResearchControlPlane:
    """The deliberately small dependency set used by submitters.

    A CLI or scheduler only needs the immutable catalog and durable request
    store.  Keeping this separate from :class:`ResearchRuntime` makes it
    impossible for a normal submission to accidentally preflight a Provider,
    instantiate a Codex session, or execute a Cycle in-process.
    """

    root: Path
    config: AdvisorConfig
    catalog: ManifestCatalog
    repository: ResearchRepository
    database_path: Path

    def close(self) -> None:
        self.repository.close()


@dataclass(frozen=True)
class PreflightResult:
    status: str
    catalog: str
    database: str
    artifact_store: str
    cli_version: str
    policy: str
    message: str = ""
    catalog_path: str = ""
    database_path: str = ""
    artifact_store_path: str = ""
    output_dir: str = ""
    checks: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ResearchRun:
    cycle: ResearchCycleResult
    publication: CyclePublication


@dataclass(frozen=True)
class DailyResearchRun:
    batch: DailyBatchResult
    publications: dict[str, CyclePublication]
    briefs: dict[str, Path]


def build_runtime(
    *,
    root: Path | None = None,
    config_path: Path | None = None,
    db_path: Path | None = None,
    artifact_dir: Path | None = None,
    provider_registry: ProviderRegistry | None = None,
    executor: Any | None = None,
    mx_snapshot: Any | None = None,
    mx_events_database: Path | None = None,
    allowed_rids_path: Path | None = None,
) -> ResearchRuntime:
    resolved_root = (root or repo_root()).resolve()
    resolved_config = (config_path or resolved_root / "config" / "advisor.yaml").resolve()
    config = load_advisor_config(resolved_config)
    configured_catalog = resolve_research_catalog(config, resolved_root)
    default_catalog = (resolved_root / "config" / "research").resolve()
    catalog = load_catalog(resolved_root) if configured_catalog == default_catalog else load_catalog_from_directory(configured_catalog)
    policy = catalog.execution_policy(config.research.execution_policy)
    resolved_db = (db_path or resolve_state_db(config, resolved_root)).resolve()
    resolved_artifacts = (artifact_dir or resolve_research_artifact_dir(config, resolved_root)).resolve()
    resolved_mx_events = _resolve_runtime_path(
        resolved_root,
        mx_events_database if mx_events_database is not None else Path("data/state/events.sqlite"),
    )
    resolved_allowed_rids = _resolve_runtime_path(
        resolved_root,
        allowed_rids_path if allowed_rids_path is not None else Path("config/allowed-rids.yaml"),
    )
    repository = ResearchRepository.open(resolved_db)
    store = ArtifactStore(resolved_artifacts)
    active_executor = executor or CodexExecutor()
    registry = provider_registry or build_default_provider_registry(
        mx_snapshot=mx_snapshot,
        database_path=resolved_db,
        mx_events_database=resolved_mx_events,
        allowed_rids_path=resolved_allowed_rids,
        repository_root=resolved_root,
        research_repository=repository,
        query_staging_dir=store.staging_dir,
    )
    products = DataProductEngine(catalog, registry, store, repository)
    agents = AgentRunner(catalog, active_executor, artifact_store=store, repository=repository)
    pipeline = DecisionPipeline(catalog, active_executor, artifact_store=store)
    cycles = ResearchCycleEngine(catalog, products, agents, pipeline, repository=repository)
    return ResearchRuntime(
        root=resolved_root,
        config=config,
        catalog=catalog,
        policy=policy,
        repository=repository,
        artifact_store=store,
        executor=active_executor,
        product_engine=products,
        agent_runner=agents,
        decision_pipeline=pipeline,
        cycle_engine=cycles,
        catalog_path=configured_catalog,
        database_path=resolved_db,
        artifact_store_path=resolved_artifacts,
        config_path=resolved_config,
    )


def open_control_plane(
    *,
    root: Path | None = None,
    config_path: Path | None = None,
    db_path: Path | None = None,
) -> ResearchControlPlane:
    """Open just enough local state to enqueue or inspect Research work."""
    resolved_root = (root or repo_root()).resolve()
    resolved_config = (config_path or resolved_root / "config" / "advisor.yaml").resolve()
    config = load_advisor_config(resolved_config)
    configured_catalog = resolve_research_catalog(config, resolved_root)
    default_catalog = (resolved_root / "config" / "research").resolve()
    catalog = load_catalog(resolved_root) if configured_catalog == default_catalog else load_catalog_from_directory(configured_catalog)
    database = (db_path or resolve_state_db(config, resolved_root)).resolve()
    return ResearchControlPlane(
        root=resolved_root,
        config=config,
        catalog=catalog,
        repository=ResearchRepository.open(database),
        database_path=database,
    )


def preflight_runtime(
    runtime: ResearchRuntime,
    *,
    output_dir: Path | None = None,
    team_refs: Sequence[str | VersionRef] | None = None,
) -> PreflightResult:
    checks: dict[str, str] = {}
    resolved_output = output_dir.resolve() if output_dir is not None else None
    try:
        runtime.catalog.validate()
        checks["catalog"] = "passed"
        runtime.repository.connection.execute("SELECT 1").fetchone()
        checks["database"] = "passed"
        runtime.artifact_store.root.mkdir(parents=True, exist_ok=True)
        if runtime.artifact_store.root.is_symlink() or not runtime.artifact_store.root.is_dir():
            raise RuntimeError("Artifact Store path must be a real directory")
        checks["artifact_store"] = "passed"
        if resolved_output is not None:
            resolved_output.mkdir(parents=True, exist_ok=True)
            if resolved_output.is_symlink() or not resolved_output.is_dir():
                raise RuntimeError("output directory must be a real directory")
            checks["output"] = "passed"
        else:
            checks["output"] = "not-requested"
        checks.update(_preflight_sources(runtime, team_refs=team_refs))
        preflight = getattr(runtime.executor, "preflight", None)
        if callable(preflight):
            cli_version = str(
                preflight(runtime.policy, probe=True)
                if isinstance(runtime.executor, CodexExecutor)
                else preflight(runtime.policy)
            )
        else:
            cli_version = "not-required"
        checks["local_codex"] = "passed"
        checks["execution_policy"] = "passed"
    except Exception as error:
        return PreflightResult(
            "blocked",
            checks.get("catalog", "failed"),
            checks.get("database", "failed"),
            checks.get("artifact_store", "failed"),
            "",
            str(runtime.policy.policy),
            f"{type(error).__name__}: {str(error)[:240]}",
            catalog_path=str(runtime.catalog_path or ""),
            database_path=str(runtime.database_path or ""),
            artifact_store_path=str(runtime.artifact_store_path or runtime.artifact_store.root),
            output_dir=str(resolved_output or ""),
            checks=checks,
        )
    return PreflightResult(
        "passed",
        "passed",
        "passed",
        "passed",
        cli_version,
        str(runtime.policy.policy),
        catalog_path=str(runtime.catalog_path or ""),
        database_path=str(runtime.database_path or ""),
        artifact_store_path=str(runtime.artifact_store_path or runtime.artifact_store.root),
        output_dir=str(resolved_output or ""),
        checks=checks,
    )


def run_research_cycle(
    runtime: ResearchRuntime,
    *,
    code: str,
    as_of: datetime,
    subject_name: str | None = None,
    team_refs: Sequence[str | VersionRef] | None = None,
    reports_root: Path | None = None,
    cycle_id: str | None = None,
) -> ResearchRun:
    subject = ResearchSubject(code=code, name=subject_name)
    boundary = ResearchBoundary(as_of=as_of)
    teams = [str(VersionRef.parse(item)) for item in (team_refs or runtime.config.research.default_teams)]
    active_cycle_id = cycle_id or _cycle_id(subject, boundary, teams, runtime.policy)
    cycle = runtime.cycle_engine.run_cycle(active_cycle_id, subject, boundary, teams, runtime.policy)
    reporter = CycleReporter((reports_root or runtime.root / "reports"), artifact_store=runtime.artifact_store)
    publication = reporter.publish_or_recover(cycle)
    _persist_publication(runtime, cycle, publication)
    return ResearchRun(cycle, publication)


def run_daily_batch(
    runtime: ResearchRuntime,
    *,
    batch_id: str,
    codes: Sequence[str],
    mx_codes: Sequence[str] = (),
    position_codes: Sequence[str] = (),
    as_of: datetime,
    team_refs: Sequence[str | VersionRef] | None = None,
    reports_root: Path | None = None,
) -> DailyResearchRun:
    selected_teams = runtime.config.research.default_teams if team_refs is None else team_refs
    teams = tuple(str(VersionRef.parse(item)) for item in selected_teams)
    boundary = ResearchBoundary(as_of=as_of)
    if not teams:
        return DailyResearchRun(
            DailyBatchResult(
                batch_id,
                boundary,
                (),
                str(runtime.policy.policy),
                {},
                {},
                "skipped",
            ),
            {},
            {},
        )
    candidates = select_candidates(
        explicit_codes=codes,
        mx_codes=mx_codes,
        position_codes=position_codes,
        max_subjects=runtime.config.research.max_subjects,
    )
    with runtime.repository.transaction():
        runtime.repository.create_batch(
            batch_id=batch_id,
            as_of=boundary.as_of.isoformat(),
            team_refs=list(teams),
            subject_refs=[candidate.subject.code for candidate in candidates],
            execution_policy_ref=str(runtime.policy.policy),
        )
        runtime.repository.set_status("research_batches", "batch_id", batch_id, RunStatus.running.value)
    try:
        batch = DailyResearchBatch(runtime.cycle_engine, max_cycle_concurrency=1).run(
            batch_id,
            candidates,
            teams,
            boundary,
            runtime.policy,
        )
    except Exception as error:
        with runtime.repository.transaction():
            runtime.repository.set_status(
                "research_batches",
                "batch_id",
                batch_id,
                RunStatus.failed.value,
                message=f"{type(error).__name__}: {str(error)[:240]}",
                finished=True,
            )
        raise
    root = reports_root or runtime.root / "reports"
    try:
        reporter = CycleReporter(root, artifact_store=runtime.artifact_store)
        publications = {
            code: reporter.publish_or_recover(cycle)
            for code, cycle in sorted(batch.cycles.items())
        }
        for code, publication in publications.items():
            _persist_publication(runtime, batch.cycles[code], publication)
        renderer = DailyBriefRenderer(root)
        briefs = {team: renderer.render(batch, team) for team in teams}
    except Exception as error:
        with runtime.repository.transaction():
            runtime.repository.set_status(
                "research_batches",
                "batch_id",
                batch_id,
                RunStatus.failed.value,
                message=f"{type(error).__name__}: {str(error)[:240]}",
                finished=True,
            )
        raise
    with runtime.repository.transaction():
        runtime.repository.set_status(
            "research_batches",
            "batch_id",
            batch_id,
            batch.status.value,
            message=None if batch.status == RunStatus.passed else "one or more Subject cycles blocked",
            finished=True,
        )
    return DailyResearchRun(batch, publications, briefs)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run" and not args.team:
            raise ValueError("手动研究必须通过 --team 指定已发布的团队版本")
        if args.command == "preflight":
            runtime = build_runtime(root=args.root, config_path=args.config, db_path=args.db, artifact_dir=args.artifact_dir)
            try:
                result = preflight_runtime(runtime)
            finally:
                runtime.close()
            print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True))
            return 0 if result.status == "passed" else 1
        if args.command == "service":
            return _service_main(args)

        # The CLI and scheduler are submitters.  They intentionally open no
        # Provider, Codex executor, State Machine, or report writer here.
        control = open_control_plane(root=args.root, config_path=args.config, db_path=args.db)
        try:
            as_of = _resolve_as_of(args.as_of, args.date)
            if args.command == "run":
                if len(args.team) != 1:
                    raise ValueError("手动研究一次只能提交一个精确 Team 版本")
                team = control.catalog.team(args.team[0])
                codes = _parse_codes(args.codes, allow_empty=team.scope == ResearchScope.market)
                if team.scope == ResearchScope.security and len(codes) != 1:
                    parser.error("Security Team 的 run 必须提供一个六位 --codes")
                if team.scope == ResearchScope.market and codes:
                    parser.error("Market Team 的 run 不接受 --codes")
                subject = ResearchSubject(
                    scope=team.scope,
                    code=codes[0] if team.scope == ResearchScope.security else None,
                    name=args.name,
                )
                control.catalog.validate_subject_for_team(team.team, subject)
                request = control.repository.submit_request(
                    team_ref=team.team,
                    subject=subject,
                    origin="cli",
                    submission_identity=args.submission_id or f"cli-{uuid4().hex}",
                    requested_at=as_of,
                    accepted_at=as_of,
                )
                control.repository.connection.commit()
                result = _wait_for_request(control.repository, request.request_id, args.wait_seconds)
                payload = _request_payload(result)
            else:
                payload = _submit_scheduled_batch(control, args, as_of)
        finally:
            control.close()
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except SystemExit:
        raise
    except Exception as error:
        print(json.dumps({"status": "failed", "error": type(error).__name__, "message": str(error)[:240]}, ensure_ascii=False, sort_keys=True))
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run A Hunter's self-contained Research Engine.")
    parser.add_argument("--root", type=Path, default=repo_root())
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--artifact-dir", type=Path, default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight", help="validate the catalog, storage, policy, and local Codex session")
    preflight.set_defaults(command="preflight")
    run = subparsers.add_parser("run", help="提交一个明确 Subject 的持久化研究请求")
    run.add_argument("--codes")
    run.add_argument("--name")
    run.add_argument("--team", action="append")
    run.add_argument("--as-of", type=datetime.fromisoformat)
    run.add_argument("--date")
    run.add_argument("--cycle-id")
    run.add_argument("--events-db", type=Path, default=Path("data/state/events.sqlite"))
    run.add_argument("--allowed-rids", type=Path, default=Path("config/allowed-rids.yaml"))
    run.add_argument("--output-dir", type=Path, default=Path("reports"))
    run.add_argument("--submission-id", help="用于可重试传输的稳定提交标识")
    run.add_argument("--wait-seconds", type=float, default=0.0, help="仅轮询控制面，最长等待秒数")
    run.set_defaults(command="run")
    batch = subparsers.add_parser("batch", help="为一次调度 occurrence 提交独立的持久化研究请求")
    batch.add_argument("--codes")
    batch.add_argument("--team", action="append")
    batch.add_argument("--as-of", type=datetime.fromisoformat)
    batch.add_argument("--date")
    batch.add_argument("--batch-id")
    batch.add_argument("--events-db", type=Path, default=Path("data/state/events.sqlite"))
    batch.add_argument("--allowed-rids", type=Path, default=Path("config/allowed-rids.yaml"))
    batch.add_argument("--output-dir", type=Path, default=Path("reports"))
    batch.add_argument("--submission-id", help="用于可重试调度传输的稳定提交标识")
    batch.set_defaults(command="batch")
    service = subparsers.add_parser("service", help="运行或查看唯一的 Research Service")
    service_commands = service.add_subparsers(dest="service_command", required=True)
    service_run = service_commands.add_parser("run", help="认领队列并顺序执行研究请求")
    service_run.add_argument("--poll-seconds", type=float, default=0.5)
    service_run.add_argument("--once", action="store_true", help="最多认领一个请求后退出（仅用于受控运维或测试）")
    service_run.add_argument("--events-db", type=Path, default=Path("data/state/events.sqlite"))
    service_run.add_argument("--allowed-rids", type=Path, default=Path("config/allowed-rids.yaml"))
    service_run.add_argument("--output-dir", type=Path, default=Path("reports"))
    service_run.set_defaults(command="service", service_command="run")
    service_status = service_commands.add_parser("status", help="只读查看队列和服务租约")
    service_status.set_defaults(command="service", service_command="status")
    return parser


def _request_payload(request) -> dict[str, object]:
    """Return the bounded public projection used by the command line."""
    return {
        "request_id": request.request_id,
        "team_ref": str(request.team),
        "scope": request.scope.value,
        "subject": request.subject.model_dump(mode="json"),
        "origin": request.origin,
        "status": request.status,
        "phase": request.phase,
        "requested_at": request.requested_at.isoformat(),
        "accepted_at": request.accepted_at.isoformat(),
        "boundary_at": request.boundary.as_of.isoformat() if request.boundary else None,
        "reason_code": request.reason_code,
        "record_id": request.request_id.replace("request-", "record-", 1)
        if request.status in {"passed", "partial", "blocked", "failed", "cancelled"}
        else None,
    }


def _wait_for_request(repository: ResearchRepository, request_id: str, wait_seconds: float) -> object:
    """Poll only the durable control plane; this is never an execution loop."""
    if not isinstance(wait_seconds, (int, float)) or wait_seconds < 0 or wait_seconds > 86_400:
        raise ValueError("--wait-seconds 必须在 0 到 86400 之间")
    deadline = wall_time.monotonic() + float(wait_seconds)
    request = repository.get_request(request_id)
    while request.status not in {"passed", "partial", "blocked", "failed", "cancelled"} and wall_time.monotonic() < deadline:
        wall_time.sleep(min(0.2, max(0.0, deadline - wall_time.monotonic())))
        request = repository.get_request(request_id)
    return request


def _repository_position_codes(repository: ResearchRepository) -> tuple[str, ...]:
    try:
        rows = repository.connection.execute(
            "SELECT code FROM positions WHERE quantity > 0 ORDER BY code"
        ).fetchall()
    except Exception:
        return ()
    return tuple(str(row[0]) for row in rows if isinstance(row[0], str))


def _submit_scheduled_batch(control: ResearchControlPlane, args: argparse.Namespace, as_of: datetime) -> dict[str, object]:
    """Submit one occurrence worth of work without executing any Research."""
    selected = tuple(args.team or control.config.research.default_teams)
    if not selected:
        return {
            "status": "skipped",
            "message": "当前未启用每日 Team，08:30 批次已跳过",
            "requests": [],
        }
    teams = tuple(control.catalog.team(item) for item in selected)
    raw_codes = _parse_codes(args.codes, allow_empty=True)
    mx_snapshot = _read_optional_mx_snapshot(args.events_db, args.allowed_rids, as_of)
    candidates = select_candidates(
        explicit_codes=raw_codes,
        mx_codes=_mx_codes(mx_snapshot),
        position_codes=_repository_position_codes(control.repository),
        max_subjects=control.config.research.max_subjects,
    )
    occurrence = args.submission_id or args.batch_id or f"scheduled-{as_of.isoformat()}-{uuid4().hex}"
    submitted = []
    for team in teams:
        if team.scope == ResearchScope.market:
            subject = ResearchSubject(scope=ResearchScope.market)
            identity = f"{occurrence}|{team.team}|market"
            request = control.repository.submit_request(
                team_ref=team.team,
                subject=subject,
                origin="scheduled",
                submission_identity=identity,
                requested_at=as_of,
                accepted_at=as_of,
            )
            submitted.append(_request_payload(request))
            continue
        for candidate in candidates:
            code = candidate.subject.code
            if code is None:
                continue
            subject = ResearchSubject(scope=ResearchScope.security, code=code, name=candidate.subject.name)
            identity = f"{occurrence}|{team.team}|security|{code}"
            request = control.repository.submit_request(
                team_ref=team.team,
                subject=subject,
                origin="scheduled",
                submission_identity=identity,
                requested_at=as_of,
                accepted_at=as_of,
            )
            submitted.append(_request_payload(request))
    control.repository.connection.commit()
    return {
        "status": "queued",
        "occurrence_id": occurrence if args.submission_id or args.batch_id else None,
        "requests": submitted,
        "message": "已提交持久化研究请求；Research Service 将按队列顺序执行",
    }


def _service_main(args: argparse.Namespace) -> int:
    if args.service_command == "status":
        control = open_control_plane(root=args.root, config_path=args.config, db_path=args.db)
        try:
            service = ResearchService(control.repository, lambda *_args, **_kwargs: ServiceExecutionResult("blocked"))
            status = service.status()
            print(json.dumps(_service_status_payload(status), ensure_ascii=False, sort_keys=True))
            return 0
        finally:
            control.close()

    if args.poll_seconds <= 0 or args.poll_seconds > 60:
        raise ValueError("--poll-seconds 必须在 0 到 60 之间")
    runtime = build_runtime(
        root=args.root,
        config_path=args.config,
        db_path=args.db,
        artifact_dir=args.artifact_dir,
        mx_events_database=args.events_db,
        allowed_rids_path=args.allowed_rids,
    )
    reports_root = (args.output_dir if args.output_dir.is_absolute() else runtime.root / args.output_dir).resolve()
    service = ResearchService(
        runtime.repository,
        lambda request, boundary, progress, cancelled: _execute_service_request(
            runtime, request, boundary, progress, cancelled, reports_root=reports_root
        ),
    )
    previous_handlers: dict[int, object] = {}

    def stop_handler(_signal: int, _frame: object) -> None:
        service.stop()

    try:
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signal_number] = signal.signal(signal_number, stop_handler)
        if not service.start():
            print(json.dumps({"event": "service_lease_held", "status": "offline"}, ensure_ascii=False, sort_keys=True))
            return 0
        print(json.dumps({"event": "service_started", **_service_status_payload(service.status())}, ensure_ascii=False, sort_keys=True))
        while True:
            claimed = service.tick()
            if args.once:
                return 0
            if service.status().state == "stopping":
                return 0
            if claimed is None or getattr(claimed, "kind", None) == "experiment" and claimed.state in {"running", "waiting"}:
                wall_time.sleep(args.poll_seconds)
    finally:
        for signal_number, handler in previous_handlers.items():
            signal.signal(signal_number, handler)  # type: ignore[arg-type]
        runtime.close()


def _service_status_payload(status: ResearchServiceStatus) -> dict[str, object]:
    return {
        "state": status.state,
        "active_request_id": status.active_request_id,
        "active_test_id": status.active_test_id,
        "queued_count": status.queued_count,
        "heartbeat_at": status.heartbeat_at,
        "reason_code": status.reason_code,
    }


def _execute_service_request(
    runtime: ResearchRuntime,
    request,
    boundary: ResearchBoundary,
    progress,
    cancelled,
    *,
    reports_root: Path,
) -> ServiceExecutionResult:
    """The only in-process bridge from a durable Request to a Cycle.

    This code is reached exclusively after ``ResearchService`` has atomically
    claimed the request.  Submitters never invoke it.
    """
    from advisor.research.execution_settings import request_execution_policy

    try:
        policy = request_execution_policy(runtime, request.request_id)
    except (OSError, ValueError):
        return ServiceExecutionResult(status="blocked", reason_code="preflight_failed")
    from advisor.research.lagent import LAgentRunner
    if getattr(request, "mode", "team") == "lagent":
        return LAgentRunner(runtime, request, boundary, policy, progress, cancelled).execute()
    runtime.catalog.validate_subject_for_team(request.team, request.subject)
    team = runtime.catalog.team(request.team)
    total_agents = len(team.agents)
    if cancelled():
        raise ResearchServiceCancelled()
    preflight = getattr(runtime.executor, "preflight", None)
    if callable(preflight):
        try:
            if isinstance(runtime.executor, CodexExecutor):
                preflight(
                    policy,
                    probe=True,
                    cancel_event=getattr(cancelled, "event", None),
                )
            else:
                preflight(policy)
        except Exception:
            return ServiceExecutionResult(
                status="blocked",
                reason_code="preflight_failed",
            )
    if cancelled():
        raise ResearchServiceCancelled()
    progress("snapshot", 0, total_agents, None)
    cycle_id = f"cycle-{request.request_id.removeprefix('request-')}"
    try:
        cycle = runtime.cycle_engine.run_cycle(
            cycle_id,
            request.subject,
            boundary,
            [request.team],
            policy,
            cancel_event=getattr(cancelled, "event", None),
            progress_callback=progress,
        )
    except ResearchCycleCancelled as error:
        raise ResearchServiceCancelled() from error
    if cancelled():
        raise ResearchServiceCancelled()
    team_result = cycle.team_results[str(request.team)]
    progress("decision", total_agents, total_agents, "publication")
    if team_result.status != RunStatus.passed:
        # A blocked or failed Team has no Team Report, but the immutable
        # diagnostic Cycle remains owner-readable through cycle.json,
        # index.md, status.json and complete.json.  Publish it before the
        # durable Request enters its terminal state so restart recovery uses
        # the same byte-for-byte boundary as successful reports.
        CycleReporter(
            reports_root,
            artifact_store=getattr(runtime, "artifact_store", None),
        ).publish_or_recover(cycle)
    if team_result.status == RunStatus.failed:
        return ServiceExecutionResult(
            status="failed",
            reason_code=team_result.reason_code or "pipeline_failed",
            cycle_id=cycle.cycle_id,
        )
    if team_result.status != RunStatus.passed:
        return ServiceExecutionResult(
            status="blocked",
            reason_code=team_result.reason_code or "quality_blocked",
            cycle_id=cycle.cycle_id,
        )
    publication = CycleReporter(reports_root, artifact_store=runtime.artifact_store).publish_or_recover(cycle)
    market_status = getattr(team_result.conclusion, "status", None) if request.scope == ResearchScope.market else None
    if request.scope == ResearchScope.market:
        team_dir = publication.root / "teams" / str(request.team)
        json_ref = runtime.artifact_store.put_bytes(
            (team_dir / "conclusion.json").read_bytes(), media_type="application/json"
        )
        markdown_ref = runtime.artifact_store.put_bytes(
            (team_dir / "report.md").read_bytes(), media_type="text/markdown; charset=utf-8"
        )
        with runtime.repository.transaction():
            for ref in (json_ref, markdown_ref):
                runtime.repository.record_artifact(
                    ref,
                    relative_path=str(runtime.artifact_store._path_for(ref.content_hash).relative_to(runtime.artifact_store.root)),
                    retention_class="market",
                )
        report_json_hash, report_markdown_hash = json_ref.content_hash, markdown_ref.content_hash
    else:
        _persist_publication(runtime, cycle, publication)
        row = runtime.repository.connection.execute(
            """
            SELECT json_hash, markdown_hash FROM research_reports
            WHERE cycle_id = ? AND team_ref = ? AND report_kind = 'conclusion'
            """,
            (cycle.cycle_id, str(request.team)),
        ).fetchone()
        if row is None or not isinstance(row[0], str) or not isinstance(row[1], str):
            raise RuntimeError("published report artifact is unavailable")
        report_json_hash, report_markdown_hash = row[0], row[1]
    return ServiceExecutionResult(
        status="partial" if market_status == "partial" else "passed",
        cycle_id=cycle.cycle_id,
        report_json_hash=report_json_hash,
        report_markdown_hash=report_markdown_hash,
        published_at=datetime.now(boundary.as_of.tzinfo),
    )


def _resolve_as_of(value: datetime | None, report_date: str | None) -> datetime:
    if value is not None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("--as-of must include a timezone offset")
        return value
    day = date.fromisoformat(report_date) if report_date else datetime.now(_SHANGHAI).date()
    return datetime.combine(day, time(8, 30), tzinfo=_SHANGHAI)


def _parse_codes(raw: str | None, *, allow_empty: bool = False) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        if allow_empty:
            return ()
        raise ValueError("--codes must contain exactly one six-digit A-share code")
    values = tuple(part.strip() for part in raw.split(","))
    if any(not value.isdigit() or len(value) != 6 for value in values):
        raise ValueError("--codes must contain comma-separated six-digit A-share codes")
    return values


def _status_value(status: RunStatus | str) -> str:
    return status.value if isinstance(status, RunStatus) else status


def _mx_codes(snapshot: Any | None) -> tuple[str, ...]:
    if snapshot is None:
        return ()
    found: list[str] = []
    for event in getattr(snapshot, "events", ()):
        text = str(getattr(event, "summary", ""))
        found.extend(re.findall(r"(?<!\d)\d{6}(?!\d)", text))
    return tuple(dict.fromkeys(found))


def _preflight_sources(
    runtime: ResearchRuntime,
    *,
    team_refs: Sequence[str | VersionRef] | None = None,
) -> dict[str, str]:
    seen: set[int] = set()
    selected_teams = tuple(team_refs or runtime.config.research.default_teams)
    for team_ref in selected_teams:
        team = runtime.catalog.team(team_ref)
        for agent_ref in team.agents:
            agent = runtime.catalog.agent(agent_ref)
            for product_ref in agent.product_refs:
                product = runtime.catalog.product(product_ref)
                providers = runtime.product_engine.registry.providers_for(
                    product_ref,
                    provider_ids=product.providers,
                )
                if not product.derived and not providers:
                    raise RuntimeError(f"no Provider Adapter registered for {product_ref}")
                for provider in providers:
                    provider_key = id(provider)
                    if provider_key in seen:
                        continue
                    seen.add(provider_key)
                    check = getattr(provider, "preflight", None)
                    if callable(check):
                        check()
    return {"public_sources": "passed"}


def _write_preflight_status(output_dir: Path, as_of: datetime, result: PreflightResult) -> Path:
    report_day = as_of.astimezone(_SHANGHAI).date().isoformat()
    root = output_dir.resolve() / report_day
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": result.status,
        "as_of": as_of.isoformat(),
        "preflight": result.__dict__,
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
    path = root / f"preflight-{digest}.json"
    if not path.exists():
        with path.open("x", encoding="utf-8") as handle:
            handle.write(serialized)
    return path


def _position_codes(runtime: ResearchRuntime) -> tuple[str, ...]:
    try:
        rows = runtime.repository.connection.execute(
            "SELECT code FROM positions WHERE quantity > 0 ORDER BY code"
        ).fetchall()
    except Exception:
        return ()
    return tuple(row[0] for row in rows if isinstance(row[0], str))


def _read_optional_mx_snapshot(events_db: Path, allowed_rids: Path, as_of: datetime):
    resolved_events = events_db.resolve()
    resolved_rids = allowed_rids.resolve()
    if not resolved_events.is_file() or not resolved_rids.is_file():
        return None
    return read_collector_snapshot(resolved_events, resolved_rids, as_of=as_of)


def _cycle_id(subject: ResearchSubject, boundary: ResearchBoundary, teams: list[str], policy: ExecutionPolicy) -> str:
    digest = content_hash({
        "subject": subject.model_dump(mode="json"),
        "as_of": boundary.as_of.isoformat(),
        "teams": teams,
        "policy": str(policy.policy),
    })[:10]
    subject_key = subject.code or subject.scope.value
    return f"cycle-{boundary.as_of.astimezone(_SHANGHAI):%Y%m%d%H%M}-{subject_key}-{digest}"


def _persist_publication(runtime: ResearchRuntime, cycle: ResearchCycleResult, publication: CyclePublication) -> None:
    if runtime.repository.connection.execute(
        "SELECT 1 FROM research_cycles WHERE cycle_id = ?", (cycle.cycle_id,)
    ).fetchone() is None:
        return

    def save(path: Path, media_type: str) -> str:
        ref = runtime.artifact_store.put_bytes(path.read_bytes(), media_type=media_type)
        relative = str(runtime.artifact_store._path_for(ref.content_hash).relative_to(runtime.artifact_store.root))
        with runtime.repository.transaction():
            runtime.repository.record_artifact(ref, relative_path=relative)
        return ref.content_hash

    cycle_hash = save(publication.cycle_json, "application/json")
    index_hash = save(publication.index, "text/markdown; charset=utf-8")
    save(publication.complete_marker, "application/json")
    with runtime.repository.transaction():
        runtime.repository.record_report(
            report_id=f"{cycle.cycle_id}:cycle-index",
            cycle_id=cycle.cycle_id,
            team_ref="cycle_index@1",
            report_kind="cycle_index",
            json_hash=cycle_hash,
            markdown_hash=index_hash,
            status="complete",
        )
        for team_ref, result in cycle.team_results.items():
            team_dir = publication.root / "teams" / team_ref
            if result.status == RunStatus.passed:
                json_hash = save(team_dir / "conclusion.json", "application/json")
                markdown_hash = save(team_dir / "report.md", "text/markdown; charset=utf-8")
                runtime.repository.record_report(
                    report_id=f"{cycle.cycle_id}:{team_ref}:conclusion",
                    cycle_id=cycle.cycle_id,
                    team_ref=team_ref,
                    report_kind="conclusion",
                    json_hash=json_hash,
                    markdown_hash=markdown_hash,
                    status="complete",
                )
            else:
                status_hash = save(team_dir / "status.json", "application/json")
                runtime.repository.record_report(
                    report_id=f"{cycle.cycle_id}:{team_ref}:blocked",
                    cycle_id=cycle.cycle_id,
                    team_ref=team_ref,
                    report_kind="blocked",
                    json_hash=status_hash,
                    markdown_hash=None,
                    status="blocked",
                )


if __name__ == "__main__":
    raise SystemExit(main())
