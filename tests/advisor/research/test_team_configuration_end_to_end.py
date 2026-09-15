from __future__ import annotations

import shutil
from datetime import datetime, timezone
import json
from pathlib import Path

from fastapi.testclient import TestClient
import yaml

from advisor.research.catalog import load_catalog_from_directory
from advisor.research.cli import build_runtime, run_daily_batch
from advisor.research.daily_teams import DailyTeamSetService
from advisor.research.data_products.engine import ProviderRegistry
from advisor.web.api import create_app
from tests.advisor.research.test_end_to_end import FixtureCodex, _registry


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "workspace"
    shutil.copytree(Path.cwd() / "config" / "research", root / "config" / "research")
    config = yaml.safe_load((Path.cwd() / "config" / "advisor.yaml").read_text(encoding="utf-8"))
    config["research"]["default_teams"] = []
    config_path = root / "config" / "advisor.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (root / "config" / "research" / "agents" / "broken_agent.yaml").write_text(
        """
agent: broken_agent@1
title: 失败样例专家
instructions: 仅用于离线失败隔离验证。
required_products: [company_identity@1]
""",
        encoding="utf-8",
    )
    return root, config_path


class FailingFixtureCodex(FixtureCodex):
    def execute(self, capsule, policy, *, validator=None, **kwargs):
        manifest = json.loads(capsule.manifest_path.read_text(encoding="utf-8"))
        if manifest["label"] == "research-agent-broken_agent@1":
            raise RuntimeError("fixture Agent failure")
        if manifest["label"] == "decision-portfolio_manager" and validator is not None:
            team_id, version = manifest["context"]["team"].split("@", 1)

            def validate_current_team(payload):
                payload = {**payload, "team": {"id": team_id, "version": int(version)}}
                return validator(payload)

            return super().execute(capsule, policy, validator=validate_current_team, **kwargs)
        return super().execute(capsule, policy, validator=validator, **kwargs)


def test_team_configuration_flows_from_api_to_independent_daily_reports_without_real_state_changes(tmp_path: Path):
    root, config_path = _workspace(tmp_path)
    real_config_path = Path.cwd() / "config" / "advisor.yaml"
    real_config = real_config_path.read_bytes()
    client = TestClient(
        create_app(
            state_dir=tmp_path / "state",
            db_path=tmp_path / "web.sqlite",
            research_root=root,
            research_config_path=config_path,
        )
    )

    assert client.get("/api/research/agents").status_code == 200
    value = client.post(
        "/api/research/teams",
        json={"team_id": "value_style", "scope": "security", "title": "价值风格", "agent_ids": ["market", "news"]},
    )
    repeated = client.post(
        "/api/research/teams",
        json={"team_id": "value_style", "scope": "security", "title": "价值风格", "agent_ids": ["news", "market"]},
    )
    revised = client.post(
        "/api/research/teams",
        json={"team_id": "value_style", "scope": "security", "title": "价值风格二版", "agent_ids": ["market", "news"]},
    )
    broken = client.post(
        "/api/research/teams",
        json={"team_id": "broken_style", "scope": "security", "title": "失败风格", "agent_ids": ["broken_agent"]},
    )

    assert value.status_code == 201
    assert value.json()["team"]["team_ref"] == "value_style@1"
    assert repeated.status_code == 200
    assert repeated.json()["created"] is False
    assert revised.status_code == 201
    assert revised.json()["team"]["team_ref"] == "value_style@2"
    assert broken.status_code == 201
    assert DailyTeamSetService(config_path=config_path, root=root).read().team_refs == ()
    assert client.put("/api/research/daily-teams/a_share_core@1").status_code == 200
    assert client.put("/api/research/daily-teams/value_style@1").status_code == 200
    assert client.put("/api/research/daily-teams/value_style@2").json()["daily_teams"] == ["a_share_core@1", "value_style@2"]
    assert client.put("/api/research/daily-teams/broken_style@1").status_code == 200

    executor = FailingFixtureCodex()
    runtime = build_runtime(
        root=root,
        config_path=config_path,
        db_path=tmp_path / "research.sqlite",
        artifact_dir=tmp_path / "artifacts",
        provider_registry=_registry(),
        executor=executor,
    )
    try:
        result = run_daily_batch(
            runtime,
            batch_id="team-config-e2e",
            codes=("600519",),
            as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc),
            reports_root=tmp_path / "reports",
        )
        cycle = result.batch.cycles["600519"]
        assert result.batch.team_refs == ("a_share_core@1", "value_style@2", "broken_style@1")
        assert cycle.team_results["a_share_core@1"].status.value == "passed"
        assert cycle.team_results["value_style@2"].status.value == "passed"
        assert cycle.team_results["broken_style@1"].status.value == "blocked"
        assert set(cycle.team_findings["value_style@2"]) == {"market@1", "news@1"}
        assert runtime.repository.connection.execute("SELECT COUNT(*) FROM research_invocations").fetchone()[0] == 8
        assert (result.publications["600519"].root / "teams" / "a_share_core@1" / "conclusion.json").is_file()
        assert (result.publications["600519"].root / "teams" / "value_style@2" / "conclusion.json").is_file()
        assert (result.publications["600519"].root / "teams" / "broken_style@1" / "status.json").is_file()
        assert not (result.publications["600519"].root / "teams" / "broken_style@1" / "conclusion.json").exists()
        assert "stance" not in result.publications["600519"].index.read_text(encoding="utf-8").lower()
    finally:
        runtime.close()

    daily_teams = DailyTeamSetService(config_path=config_path, root=root)
    assert daily_teams.read().team_refs == ("a_share_core@1", "value_style@2", "broken_style@1")
    assert (root / "config" / "research" / "teams" / "value_style_v1.yaml").is_file()
    market_v2 = (root / "config" / "research" / "agents" / "market.yaml").read_text(encoding="utf-8")
    (root / "config" / "research" / "agents" / "market_v2.yaml").write_text(
        market_v2.replace("agent: market@1", "agent: market@2").replace("市场走势专家", "市场走势专家二版"),
        encoding="utf-8",
    )
    listed_agents = client.get("/api/research/agents").json()["agents"]
    assert next(item for item in listed_agents if item["agent_id"] == "market")["agent_ref"] == "market@2"
    assert [str(agent) for agent in load_catalog_from_directory(root / "config" / "research").team("a_share_core@1").agents][0] == "market@1"

    for reference in ("a_share_core@1", "value_style@2", "broken_style@1"):
        daily_teams.disable(reference)
    empty_runtime = build_runtime(
        root=root,
        config_path=config_path,
        db_path=tmp_path / "empty.sqlite",
        artifact_dir=tmp_path / "empty-artifacts",
        provider_registry=ProviderRegistry(),
        executor=object(),
    )
    try:
        skipped = run_daily_batch(
            empty_runtime,
            batch_id="team-config-empty",
            codes=("600519",),
            as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc),
            reports_root=tmp_path / "empty-reports",
        )
        assert skipped.batch.status == "skipped"
        assert skipped.publications == skipped.briefs == {}
    finally:
        empty_runtime.close()

    assert not (Path.cwd() / "config" / "research" / "teams" / "value_style_v1.yaml").exists()
    assert not (Path.cwd() / "reports" / "2026-08-06" / "team-config-e2e").exists()
    assert real_config_path.read_bytes() == real_config
