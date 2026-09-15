from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from advisor.scheduler import premarket


def test_premarket_scheduler_delegates_to_self_contained_research_cli(monkeypatch, tmp_path: Path):
    calls = []

    def fake_cli(argv):
        calls.append(tuple(argv or ()))
        return 0

    monkeypatch.setattr(premarket, "research_main", fake_cli)

    assert premarket.main(
        [
            "--codes", "600519",
            "--as-of", "2026-08-06T08:30:00+08:00",
            "--events-db", "data/state/events.sqlite",
            "--allowed-rids", "config/allowed-rids.yaml",
            "--output-dir", str(tmp_path / "reports"),
        ]
    ) == 0
    assert calls == [
        (
            "batch",
                "--codes", "600519",
                "--as-of", "2026-08-06T08:30:00+08:00",
                "--events-db", "data/state/events.sqlite",
                "--allowed-rids", "config/allowed-rids.yaml",
                "--output-dir", str(tmp_path / "reports"),
        )
    ]
