from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from advisor.research.daily_teams import DailyTeamSetService, DailyTeamSetValidationError


def _workspace(tmp_path: Path, *, default_teams: list[str] | None = None) -> Path:
    root = tmp_path / "workspace"
    catalog = root / "config" / "research"
    for directory in ("products", "agents", "teams", "pipelines", "execution"):
        (catalog / directory).mkdir(parents=True)
    (catalog / "products" / "identity.yaml").write_text(
        """
product: {id: identity, version: 1}
title: 公司信息
providers: [fixture]
""",
        encoding="utf-8",
    )
    for agent in ("market", "news"):
        (catalog / "agents" / f"{agent}.yaml").write_text(
            f"""
agent: {{id: {agent}, version: 1}}
title: {"市场" if agent == "market" else "资讯"}研究
instructions: 使用固定样例数据。
required_products: [{{id: identity, version: 1}}]
""",
            encoding="utf-8",
        )
    for team, version, agents in (
        ("core", 1, ["market@1"]),
        ("core", 2, ["market@1", "news@1"]),
        ("news_style", 1, ["news@1"]),
    ):
        agent_yaml = ", ".join(f"{{id: {agent.split('@')[0]}, version: {agent.split('@')[1]}}}" for agent in agents)
        (catalog / "teams" / f"{team}_{version}.yaml").write_text(
            f"""
team: {{id: {team}, version: {version}}}
title: {"核心" if team == "core" else "资讯"}风格
agents: [{agent_yaml}]
""",
            encoding="utf-8",
        )
    config = root / "config" / "advisor.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "market": {"primary": "A股"},
                "schedule": {"premarket_time": "08:30", "review_time": "22:30"},
                "storage": {"database": "data/advisor/advisor.sqlite"},
                "data_sources": {"allow_tushare": False, "free_sources": []},
                "research": {
                    "catalog_dir": "config/research",
                    "artifact_dir": "data/advisor/research-artifacts",
                    "default_teams": default_teams or [],
                    "execution_policy": "codex@1",
                    "max_subjects": 20,
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return root


def test_daily_team_set_allows_empty_and_replaces_only_the_same_stable_team(tmp_path: Path):
    root = _workspace(tmp_path)
    service = DailyTeamSetService(config_path=root / "config" / "advisor.yaml", root=root)

    assert service.read().team_refs == ()
    assert service.enable("core@1").team_refs == ("core@1",)
    assert service.enable("news_style@1").team_refs == ("core@1", "news_style@1")
    assert service.enable("core@2").team_refs == ("core@2", "news_style@1")
    assert service.enable("core@2").team_refs == ("core@2", "news_style@1")
    assert service.disable("core@1").team_refs == ("core@2", "news_style@1")
    assert service.disable("core@2").team_refs == ("news_style@1",)
    assert service.disable("news_style@1").team_refs == ()


def test_daily_team_set_persists_only_valid_exact_published_references(tmp_path: Path):
    root = _workspace(tmp_path, default_teams=["core@1"])
    config_path = root / "config" / "advisor.yaml"
    service = DailyTeamSetService(config_path=config_path, root=root)

    before = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    with pytest.raises(DailyTeamSetValidationError, match="未发布"):
        service.enable("unknown@1")
    assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == before

    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["research"]["default_teams"] = ["core@1", "core@2"]
    config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(DailyTeamSetValidationError, match="多个版本"):
        service.read()


def test_daily_team_set_preserves_unrelated_advisor_configuration(tmp_path: Path):
    root = _workspace(tmp_path)
    config_path = root / "config" / "advisor.yaml"
    before = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    DailyTeamSetService(config_path=config_path, root=root).enable("core@1")

    after = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert after["market"] == before["market"]
    assert after["schedule"] == before["schedule"]
    assert after["storage"] == before["storage"]
    assert after["data_sources"] == before["data_sources"]
    assert after["research"] | {"default_teams": []} == before["research"]
    assert after["research"]["default_teams"] == ["core@1"]


def test_concurrent_daily_team_enables_do_not_lose_an_unrelated_team(tmp_path: Path):
    root = _workspace(tmp_path)
    service = DailyTeamSetService(config_path=root / "config" / "advisor.yaml", root=root)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(service.enable, ("core@1", "news_style@1")))

    assert set(service.read().team_refs) == {"core@1", "news_style@1"}
