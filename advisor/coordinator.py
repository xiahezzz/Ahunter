"""Small application facade for the A Hunter Research Engine.

The scheduler and CLI own process-level argument handling.  This module keeps
one importable composition seam for callers that need to start a single
explicit Subject programmatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

from advisor.research.cli import (
    PreflightResult,
    ResearchRun,
    ResearchRuntime,
    build_runtime,
    preflight_runtime,
    run_research_cycle,
)
from advisor.research.contracts import VersionRef


@dataclass(frozen=True)
class CoordinatorResult:
    run: ResearchRun
    preflight: PreflightResult

    @property
    def cycle(self):
        return self.run.cycle

    @property
    def publication(self):
        return self.run.publication


def run_single_subject(
    *,
    code: str,
    as_of: datetime,
    output_dir: Path,
    subject_name: str | None = None,
    team_refs: Sequence[str | VersionRef] | None = None,
    config_path: Path | None = None,
    db_path: Path | None = None,
    artifact_dir: Path | None = None,
    executor=None,
    provider_registry=None,
    mx_snapshot=None,
    cycle_id: str | None = None,
) -> CoordinatorResult:
    runtime = build_runtime(
        config_path=config_path,
        db_path=db_path,
        artifact_dir=artifact_dir,
        executor=executor,
        provider_registry=provider_registry,
        mx_snapshot=mx_snapshot,
    )
    try:
        preflight = preflight_runtime(runtime, output_dir=output_dir, team_refs=team_refs)
        if preflight.status != "passed":
            raise RuntimeError(preflight.message or "Research Engine preflight blocked")
        run = run_research_cycle(
            runtime,
            code=code,
            subject_name=subject_name,
            as_of=as_of,
            team_refs=team_refs,
            reports_root=output_dir,
            cycle_id=cycle_id,
        )
        return CoordinatorResult(run, preflight)
    finally:
        runtime.close()


__all__ = [
    "CoordinatorResult",
    "PreflightResult",
    "ResearchRun",
    "ResearchRuntime",
    "build_runtime",
    "preflight_runtime",
    "run_research_cycle",
    "run_single_subject",
]
