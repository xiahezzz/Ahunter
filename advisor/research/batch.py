from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import re
from typing import Iterable, Literal

from advisor.research.contracts import ExecutionPolicy, ResearchBoundary, ResearchSubject, RunStatus, VersionRef
from advisor.research.state_machine import ResearchCycleResult, TeamRunResult


class CandidateSelectionError(ValueError):
    pass


@dataclass(frozen=True)
class Candidate:
    subject: ResearchSubject
    sources: tuple[str, ...]


@dataclass(frozen=True)
class DailyBatchResult:
    batch_id: str
    boundary: ResearchBoundary
    team_refs: tuple[str, ...]
    execution_policy_ref: str
    cycles: dict[str, ResearchCycleResult]
    candidate_sources: dict[str, tuple[str, ...]]
    status: RunStatus | Literal["skipped"] = RunStatus.blocked


def select_candidates(
    *,
    explicit_codes: Iterable[str] = (),
    mx_codes: Iterable[str] = (),
    position_codes: Iterable[str] = (),
    max_subjects: int,
) -> tuple[Candidate, ...]:
    if max_subjects < 1:
        raise CandidateSelectionError("maximum subjects must be positive")
    ordered: dict[str, list[str]] = {}
    for source, codes in (("explicit", explicit_codes), ("mx", mx_codes), ("position", position_codes)):
        for raw_code in codes:
            try:
                subject = ResearchSubject(code=raw_code)
            except (TypeError, ValueError) as error:
                raise CandidateSelectionError("candidate code must be a six-digit A-share code") from error
            ordered.setdefault(subject.code, []).append(source)
    if len(ordered) > max_subjects:
        raise CandidateSelectionError(f"candidate count exceeds maximum of {max_subjects}")
    return tuple(Candidate(ResearchSubject(code=code), tuple(dict.fromkeys(sources))) for code, sources in ordered.items())


class DailyResearchBatch:
    def __init__(self, cycle_engine, *, max_cycle_concurrency: int = 1) -> None:
        if max_cycle_concurrency < 1:
            raise ValueError("max_cycle_concurrency must be positive")
        self.cycle_engine = cycle_engine
        self.max_cycle_concurrency = max_cycle_concurrency

    def run(
        self,
        batch_id: str,
        candidates: tuple[Candidate, ...],
        team_refs: tuple[str, ...],
        boundary: ResearchBoundary,
        policy: ExecutionPolicy,
    ) -> DailyBatchResult:
        if not _BATCH_ID_RE.fullmatch(batch_id):
            raise ValueError("unsafe batch_id")
        if not candidates or not team_refs:
            raise ValueError("Daily Batch requires candidates and Teams")
        normalized_teams = tuple(str(VersionRef.parse(ref)) for ref in team_refs)
        cycles: dict[str, ResearchCycleResult] = {}

        def run_one(candidate: Candidate) -> tuple[str, ResearchCycleResult]:
            cycle_id = f"{batch_id}-{candidate.subject.code}"
            try:
                if getattr(self.cycle_engine, "repository", None) is not None:
                    cycle = self.cycle_engine.run_cycle(
                        cycle_id,
                        candidate.subject,
                        boundary,
                        list(normalized_teams),
                        policy,
                        batch_id=batch_id,
                    )
                else:
                    cycle = self.cycle_engine.run_cycle(cycle_id, candidate.subject, boundary, list(normalized_teams), policy)
            except Exception as error:
                team_results = {
                    team_ref: TeamRunResult(VersionRef.parse(team_ref), RunStatus.blocked, message=f"cycle failed: {type(error).__name__}")
                    for team_ref in normalized_teams
                }
                cycle = ResearchCycleResult(
                    cycle_id,
                    candidate.subject,
                    boundary,
                    RunStatus.blocked,
                    "",
                    None,
                    {},
                    {"cycle": type(error).__name__},
                    team_results,
                )
            return candidate.subject.code, cycle

        with ThreadPoolExecutor(max_workers=min(self.max_cycle_concurrency, len(candidates)), thread_name_prefix="a-hunter-cycle") as pool:
            futures = [pool.submit(run_one, candidate) for candidate in candidates]
            for future in as_completed(futures):
                code, cycle = future.result()
                cycles[code] = cycle
        cycles = {code: cycles[code] for code in sorted(cycles)}
        status = RunStatus.passed if any(cycle.status == RunStatus.passed for cycle in cycles.values()) else RunStatus.blocked
        return DailyBatchResult(
            batch_id,
            boundary,
            normalized_teams,
            str(policy.policy),
            cycles,
            {candidate.subject.code: candidate.sources for candidate in candidates},
            status,
        )


_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
