from __future__ import annotations

from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import inspect
import json
from typing import Any, Callable

from advisor.research.catalog import ManifestCatalog
from advisor.research.artifacts import ArtifactRef
from advisor.research.contracts import ExecutionPolicy, FindingQuality, ResearchBoundary, ResearchScope, ResearchSubject, RunStatus, TeamConclusion, VersionRef, content_hash
from advisor.research.agents.runner import (
    AgentFindingError,
    AgentInputUnavailable,
    invocation_key,
    visible_input_artifacts,
    visible_input_hashes,
)
from advisor.research.data_products.engine import SnapshotResult
from advisor.research.decision.contracts import StageReview, TraderProposal
from advisor.research.decision.pipeline import DecisionPipelineCancelled, PipelineResult, StageResult, TeamBlocked
from advisor.research.decision.market import MarketPipelineResult, MarketTeamBlocked


class InvalidTransition(RuntimeError):
    pass


class ResearchCycleCancelled(RuntimeError):
    pass


_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.pending: frozenset({RunStatus.running, RunStatus.blocked, RunStatus.failed, RunStatus.cancelled}),
    RunStatus.running: frozenset({RunStatus.passed, RunStatus.blocked, RunStatus.failed, RunStatus.cancelled}),
    RunStatus.passed: frozenset(),
    RunStatus.blocked: frozenset(),
    RunStatus.failed: frozenset(),
    RunStatus.cancelled: frozenset(),
}


def transition(current: RunStatus, target: RunStatus) -> RunStatus:
    if target not in _TRANSITIONS[current]:
        raise InvalidTransition(f"cannot transition {current.value} -> {target.value}")
    return target


@dataclass(frozen=True)
class TeamRunResult:
    team: VersionRef
    status: RunStatus
    conclusion: Any | None = None
    message: str | None = None
    agent_errors: dict[str, str] = field(default_factory=dict)
    reason_code: str | None = None


@dataclass(frozen=True)
class ResearchCycleResult:
    cycle_id: str
    subject: ResearchSubject
    boundary: ResearchBoundary
    status: RunStatus
    fingerprint: str
    snapshot: SnapshotResult | None
    agent_results: dict[str, Any]
    agent_errors: dict[str, str]
    team_results: dict[str, TeamRunResult]
    team_findings: dict[str, dict[str, Any]] = field(default_factory=dict)


class ResearchCycleEngine:
    """A Hunter-owned, replay-safe orchestrator for one Subject boundary."""

    def __init__(self, catalog: ManifestCatalog, product_engine, agent_runner, decision_pipeline, *, repository=None) -> None:
        self.catalog = catalog
        self.product_engine = product_engine
        self.agent_runner = agent_runner
        self.decision_pipeline = decision_pipeline
        self.repository = repository
        self._completed: dict[str, ResearchCycleResult] = {}
        self._signatures: dict[str, str] = {}

    def run_cycle(
        self,
        cycle_id: str,
        subject: ResearchSubject,
        boundary: ResearchBoundary,
        team_refs: list[str | VersionRef],
        policy: ExecutionPolicy,
        *,
        batch_id: str | None = None,
        cancel_event: Any | None = None,
        progress_callback: Callable[[str, int, int, str | None], None] | None = None,
    ) -> ResearchCycleResult:
        _raise_if_cancelled(cancel_event)
        if not cycle_id or not team_refs:
            raise ValueError("cycle_id and at least one Team are required")
        normalized_teams = tuple(str(VersionRef.parse(ref)) for ref in team_refs)
        signature = self._input_signature(subject, boundary, normalized_teams, policy)
        if cycle_id in self._completed:
            if self._signatures[cycle_id] != signature:
                raise ValueError("cycle_id was already used with different inputs")
            return self._completed[cycle_id]
        for team_ref in normalized_teams:
            self.catalog.validate_subject_for_team(team_ref, subject)

        existing_cycle_status = None
        if self.repository is not None and not subject.is_market:
            existing_cycle = self.repository.connection.execute(
                "SELECT status FROM research_cycles WHERE cycle_id = ?", (cycle_id,)
            ).fetchone()
            existing_cycle_status = existing_cycle[0] if existing_cycle else None
            with self.repository.transaction():
                self.repository.create_cycle(
                    cycle_id=cycle_id,
                    subject_code=subject.code,
                    subject_name=subject.name,
                    as_of=boundary.as_of.isoformat(),
                    fingerprint=signature,
                    batch_id=batch_id,
                )
                for team_ref in normalized_teams:
                    self.repository.create_run(run_id=f"{cycle_id}:{team_ref}", cycle_id=cycle_id, team_ref=team_ref)
                if existing_cycle_status not in {item.value for item in (RunStatus.passed, RunStatus.blocked, RunStatus.failed, RunStatus.cancelled)}:
                    self.repository.set_status("research_cycles", "cycle_id", cycle_id, RunStatus.running.value)
                for team_ref in normalized_teams:
                    run_id = f"{cycle_id}:{team_ref}"
                    run_status = self.repository.connection.execute(
                        "SELECT status FROM research_runs WHERE research_run_id = ?",
                        (run_id,),
                    ).fetchone()
                    if run_status is not None and run_status[0] not in {
                        item.value for item in (RunStatus.passed, RunStatus.blocked, RunStatus.failed, RunStatus.cancelled)
                    }:
                        self.repository.set_status("research_runs", "research_run_id", run_id, RunStatus.running.value)

        snapshot: SnapshotResult | None = None
        agent_results: dict[str, Any] = {}
        agent_errors: dict[str, str] = {}
        team_results: dict[str, TeamRunResult] = {}
        try:
            snapshot = self.product_engine.build_snapshot(
                cycle_id=cycle_id,
                subject=subject,
                boundary=boundary,
                team_refs=list(normalized_teams),
            )
            _raise_if_cancelled(cancel_event)
        except ResearchCycleCancelled:
            raise
        except Exception as error:
            message = f"Snapshot blocked: {type(error).__name__}"
            for team_ref in normalized_teams:
                team_results[team_ref] = TeamRunResult(
                    VersionRef.parse(team_ref),
                    RunStatus.blocked,
                    message=message,
                    reason_code="snapshot_unavailable",
                )
            result = ResearchCycleResult(cycle_id, subject, boundary, RunStatus.blocked, signature, None, {}, {"snapshot": message}, team_results)
            return self._finish(cycle_id, signature, result)

        all_agents = sorted({str(agent) for team_ref in normalized_teams for agent in self.catalog.team(team_ref).agents})
        _notify_progress(progress_callback, "agents", 0, len(all_agents), None)
        snapshot_agent_errors = _snapshot_agent_errors(
            self.catalog,
            self.agent_runner,
            all_agents,
            snapshot,
        )
        agent_errors.update(snapshot_agent_errors)
        agent_error_classes = {agent_ref: "snapshot_unavailable" for agent_ref in snapshot_agent_errors}

        # A Security Team is all-or-nothing.  If one required Agent cannot
        # receive its declared Snapshot products, do not spend model calls on
        # the rest of that Team.  An Agent shared with another viable Team is
        # still allowed to run for that independent consumer.  Market Teams
        # retain their explicit partial-report behavior.
        preblocked_teams: set[str] = set()
        for team_ref in normalized_teams:
            team = self.catalog.team(team_ref)
            unavailable = [str(agent) for agent in team.agents if str(agent) in snapshot_agent_errors]
            if unavailable and team.scope == ResearchScope.security:
                preblocked_teams.add(team_ref)
                team_results[team_ref] = TeamRunResult(
                    team.team,
                    RunStatus.blocked,
                    message=f"required Snapshot input unavailable: {', '.join(sorted(unavailable))}",
                    agent_errors={agent: snapshot_agent_errors[agent] for agent in unavailable},
                    reason_code="snapshot_unavailable",
                )
        needed_agents = {
            str(agent)
            for team_ref in normalized_teams
            if team_ref not in preblocked_teams
            for agent in self.catalog.team(team_ref).agents
        }
        # A Market invocation has no synthetic Security code, so it is stored
        # in the explicit-scope audit tables.  Its terminal result is still
        # immutable.  When a worker is restarted after an Agent has already
        # failed, retain that fact and let the fixed Market Pipeline publish a
        # partial report from the remaining Insights instead of trying to
        # transition the failed invocation back to running.
        agents_to_run = sorted(needed_agents - set(snapshot_agent_errors))
        agents_to_audit = sorted(set(agents_to_run) | set(snapshot_agent_errors))
        if self.repository is not None:
            with self.repository.transaction():
                for agent_ref in agents_to_audit:
                    agent = self.catalog.agent(agent_ref)
                    # Keep preflight failures auditable even though unavailable
                    # inputs never reach AgentRunner. Unrelated Teams continue.
                    input_hashes = list(visible_input_hashes(agent, snapshot))
                    key = invocation_key(
                        agent_ref,
                        snapshot,
                        policy,
                        input_products=agent.product_refs,
                        input_artifacts=visible_input_artifacts(agent, snapshot),
                    )
                    if subject.is_market:
                        self.repository.create_scope_invocation(
                            invocation_key=key,
                            cycle_id=cycle_id,
                            subject=subject,
                            agent_ref=agent_ref,
                            as_of=boundary.as_of.isoformat(),
                            input_hashes=input_hashes,
                            policy_ref=str(policy.policy),
                        )
                        persisted = self.repository.scope_invocation_cache(key)
                        if persisted is not None and persisted[0] in {
                            RunStatus.blocked.value,
                            RunStatus.failed.value,
                            RunStatus.cancelled.value,
                        }:
                            diagnostic = self.repository.scope_invocation_diagnostic(key)
                            persisted_message = diagnostic[1] if diagnostic is not None else None
                            agent_errors[agent_ref] = persisted_message or (
                                f"Recovered terminal Agent invocation: {persisted[0]}"
                            )
                            agent_error_classes.setdefault(agent_ref, "agent_invalid")
                            if agent_ref in agents_to_run:
                                agents_to_run.remove(agent_ref)
                            continue
                        self.repository.start_scope_invocation(key)
                    else:
                        self.repository.create_invocation(
                            invocation_key=key,
                            cycle_id=cycle_id,
                            agent_ref=agent_ref,
                            subject_code=subject.code,
                            as_of=boundary.as_of.isoformat(),
                            input_hashes=input_hashes,
                            policy_ref=str(policy.policy),
                        )
                        invocation_row = self.repository.connection.execute(
                            "SELECT status FROM research_invocations WHERE invocation_key = ?", (key,)
                        ).fetchone()
                        if not invocation_row or invocation_row[0] != RunStatus.passed.value:
                            self.repository.set_status("research_invocations", "invocation_key", key, RunStatus.running.value)
        for agent_ref, message in snapshot_agent_errors.items():
            self._persist_agent_failure(
                agent_ref,
                snapshot,
                policy,
                message,
                error_class="snapshot_unavailable",
            )
        completed_agents = set(all_agents) - set(agents_to_run)
        _notify_progress(progress_callback, "agents", len(completed_agents), len(all_agents), None)
        _raise_if_cancelled(cancel_event)
        with ThreadPoolExecutor(max_workers=max(1, min(len(agents_to_run), policy.max_agent_concurrency)), thread_name_prefix="a-hunter-agent") as pool:
            runner_kwargs = {"cancel_event": cancel_event} if cancel_event is not None else {}
            futures = {
                pool.submit(self.agent_runner.run, agent_ref, snapshot, policy, **runner_kwargs): agent_ref
                for agent_ref in agents_to_run
            }
            cancellation_observed = False
            for future in as_completed(futures):
                agent_ref = futures[future]
                try:
                    result = future.result()
                    if not self._invocation_is_passed(result.invocation_key):
                        self._persist_agent_success(result, policy)
                    agent_results[agent_ref] = result
                except Exception as error:
                    cancelled = (
                        _is_cancelled(cancel_event)
                        or isinstance(error, (CancelledError, ResearchCycleCancelled))
                        or getattr(error, "error_class", None) == "cancelled"
                    )
                    if cancelled:
                        cancellation_observed = True
                        for pending in futures:
                            pending.cancel()
                    agent_errors[agent_ref] = f"{type(error).__name__}: {str(error)[:240]}"
                    agent_error_classes[agent_ref] = str(
                        "cancelled"
                        if cancelled
                        else getattr(error, "error_class", None) or type(error).__name__
                    )
                    self._persist_agent_failure(
                        agent_ref,
                        snapshot,
                        policy,
                        agent_errors[agent_ref],
                        attempts=tuple(getattr(error, "attempts", ())),
                        capsule_hash=getattr(error, "capsule_hash", None),
                        error_class=getattr(error, "error_class", None),
                        query_log_hash=getattr(error, "query_log_hash", None),
                        query_capsule_hash=getattr(error, "query_capsule_hash", None),
                        terminal_status=(
                            RunStatus.cancelled.value if cancelled else RunStatus.failed.value
                        ),
                    )
                if _is_cancelled(cancel_event):
                    cancellation_observed = True
                    for pending in futures:
                        pending.cancel()
                completed_agents.add(agent_ref)
                _notify_progress(progress_callback, "agents", len(completed_agents), len(all_agents), None)
            if cancellation_observed:
                raise ResearchCycleCancelled()

        for team_ref in normalized_teams:
            _raise_if_cancelled(cancel_event)
            if team_ref in team_results:
                continue
            team = self.catalog.team(team_ref)
            missing = [str(agent) for agent in team.agents if str(agent) not in agent_results]
            if missing and team.scope != ResearchScope.market:
                team_results[team_ref] = TeamRunResult(
                    team.team,
                    RunStatus.blocked,
                    message=f"required Agent failure: {', '.join(sorted(missing))}",
                    agent_errors={agent: agent_errors[agent] for agent in missing if agent in agent_errors},
                    reason_code=_agent_failure_reason(missing, agent_errors, agent_error_classes),
                )
                continue
            findings = {
                agent_ref: agent_results[agent_ref].finding
                for agent_ref in (str(agent) for agent in team.agents)
                if agent_ref in agent_results
            }
            if team.scope == ResearchScope.market:
                try:
                    pipeline_result = self._run_pipeline(
                        cycle_id,
                        team_ref,
                        snapshot,
                        findings,
                        policy,
                        agent_errors={agent: agent_errors[agent] for agent in missing if agent in agent_errors},
                        cancel_event=cancel_event,
                    )
                except MarketTeamBlocked as error:
                    team_results[team_ref] = TeamRunResult(
                        team.team,
                        RunStatus.blocked,
                        message=f"Market Pipeline blocked: {type(error).__name__}: {str(error)[:200]}",
                        agent_errors={agent: agent_errors[agent] for agent in missing if agent in agent_errors},
                        reason_code=getattr(error, "reason_code", "pipeline_blocked"),
                    )
                except Exception as error:
                    if _is_cancelled(cancel_event) or isinstance(error, DecisionPipelineCancelled):
                        raise ResearchCycleCancelled() from error
                    team_results[team_ref] = TeamRunResult(
                        team.team,
                        RunStatus.failed,
                        message=f"Market Pipeline failed: {type(error).__name__}: {str(error)[:200]}",
                        agent_errors={agent: agent_errors[agent] for agent in missing if agent in agent_errors},
                        reason_code="pipeline_failed",
                    )
                else:
                    if not isinstance(pipeline_result, MarketPipelineResult) or pipeline_result.status == "blocked":
                        team_results[team_ref] = TeamRunResult(
                            team.team,
                            RunStatus.blocked,
                            message="Market Pipeline 没有可发布的 Insight",
                            agent_errors={agent: agent_errors[agent] for agent in missing if agent in agent_errors},
                            reason_code=_market_block_reason(pipeline_result),
                        )
                    else:
                        team_results[team_ref] = TeamRunResult(team.team, RunStatus.passed, conclusion=pipeline_result)
                continue
            cached_pipeline = self._load_pipeline(cycle_id, team_ref, snapshot)
            if cached_pipeline is not None:
                team_results[team_ref] = TeamRunResult(team.team, RunStatus.passed, conclusion=cached_pipeline)
                continue
            try:
                pipeline_result = self._run_pipeline(
                    cycle_id, team_ref, snapshot, findings, policy, cancel_event=cancel_event
                )
            except Exception as error:
                if _is_cancelled(cancel_event) or isinstance(error, DecisionPipelineCancelled):
                    raise ResearchCycleCancelled() from error
                team_results[team_ref] = TeamRunResult(
                    team.team,
                    RunStatus.blocked,
                    message=f"Decision Pipeline blocked: {type(error).__name__}: {str(error)[:200]}",
                    reason_code="pipeline_blocked",
                )
                self._persist_pipeline_failure(cycle_id, team_ref, error, policy)
            else:
                team_results[team_ref] = TeamRunResult(team.team, RunStatus.passed, conclusion=pipeline_result)
                self._persist_pipeline(cycle_id, team_ref, pipeline_result, policy)

        cycle_status = (
            RunStatus.failed
            if any(item.status == RunStatus.failed for item in team_results.values())
            else RunStatus.passed
            if any(item.status == RunStatus.passed for item in team_results.values())
            else RunStatus.blocked
        )
        fingerprint = content_hash(
            {
                "inputs": signature,
                "snapshot": snapshot.snapshot_hash,
                "agents": {
                    key: getattr(value, "output_hash", None)
                    for key, value in sorted(agent_results.items())
                },
                "teams": {
                    key: {
                        "status": value.status.value,
                        "conclusion": _conclusion_payload(value.conclusion),
                    }
                    for key, value in sorted(team_results.items())
                },
            }
        )
        team_findings = {
            team_ref: {
                agent_ref: agent_results[agent_ref].finding
                for agent_ref in (str(agent) for agent in self.catalog.team(team_ref).agents)
                if agent_ref in agent_results
            }
            for team_ref in normalized_teams
        }
        result = ResearchCycleResult(
            cycle_id,
            subject,
            boundary,
            cycle_status,
            fingerprint,
            snapshot,
            agent_results,
            agent_errors,
            team_results,
            team_findings,
        )
        return self._finish(cycle_id, signature, result)

    def _run_pipeline(
        self,
        cycle_id: str,
        team_ref: str,
        snapshot: SnapshotResult,
        findings: dict[str, Any],
        policy: ExecutionPolicy,
        *,
        agent_errors: dict[str, str] | None = None,
        cancel_event: Any | None = None,
    ) -> PipelineResult | MarketPipelineResult:
        run = self.decision_pipeline.run
        try:
            parameters = inspect.signature(run).parameters
        except (TypeError, ValueError):
            parameters = {}
        if "completed_stages" not in parameters or "stage_callback" not in parameters:
            if "agent_errors" in parameters:
                kwargs = {"agent_errors": agent_errors}
                if "cancel_event" in parameters:
                    kwargs["cancel_event"] = cancel_event
                return run(team_ref, snapshot, findings, policy, **kwargs)
            kwargs = {"cancel_event": cancel_event} if "cancel_event" in parameters else {}
            return run(team_ref, snapshot, findings, policy, **kwargs)
        completed = self._load_stage_cache(cycle_id, team_ref)
        def callback(stage):
            _raise_if_cancelled(cancel_event)
            self._persist_stage(cycle_id, team_ref, stage, policy)
        kwargs = {
            "completed_stages": completed,
            "stage_callback": callback,
        }
        if "agent_errors" in parameters:
            kwargs["agent_errors"] = agent_errors
        if "cancel_event" in parameters:
            kwargs["cancel_event"] = cancel_event
        return run(
            team_ref,
            snapshot,
            findings,
            policy,
            **kwargs,
        )
    def _input_signature(
        self,
        subject: ResearchSubject,
        boundary: ResearchBoundary,
        team_refs: tuple[str, ...],
        policy: ExecutionPolicy,
    ) -> str:
        teams = []
        products: dict[str, Any] = {}
        for team_ref in sorted(team_refs):
            team = self.catalog.team(team_ref)
            agents = []
            for agent_ref in team.agents:
                agent = self.catalog.agent(agent_ref)
                agents.append(agent.model_dump(mode="json"))
                for product_ref in agent.product_refs:
                    product = self.catalog.product(product_ref)
                    products[str(product_ref)] = product.model_dump(mode="json")
            teams.append({"team": team.model_dump(mode="json"), "agents": agents})
        pipeline = getattr(self.decision_pipeline, "pipeline", None)
        return content_hash({
            "subject": subject.model_dump(mode="json"),
            "as_of": boundary.as_of.isoformat(),
            "teams": teams,
            "products": products,
            "pipeline": pipeline.model_dump(mode="json") if pipeline is not None else None,
            "policy": policy.model_dump(mode="json"),
        })

    def _load_pipeline(self, cycle_id: str, team_ref: str, snapshot: SnapshotResult) -> PipelineResult | None:
        if self.catalog.team(team_ref).scope == ResearchScope.market:
            return None
        if self.repository is None:
            return None
        store = getattr(self.decision_pipeline, "artifact_store", None)
        if store is None:
            return None
        run_id = f"{cycle_id}:{team_ref}"
        row = self.repository.connection.execute(
            "SELECT status, conclusion_hash FROM research_runs WHERE research_run_id = ?", (run_id,)
        ).fetchone()
        if row is None or row[0] != RunStatus.passed.value or not isinstance(row[1], str):
            return None
        stages = self._load_stage_cache(cycle_id, team_ref)
        if len(stages) != len(self.decision_pipeline.pipeline.stages):
            return None
        try:
            conclusion = stages["portfolio_manager"].output
            if not isinstance(conclusion, TeamConclusion):
                return None
            if conclusion.subject != snapshot.subject or conclusion.boundary != snapshot.boundary:
                return None
            return PipelineResult(
                VersionRef.parse(team_ref),
                snapshot.subject,
                snapshot.boundary,
                conclusion,
                stages,
                self.decision_pipeline.pipeline.pipeline,
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def _load_stage_cache(self, cycle_id: str, team_ref: str) -> dict[str, StageResult]:
        if self.repository is None:
            return {}
        store = getattr(self.decision_pipeline, "artifact_store", None)
        if store is None:
            return {}
        run_id = f"{cycle_id}:{team_ref}"
        rows = self.repository.connection.execute(
            """
            SELECT stage_name, input_hashes_json, output_hash
            FROM research_stage_runs
            WHERE research_run_id = ? AND status = 'passed' AND output_hash IS NOT NULL
            ORDER BY stage_order
            """,
            (run_id,),
        ).fetchall()
        stages: dict[str, StageResult] = {}
        for stage_name, input_hashes_json, output_hash in rows:
            if stage_name not in self.decision_pipeline.pipeline.stages or not store.verify(output_hash):
                continue
            try:
                value = store.read_json(output_hash)
                if stage_name in {"finding_quality", "publication_quality"}:
                    output = FindingQuality.model_validate(value)
                elif stage_name == "trader":
                    output = TraderProposal.model_validate(value)
                elif stage_name == "portfolio_manager":
                    output = TeamConclusion.model_validate(value)
                else:
                    output = StageReview.model_validate(value)
                input_hashes = json.loads(input_hashes_json)
                attempt = self.repository.connection.execute(
                    """
                    SELECT capsule_hash FROM research_stage_attempts
                    WHERE stage_run_id = ? AND status = 'passed'
                    ORDER BY attempt_number DESC LIMIT 1
                    """,
                    (f"{run_id}:{stage_name}",),
                ).fetchone()
                stages[stage_name] = StageResult(
                    stage_name,
                    output,
                    input_hashes[0] if input_hashes else "",
                    attempt[0] if attempt else None,
                    output_hash,
                )
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
        return stages

    def _persist_agent_success(self, result: Any, policy: ExecutionPolicy) -> None:
        if self.repository is None:
            return
        key = result.invocation_key
        attempts = tuple(getattr(result, "attempts", ()))
        capsule_hash = getattr(result, "capsule_hash", None)
        output_hash = getattr(result, "output_hash", None)
        required_hashes = [capsule_hash, output_hash]
        for optional_name in ("query_log_hash", "query_capsule_hash"):
            optional_hash = getattr(result, optional_name, None)
            if optional_hash is not None:
                required_hashes.append(optional_hash)
        for attempt in attempts:
            attempt_capsule_hash = getattr(attempt, "capsule_hash", None)
            attempt_output_hash = getattr(attempt, "output_hash", None)
            if attempt_capsule_hash is not None:
                required_hashes.append(attempt_capsule_hash)
            if getattr(attempt, "status", "passed") == "passed" and attempt_output_hash is not None:
                required_hashes.append(attempt_output_hash)
        if any(
            not isinstance(required_hash, str) or not self._artifact_exists(required_hash)
            for required_hash in required_hashes
        ):
            raise AgentFindingError(
                "Agent success audit artifact is unavailable",
                attempts=attempts,
                capsule_hash=capsule_hash if isinstance(capsule_hash, str) else None,
                error_class="audit_persistence_failed",
                query_log_hash=getattr(result, "query_log_hash", None),
                query_capsule_hash=getattr(result, "query_capsule_hash", None),
            )
        with self.repository.transaction():
            self._persist_attempt_diagnostics(key, attempts)
            self._record_artifact(capsule_hash, media_type="application/vnd.a-hunter.run-capsule+json")
            if isinstance(getattr(result, "output_hash", None), str):
                self._record_artifact(result.output_hash, media_type="application/vnd.a-hunter.research-finding+json")
            if isinstance(getattr(result, "query_log_hash", None), str):
                self._record_artifact(result.query_log_hash, media_type="application/vnd.a-hunter.query-log+jsonl")
            if isinstance(getattr(result, "query_capsule_hash", None), str):
                self._record_artifact(
                    result.query_capsule_hash,
                    media_type="application/vnd.a-hunter.run-capsule+json",
                )
            if getattr(getattr(result, "finding", None), "subject", None) is not None and result.finding.subject.is_market:
                if not attempts:
                    attempts = (type("Attempt", (), {"attempt_number": 1, "status": "passed", "duration_ms": 0})(),)
                for attempt in attempts:
                    attempt_capsule_hash = getattr(attempt, "capsule_hash", None)
                    if not isinstance(attempt_capsule_hash, str) or not self._artifact_exists(attempt_capsule_hash):
                        attempt_capsule_hash = capsule_hash
                    elif attempt_capsule_hash != capsule_hash:
                        self._record_artifact(
                            attempt_capsule_hash,
                            media_type="application/vnd.a-hunter.run-capsule+json",
                        )
                    attempt_output_hash = getattr(attempt, "output_hash", None)
                    if isinstance(attempt_output_hash, str) and self._artifact_exists(attempt_output_hash):
                        self._record_artifact(
                            attempt_output_hash,
                            media_type=getattr(attempt, "output_media_type", None) or "application/json",
                        )
                    elif getattr(attempt, "phase", "finding") == "finding":
                        attempt_output_hash = output_hash
                    else:
                        attempt_output_hash = None
                    attempt_policy = policy.model_dump(mode="json")
                    attempt_policy["execution_phase"] = getattr(attempt, "phase", "finding")
                    attempt_id = self.repository.create_scope_attempt(
                        invocation_key=key,
                        attempt_number=int(attempt.attempt_number),
                        capsule_hash=attempt_capsule_hash,
                        policy_json=attempt_policy,
                        cli_version=getattr(attempt, "cli_version", None) or getattr(result, "cli_version", None),
                        model=getattr(attempt, "model", None) or getattr(result, "model", None) or policy.model,
                        reasoning_effort=getattr(attempt, "reasoning_effort", None) or getattr(result, "reasoning_effort", None) or policy.reasoning_effort,
                        usage_json=getattr(attempt, "usage", None) or getattr(result, "usage", None) or {},
                    )
                    accepted = getattr(attempt, "status", "passed") == "passed"
                    self.repository.finish_scope_attempt(
                        attempt_id=attempt_id,
                        status="passed" if accepted else "failed",
                        output_hash=attempt_output_hash if accepted else None,
                        error_class=None if accepted else getattr(attempt, "error_class", "technical_failure"),
                        duration_ms=int(getattr(attempt, "duration_ms", 0)),
                    )
                self.repository.record_scope_finding(
                    key,
                    output_hash=output_hash,
                    query_log_hash=getattr(result, "query_log_hash", None),
                )
                return
            if not attempts:
                attempts = (type("Attempt", (), {"attempt_number": 1, "status": "passed", "duration_ms": 0})(),)
            for attempt in attempts:
                attempt_capsule_hash = getattr(attempt, "capsule_hash", None)
                if not isinstance(attempt_capsule_hash, str) or not self._artifact_exists(attempt_capsule_hash):
                    attempt_capsule_hash = capsule_hash
                elif attempt_capsule_hash != capsule_hash:
                    self._record_artifact(
                        attempt_capsule_hash,
                        media_type="application/vnd.a-hunter.run-capsule+json",
                    )
                attempt_output_hash = getattr(attempt, "output_hash", None)
                if isinstance(attempt_output_hash, str) and self._artifact_exists(attempt_output_hash):
                    self._record_artifact(
                        attempt_output_hash,
                        media_type=(
                            getattr(attempt, "output_media_type", None)
                            or "application/vnd.a-hunter.research-finding+json"
                        ),
                    )
                elif getattr(attempt, "phase", "finding") == "finding":
                    attempt_output_hash = output_hash
                else:
                    attempt_output_hash = None
                attempt_policy = policy.model_dump(mode="json")
                attempt_policy["execution_phase"] = getattr(attempt, "phase", "finding")
                attempt_id = self.repository.create_attempt(
                    invocation_key=key,
                    attempt_number=int(attempt.attempt_number),
                    capsule_hash=attempt_capsule_hash,
                    policy_json=attempt_policy,
                    cli_version=getattr(attempt, "cli_version", None) or getattr(result, "cli_version", None),
                    model=getattr(attempt, "model", None) or getattr(result, "model", None) or policy.model,
                    reasoning_effort=getattr(attempt, "reasoning_effort", None) or getattr(result, "reasoning_effort", None) or policy.reasoning_effort,
                    usage_json=getattr(attempt, "usage", None) or getattr(result, "usage", None) or {},
                )
                accepted = getattr(attempt, "status", "passed") == "passed"
                self.repository.finish_attempt(
                    attempt_id=attempt_id,
                    status="passed" if accepted else "failed",
                    output_hash=attempt_output_hash if accepted else None,
                    error_class=None if accepted else getattr(attempt, "error_class", "technical_failure"),
                    duration_ms=int(getattr(attempt, "duration_ms", 0)),
                )
            self.repository.record_finding(
                key,
                output_hash=output_hash,
                query_log_hash=getattr(result, "query_log_hash", None),
            )

    def _invocation_is_passed(self, invocation_key_value: str) -> bool:
        if self.repository is None:
            return False
        row = self.repository.connection.execute(
            "SELECT status FROM research_invocations WHERE invocation_key = ?",
            (invocation_key_value,),
        ).fetchone()
        if row and row[0] == RunStatus.passed.value:
            return True
        if not hasattr(self.repository, "scope_invocation_cache"):
            return False
        scope_row = self.repository.scope_invocation_cache(invocation_key_value)
        return bool(scope_row and scope_row[0] == RunStatus.passed.value)

    def _persist_agent_failure(
        self,
        agent_ref: str,
        snapshot: SnapshotResult,
        policy: ExecutionPolicy,
        message: str,
        *,
        attempts: tuple[Any, ...] = (),
        capsule_hash: str | None = None,
        error_class: str | None = None,
        query_log_hash: str | None = None,
        query_capsule_hash: str | None = None,
        terminal_status: str = RunStatus.failed.value,
    ) -> None:
        if self.repository is None:
            return
        if terminal_status not in {RunStatus.failed.value, RunStatus.cancelled.value}:
            raise ValueError("Agent failure terminal status is invalid")
        agent = self.catalog.agent(agent_ref)
        key = invocation_key(
            agent_ref,
            snapshot,
            policy,
            input_products=agent.product_refs,
            input_artifacts=visible_input_artifacts(agent, snapshot),
        )
        store = getattr(self.agent_runner, "artifact_store", None)
        available_query_log_hash = (
            query_log_hash
            if store is not None and isinstance(query_log_hash, str) and store.verify(query_log_hash)
            else None
        )
        available_query_capsule_hash = (
            query_capsule_hash
            if store is not None and isinstance(query_capsule_hash, str) and store.verify(query_capsule_hash)
            else None
        )
        self._persist_attempt_diagnostics(key, attempts)
        if snapshot.subject.is_market:
            with self.repository.transaction():
                persisted = self.repository.scope_invocation_cache(key)
                if persisted is not None and persisted[0] in {
                    RunStatus.passed.value,
                    RunStatus.blocked.value,
                    RunStatus.failed.value,
                    RunStatus.cancelled.value,
                }:
                    # A replay can discover that an already-passed artifact is
                    # corrupt.  Preserve its immutable audit state while the
                    # Market Pipeline receives the current Agent error and
                    # fails closed/partially publishes as appropriate.
                    return
                if store is not None:
                    failure_ref = store.put_json(
                        {"invocation_key": key, "status": terminal_status, "message": message},
                        media_type="application/vnd.a-hunter.failure+json",
                    )
                    self._record_artifact(failure_ref.content_hash, media_type=failure_ref.media_type, retention_class="market")
                    if available_query_log_hash is not None:
                        self._record_artifact(
                            available_query_log_hash,
                            media_type="application/vnd.a-hunter.query-log+jsonl",
                            retention_class="market",
                        )
                    if available_query_capsule_hash is not None:
                        self._record_artifact(
                            available_query_capsule_hash,
                            media_type="application/vnd.a-hunter.run-capsule+json",
                            retention_class="market",
                        )
                    attempt_capsule_hash = failure_ref.content_hash
                    if isinstance(capsule_hash, str) and store.verify(capsule_hash) and capsule_hash != failure_ref.content_hash:
                        self._record_artifact(capsule_hash, media_type="application/vnd.a-hunter.run-capsule+json", retention_class="market")
                        attempt_capsule_hash = capsule_hash
                    if not attempts:
                        attempts = (type("Attempt", (), {"attempt_number": 1, "status": terminal_status, "duration_ms": 0, "error_class": error_class or "agent_failure"})(),)
                    for attempt in attempts:
                        current_capsule_hash = getattr(attempt, "capsule_hash", None)
                        capsule_available = isinstance(current_capsule_hash, str) and store.verify(current_capsule_hash)
                        if not capsule_available:
                            current_capsule_hash = attempt_capsule_hash
                        elif current_capsule_hash != attempt_capsule_hash:
                            self._record_artifact(
                                current_capsule_hash,
                                media_type="application/vnd.a-hunter.run-capsule+json",
                                retention_class="market",
                            )
                        current_output_hash = getattr(attempt, "output_hash", None)
                        output_available = isinstance(current_output_hash, str) and store.verify(current_output_hash)
                        if output_available:
                            self._record_artifact(
                                current_output_hash,
                                media_type=getattr(attempt, "output_media_type", None) or "application/json",
                                retention_class="market",
                            )
                        else:
                            current_output_hash = None
                        current_status = (
                            "passed"
                            if getattr(attempt, "status", "failed") == "passed"
                            and capsule_available
                            and output_available
                            else terminal_status
                        )
                        attempt_policy = policy.model_dump(mode="json")
                        attempt_policy["execution_phase"] = getattr(attempt, "phase", "finding")
                        attempt_id = self.repository.create_scope_attempt(
                            invocation_key=key,
                            attempt_number=int(attempt.attempt_number),
                            capsule_hash=current_capsule_hash,
                            policy_json=attempt_policy,
                            cli_version=getattr(attempt, "cli_version", None),
                            model=getattr(attempt, "model", None) or policy.model,
                            reasoning_effort=getattr(attempt, "reasoning_effort", None) or policy.reasoning_effort,
                            usage_json=getattr(attempt, "usage", None) or {},
                        )
                        self.repository.finish_scope_attempt(
                            attempt_id=attempt_id,
                            status=current_status,
                            output_hash=current_output_hash if current_status == "passed" else None,
                            error_class=(
                                None
                                if current_status == "passed"
                                else getattr(attempt, "error_class", error_class or "agent_failure")
                            ),
                            duration_ms=int(getattr(attempt, "duration_ms", 0)),
                        )
                self.repository.record_scope_invocation_failure(
                    key,
                    status=terminal_status,
                    message=message,
                    query_log_hash=available_query_log_hash,
                )
            return
        if store is None:
            with self.repository.transaction():
                self.repository.record_invocation_failure(key, status=terminal_status, message=message)
            return
        failure_ref = store.put_json({"invocation_key": key, "status": terminal_status, "message": message}, media_type="application/vnd.a-hunter.failure+json")
        with self.repository.transaction():
            self._record_artifact(failure_ref.content_hash, media_type=failure_ref.media_type)
            if available_query_log_hash is not None:
                self._record_artifact(
                    available_query_log_hash,
                    media_type="application/vnd.a-hunter.query-log+jsonl",
                )
            if available_query_capsule_hash is not None:
                self._record_artifact(
                    available_query_capsule_hash,
                    media_type="application/vnd.a-hunter.run-capsule+json",
                )
            attempt_capsule_hash = failure_ref.content_hash
            if isinstance(capsule_hash, str) and store.verify(capsule_hash):
                attempt_capsule_hash = capsule_hash
                if capsule_hash != failure_ref.content_hash:
                    self._record_artifact(capsule_hash, media_type="application/vnd.a-hunter.run-capsule+json")
            if not attempts:
                attempts = (type("Attempt", (), {"attempt_number": 1, "status": terminal_status, "duration_ms": 0, "error_class": error_class or "agent_failure"})(),)
            for attempt in attempts:
                current_capsule_hash = getattr(attempt, "capsule_hash", None)
                capsule_available = isinstance(current_capsule_hash, str) and store.verify(current_capsule_hash)
                if not capsule_available:
                    current_capsule_hash = attempt_capsule_hash
                elif current_capsule_hash != attempt_capsule_hash:
                    self._record_artifact(
                        current_capsule_hash,
                        media_type="application/vnd.a-hunter.run-capsule+json",
                    )
                current_output_hash = getattr(attempt, "output_hash", None)
                output_available = isinstance(current_output_hash, str) and store.verify(current_output_hash)
                if output_available:
                    self._record_artifact(
                        current_output_hash,
                        media_type=getattr(attempt, "output_media_type", None) or "application/json",
                    )
                else:
                    current_output_hash = None
                attempt_policy = policy.model_dump(mode="json")
                attempt_policy["execution_phase"] = getattr(attempt, "phase", "finding")
                attempt_id = self.repository.create_attempt(
                    invocation_key=key,
                    attempt_number=int(attempt.attempt_number),
                    capsule_hash=current_capsule_hash,
                    policy_json=attempt_policy,
                    cli_version=getattr(attempt, "cli_version", None),
                    model=getattr(attempt, "model", None) or policy.model,
                    reasoning_effort=getattr(attempt, "reasoning_effort", None) or policy.reasoning_effort,
                    usage_json=getattr(attempt, "usage", None) or {},
                )
                status = (
                    "passed"
                    if getattr(attempt, "status", "failed") == "passed"
                    and capsule_available
                    and output_available
                    else terminal_status
                )
                self.repository.finish_attempt(
                    attempt_id=attempt_id,
                    status=status,
                    output_hash=current_output_hash if status == "passed" else None,
                    error_class=None if status == "passed" else getattr(attempt, "error_class", error_class or "agent_failure"),
                    duration_ms=int(getattr(attempt, "duration_ms", 0)),
                )
            self.repository.record_invocation_failure(
                key,
                status=terminal_status,
                message=message,
                query_log_hash=available_query_log_hash,
            )

    def _artifact_exists(self, content_hash_value: str) -> bool:
        store = getattr(self.agent_runner, "artifact_store", None)
        return store is not None and store.verify(content_hash_value)

    def _persist_attempt_diagnostics(self, key: str, attempts: tuple[Any, ...]) -> None:
        from advisor.research.codex.executor import _bounded_error

        store = getattr(self.agent_runner, "artifact_store", None)
        diagnostics = [
            {
                "attempt_number": int(attempt.attempt_number),
                "phase": getattr(attempt, "phase", "finding"),
                "error_class": getattr(attempt, "error_class", None),
                "message": _bounded_error(str(attempt.error_message)),
            }
            for attempt in attempts if getattr(attempt, "error_message", None)
        ]
        if store is None or not diagnostics:
            return
        artifact = store.put_json(
            {"invocation_key": key, "attempts": diagnostics},
            media_type="application/vnd.a-hunter.attempt-diagnostics+json",
        )
        self._record_artifact(artifact.content_hash, media_type=artifact.media_type)

    def _record_artifact(self, content_hash_value: str, *, media_type: str, retention_class: str = "standard") -> None:
        if self.repository is None:
            return
        store = getattr(self.agent_runner, "artifact_store", None) or getattr(self.decision_pipeline, "artifact_store", None)
        if store is None or not store.verify(content_hash_value):
            raise ValueError(f"research artifact is unavailable: {content_hash_value}")
        path = store._path_for(content_hash_value)
        self.repository.record_artifact(
            ArtifactRef(content_hash_value, len(store.read_bytes(content_hash_value)), media_type),
            relative_path=str(path.relative_to(store.root)),
            retention_class=retention_class,
        )

    def _persist_stage(self, cycle_id: str, team_ref: str, stage: StageResult, policy: ExecutionPolicy) -> None:
        """Persist one passed Decision Stage at its completion boundary.

        Stage callbacks run before the whole Team Pipeline has finished.  The
        operation is therefore deliberately idempotent: a later finalization
        pass must be able to encounter the same stage and the same Attempt
        rows without creating synthetic duplicates or changing immutable data.
        """
        if self.repository is None:
            return
        store = getattr(self.decision_pipeline, "artifact_store", None)
        if store is None:
            return
        run_id = f"{cycle_id}:{team_ref}"
        stage_name = stage.stage
        try:
            stage_order = self.decision_pipeline.pipeline.stages.index(stage_name)
        except (AttributeError, ValueError):
            stage_order = 0
        output_hash = getattr(stage, "output_hash", None)
        if not isinstance(output_hash, str) or not store.verify(output_hash):
            output = stage.output.model_dump(mode="json") if hasattr(stage.output, "model_dump") else stage.output
            output_hash = store.put_json(output, media_type="application/vnd.a-hunter.stage-output+json").content_hash
        capsule_hash = getattr(stage, "capsule_hash", None)
        if not isinstance(capsule_hash, str) or not store.verify(capsule_hash):
            capsule_hash = None
        stage_run_id = f"{run_id}:{stage_name}"
        with self.repository.transaction():
            self._record_artifact(output_hash, media_type="application/vnd.a-hunter.stage-output+json")
            self.repository.record_stage_run(
                stage_run_id=stage_run_id,
                research_run_id=run_id,
                stage_name=stage_name,
                stage_order=stage_order,
                input_hashes=[stage.input_hash],
                output_hash=output_hash,
            )
            if capsule_hash is None:
                return
            self._record_artifact(capsule_hash, media_type="application/vnd.a-hunter.run-capsule+json")
            stage_attempts = tuple(getattr(stage, "attempts", ()))
            existing_attempt = self.repository.connection.execute(
                "SELECT 1 FROM research_stage_attempts WHERE stage_run_id = ? LIMIT 1",
                (stage_run_id,),
            ).fetchone()
            # A cached StageResult has no in-memory Attempt list.  Existing
            # rows are authoritative; only a genuinely new stage gets the
            # compatibility synthetic Attempt.
            if not stage_attempts and existing_attempt is None:
                stage_attempts = (type(
                    "Attempt",
                    (),
                    {
                        "attempt_number": 1,
                        "status": "passed",
                        "duration_ms": 0,
                        "cli_version": getattr(stage, "cli_version", None),
                        "model": getattr(stage, "model", None),
                        "reasoning_effort": getattr(stage, "reasoning_effort", None),
                        "usage": getattr(stage, "usage", None) or {},
                    },
                )(),)
            for attempt in stage_attempts:
                attempt_number = int(getattr(attempt, "attempt_number", 1))
                raw_status = str(getattr(attempt, "status", "passed"))
                status = "passed" if raw_status == "passed" else "failed"
                self.repository.record_stage_attempt(
                    stage_attempt_id=f"{stage_run_id}:{attempt_number}",
                    stage_run_id=stage_run_id,
                    attempt_number=attempt_number,
                    status=status,
                    capsule_hash=capsule_hash,
                    output_hash=output_hash if status == "passed" else None,
                    error_class=None if status == "passed" else getattr(attempt, "error_class", raw_status),
                    duration_ms=int(getattr(attempt, "duration_ms", 0)),
                    policy_json=policy.model_dump(mode="json"),
                    cli_version=getattr(attempt, "cli_version", None) or getattr(stage, "cli_version", None),
                    model=getattr(attempt, "model", None) or getattr(stage, "model", None) or policy.model,
                    reasoning_effort=getattr(attempt, "reasoning_effort", None) or getattr(stage, "reasoning_effort", None) or policy.reasoning_effort,
                    usage_json=getattr(attempt, "usage", None) or getattr(stage, "usage", None) or {},
                )

    def _persist_pipeline(self, cycle_id: str, team_ref: str, pipeline_result: Any, policy: ExecutionPolicy) -> None:
        if isinstance(pipeline_result, MarketPipelineResult):
            # Market reports are stored by the durable Request publication
            # boundary.  Legacy conclusion tables are Security-only and must
            # not receive a fake code or stance.
            return
        if self.repository is None or not hasattr(pipeline_result, "stages"):
            return
        store = getattr(self.decision_pipeline, "artifact_store", None)
        if store is None:
            return
        for stage in pipeline_result.stages.values():
            self._persist_stage(cycle_id, team_ref, stage, policy)

        run_id = f"{cycle_id}:{team_ref}"
        conclusion = pipeline_result.conclusion
        conclusion_hash = store.put_json(
            conclusion.model_dump(mode="json"),
            media_type="application/vnd.a-hunter.team-conclusion+json",
        ).content_hash
        with self.repository.transaction():
            self._record_artifact(conclusion_hash, media_type="application/vnd.a-hunter.team-conclusion+json")
            self.repository.record_team_conclusion(
                conclusion_hash=conclusion_hash,
                research_run_id=run_id,
                team_ref=team_ref,
                subject_code=conclusion.subject.code,
                as_of=conclusion.boundary.as_of.isoformat(),
                stance=conclusion.stance,
                conviction=conclusion.conviction,
                quality_status=conclusion.evidence_quality.status,
            )
            self.repository.set_run_conclusion(run_id, conclusion_hash)

    def _persist_pipeline_failure(self, cycle_id: str, team_ref: str, error: Exception, policy: ExecutionPolicy) -> None:
        if self.catalog.team(team_ref).scope == ResearchScope.market:
            return
        if self.repository is None:
            return
        store = getattr(self.decision_pipeline, "artifact_store", None)
        if store is None:
            return
        run_id = f"{cycle_id}:{team_ref}"
        stage_name = getattr(error, "stage", None) or "decision_pipeline"
        try:
            stage_order = self.decision_pipeline.pipeline.stages.index(stage_name)
        except ValueError:
            stage_order = len(self.decision_pipeline.pipeline.stages)
        message = str(error)[:500]
        failure_ref = store.put_json(
            {"status": "blocked", "stage": stage_name, "message": message},
            media_type="application/vnd.a-hunter.stage-failure+json",
        )
        capsule_hash = getattr(error, "capsule_hash", None)
        with self.repository.transaction():
            self._record_artifact(failure_ref.content_hash, media_type=failure_ref.media_type)
            if not isinstance(capsule_hash, str) or not store.verify(capsule_hash):
                capsule_hash = failure_ref.content_hash
            elif capsule_hash != failure_ref.content_hash:
                self._record_artifact(capsule_hash, media_type="application/vnd.a-hunter.run-capsule+json")
            stage_run_id = f"{run_id}:{stage_name}"
            self.repository.record_stage_run(
                stage_run_id=stage_run_id,
                research_run_id=run_id,
                stage_name=stage_name,
                stage_order=stage_order,
                input_hashes=[],
                output_hash=failure_ref.content_hash,
                status=RunStatus.blocked.value,
                message=message,
            )
            attempts = tuple(getattr(error, "attempts", ()))
            if not attempts:
                attempts = (type("Attempt", (), {"attempt_number": 1, "status": "failed", "duration_ms": 0, "error_class": getattr(error, "error_class", "stage_failure")})(),)
            for attempt in attempts:
                self.repository.record_stage_attempt(
                    stage_attempt_id=f"{stage_run_id}:{int(attempt.attempt_number)}",
                    stage_run_id=stage_run_id,
                    attempt_number=int(attempt.attempt_number),
                    status="failed",
                    capsule_hash=capsule_hash,
                    output_hash=None,
                    error_class=getattr(attempt, "error_class", getattr(error, "error_class", "stage_failure")),
                    duration_ms=int(getattr(attempt, "duration_ms", 0)),
                    policy_json=policy.model_dump(mode="json"),
                    cli_version=getattr(attempt, "cli_version", None),
                    model=getattr(attempt, "model", None) or policy.model,
                    reasoning_effort=getattr(attempt, "reasoning_effort", None) or policy.reasoning_effort,
                    usage_json=getattr(attempt, "usage", None) or {},
                )

    def _finish(self, cycle_id: str, signature: str, result: ResearchCycleResult) -> ResearchCycleResult:
        self._signatures[cycle_id] = signature
        self._completed[cycle_id] = result
        if self.repository is not None and not result.subject.is_market:
            with self.repository.transaction():
                self.repository.set_status(
                    "research_cycles",
                    "cycle_id",
                    cycle_id,
                    result.status.value,
                    message=None if result.status == RunStatus.passed else "one or more Team runs blocked",
                    finished=True,
                )
                for team_ref, team_result in result.team_results.items():
                    self.repository.set_status(
                        "research_runs",
                        "research_run_id",
                        f"{cycle_id}:{team_ref}",
                        team_result.status.value,
                        message=team_result.message,
                        finished=True,
                    )
        return result


def _conclusion_payload(value: Any | None) -> Any | None:
    if value is None:
        return None
    conclusion = getattr(value, "conclusion", value)
    model_dump = getattr(conclusion, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if hasattr(conclusion, "__dict__"):
        return vars(conclusion)
    return conclusion


def _snapshot_agent_errors(
    catalog: ManifestCatalog,
    agent_runner: Any,
    agent_refs: list[str],
    snapshot: SnapshotResult,
) -> dict[str, str]:
    """Describe declared Agent inputs that the sealed Snapshot cannot serve."""
    errors: dict[str, str] = {}
    for agent_ref in agent_refs:
        missing = [
            str(product)
            for product in catalog.agent(agent_ref).product_refs
            if str(product) not in snapshot.products
        ]
        if not missing:
            continue
        details = "; ".join(
            f"{product}: {snapshot.unavailable.get(product, 'not materialized')}"
            for product in missing
        )
        errors[agent_ref] = (
            f"Agent {agent_ref} is missing Snapshot product "
            f"{', '.join(missing)} ({details})"
        )[:500]
    validate_inputs = getattr(agent_runner, "validate_snapshot_inputs", None)
    if not callable(validate_inputs):
        return errors
    for agent_ref in agent_refs:
        if agent_ref in errors:
            continue
        try:
            validate_inputs(agent_ref, snapshot)
        except AgentInputUnavailable as error:
            errors[agent_ref] = f"{type(error).__name__}: {str(error)[:440]}"
    return errors


def _agent_failure_reason(
    missing_agents: list[str],
    errors: dict[str, str],
    error_classes: dict[str, str],
) -> str:
    material = " ".join(
        f"{error_classes.get(agent, '')} {errors.get(agent, '')}" for agent in missing_agents
    ).lower()
    if "snapshot_unavailable" in material or "snapshot product" in material:
        return "snapshot_unavailable"
    if "timeout" in material:
        return "agent_timeout"
    return "agent_invalid"


def _market_block_reason(result: Any) -> str:
    reasons = {
        str(getattr(item, "reason_code", ""))
        for item in getattr(result, "blocked_insights", ())
        if getattr(item, "reason_code", None)
    }
    if len(reasons) == 1:
        return reasons.pop()
    return "quality_blocked"


def _is_cancelled(cancel_event: Any | None) -> bool:
    return bool(cancel_event is not None and callable(getattr(cancel_event, "is_set", None)) and cancel_event.is_set())


def _raise_if_cancelled(cancel_event: Any | None) -> None:
    if _is_cancelled(cancel_event):
        raise ResearchCycleCancelled()


def _notify_progress(
    callback: Callable[[str, int, int, str | None], None] | None,
    phase: str,
    completed: int,
    total: int,
    stage: str | None,
) -> None:
    if callback is not None:
        callback(phase, completed, total, stage)
