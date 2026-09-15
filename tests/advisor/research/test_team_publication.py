from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from advisor.research.catalog import load_catalog_from_directory
from advisor.research.team_publication import (
    TeamPublicationConflict,
    TeamPublicationValidationError,
    TeamPublicationService,
)


def _catalog(tmp_path: Path) -> Path:
    catalog = tmp_path / "catalog"
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
    for agent, version, title in (
        ("market", 1, "市场研究"),
        ("market", 2, "市场研究新版本"),
        ("news", 1, "资讯研究"),
    ):
        (catalog / "agents" / f"{agent}_{version}.yaml").write_text(
            f"""
agent: {{id: {agent}, version: {version}}}
title: {title}
instructions: 使用固定样例数据。
required_products: [{{id: identity, version: 1}}]
""",
            encoding="utf-8",
        )
    return catalog


def test_publish_pins_latest_agents_and_is_immediately_discoverable(tmp_path: Path):
    catalog = _catalog(tmp_path)
    service = TeamPublicationService(catalog)

    published = service.publish("value_style", "价值风格", ["news", "market"], scope="security")

    assert published.created is True
    assert published.team_ref == "value_style@1"
    assert published.path.name == "value_style_v1.yaml"
    assert [str(agent) for agent in published.team.agents] == ["market@2", "news@1"]
    assert load_catalog_from_directory(catalog).team("value_style@1") == published.team


def test_repeat_is_idempotent_but_a_revised_publication_gets_next_version(tmp_path: Path):
    catalog = _catalog(tmp_path)
    service = TeamPublicationService(catalog)

    first = service.publish("value_style", "价值风格", ["market"], scope="security")
    repeated = service.publish("value_style", "价值风格", ["market"], scope="security")
    revised = service.publish("value_style", "价值风格二版", ["market"], scope="security")

    assert first.team_ref == repeated.team_ref == "value_style@1"
    assert repeated.created is False
    assert revised.team_ref == "value_style@2"
    assert first.path.read_text(encoding="utf-8") == repeated.path.read_text(encoding="utf-8")


def test_new_agent_version_does_not_change_published_team_and_is_pinned_on_new_revision(tmp_path: Path):
    catalog = _catalog(tmp_path)
    service = TeamPublicationService(catalog)
    first = service.publish("value_style", "价值风格", ["market"], scope="security")
    (catalog / "agents" / "market_3.yaml").write_text(
        """
agent: {id: market, version: 3}
title: 市场研究第三版
instructions: 使用固定样例数据。
required_products: [{id: identity, version: 1}]
""",
        encoding="utf-8",
    )

    revised = service.publish("value_style", "价值风格", ["market"], scope="security")

    reloaded = load_catalog_from_directory(catalog)
    assert [str(agent) for agent in reloaded.team(str(first.team.team)).agents] == ["market@2"]
    assert revised.team_ref == "value_style@2"
    assert [str(agent) for agent in revised.team.agents] == ["market@3"]


@pytest.mark.parametrize(
    ("team_id", "title", "agent_ids", "error"),
    [
        ("value_style", "价值风格", [], TeamPublicationValidationError),
        ("value_style", "价值风格", ["unknown"], TeamPublicationValidationError),
        ("value_style", "价值风格", ["market", "market"], TeamPublicationValidationError),
        ("ValueStyle", "价值风格", ["market"], TeamPublicationValidationError),
        ("value_style", "Value Style", ["market"], TeamPublicationValidationError),
    ],
)
def test_invalid_publications_fail_closed_without_creating_files(
    tmp_path: Path,
    team_id: str,
    title: str,
    agent_ids: list[str],
    error: type[Exception],
):
    catalog = _catalog(tmp_path)
    service = TeamPublicationService(catalog)

    with pytest.raises(error):
        service.publish(team_id, title, agent_ids, scope="security")

    assert list((catalog / "teams").iterdir()) == []


def test_other_stable_team_cannot_reuse_the_same_agent_identity_set(tmp_path: Path):
    catalog = _catalog(tmp_path)
    service = TeamPublicationService(catalog)
    service.publish("value_style", "价值风格", ["market", "news"], scope="security")

    with pytest.raises(TeamPublicationConflict, match="成员组合"):
        service.publish("growth_style", "成长风格", ["news", "market"], scope="security")

    assert sorted(path.name for path in (catalog / "teams").iterdir()) == [".team-publication.lock", "value_style_v1.yaml"]


def test_concurrent_identical_publications_create_one_immutable_version(tmp_path: Path):
    catalog = _catalog(tmp_path)
    service = TeamPublicationService(catalog)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: service.publish("value_style", "价值风格", ["market"], scope="security"), range(2)))

    assert {result.team_ref for result in results} == {"value_style@1"}
    assert sum(result.created for result in results) == 1
    assert sorted(path.name for path in (catalog / "teams").glob("*.yaml")) == ["value_style_v1.yaml"]


def test_publication_does_not_create_database_or_runtime_artifacts(tmp_path: Path):
    catalog = _catalog(tmp_path)

    TeamPublicationService(catalog).publish("value_style", "价值风格", ["market"], scope="security")

    assert not list(tmp_path.rglob("*.sqlite"))
    assert not (tmp_path / "reports").exists()
    assert not (tmp_path / "artifacts").exists()


def test_new_team_requires_explicit_scope_and_rejects_cross_scope_agents(tmp_path: Path):
    catalog = _catalog(tmp_path)
    (catalog / "agents" / "market_overview.yaml").write_text(
        """
agent: {id: market_overview, version: 1}
scope: market
title: 全市场研究
instructions: 使用固定样例数据。
required_products: [{id: identity, version: 1}]
""",
        encoding="utf-8",
    )
    service = TeamPublicationService(catalog)

    with pytest.raises(TeamPublicationValidationError, match="必须选择"):
        service.publish("security_style", "证券风格", ["market"], scope=None)
    with pytest.raises(TeamPublicationValidationError, match="相同研究范围"):
        service.publish("market_style", "市场风格", ["market"], scope="market")

    published = service.publish("market_style", "市场风格", ["market_overview"], scope="market")
    assert published.team.scope.value == "market"
    with pytest.raises(TeamPublicationValidationError, match="不能改变研究范围"):
        service.publish("market_style", "市场风格", ["market"], scope="security")
