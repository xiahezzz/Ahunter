from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from advisor.coordinator import run_single_subject
from tests.advisor.research.test_end_to_end import FixtureCodex, _registry


def test_coordinator_uses_the_self_contained_research_engine(tmp_path: Path):
    result = run_single_subject(
        code="600519",
        as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc),
        output_dir=tmp_path / "reports",
        db_path=tmp_path / "research.sqlite",
        artifact_dir=tmp_path / "artifacts",
        provider_registry=_registry(),
        executor=FixtureCodex(),
        cycle_id="cycle-coordinator-1",
    )
    assert result.preflight.status == "passed"
    assert result.cycle.status.value == "passed"
    assert result.publication.cycle_json.is_file()
