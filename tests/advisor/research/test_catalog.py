import re
from pathlib import Path

import pytest

from advisor.research.catalog import ManifestCatalog, load_catalog
from advisor.research.contracts import (
    AgentManifest,
    DataProductManifest,
    ResearchScope,
    ResearchSubject,
    ResearchTeam,
    VersionRef,
)


def test_version_ref_and_subject_are_canonical():
    assert str(VersionRef.parse("market@1")) == "market@1"
    assert ResearchSubject(code="600519").code == "600519"


def test_repository_product_titles_are_localized_for_webui():
    repository_root = Path(__file__).resolve().parents[3]
    catalog = load_catalog(repository_root)

    missing_chinese = {
        product_ref: product.title
        for product_ref, product in catalog.products.items()
        if re.search(r"[\u4e00-\u9fff]", product.title) is None
    }
    assert missing_chinese == {}


def test_repository_preserves_historical_policy_and_publishes_supported_active_policy():
    repository_root = Path(__file__).resolve().parents[3]
    catalog = load_catalog(repository_root)

    assert catalog.execution_policy("codex@1").model == "gpt-5.4"
    assert catalog.execution_policy("codex@2").model == "gpt-5.6-sol"
    active = catalog.execution_policy("codex@3")
    assert active.model == "gpt-5.6-sol"
    assert active.reasoning_effort == "medium"
    assert active.proxy_environment is not None
    assert active.proxy_environment.model_dump() == {
        "https_proxy": "http://127.0.0.1:7897",
        "http_proxy": "http://127.0.0.1:7897",
        "all_proxy": "socks5://127.0.0.1:7897",
    }


def test_invalid_version_ref_and_subject_fail_closed():
    with pytest.raises(ValueError):
        VersionRef.parse("Market@1")
    with pytest.raises(ValueError):
        ResearchSubject(code="60051")


def test_research_subject_has_explicit_market_and_security_shapes_without_sentinel_code():
    legacy = ResearchSubject(code="600519")
    market = ResearchSubject(scope=ResearchScope.market)

    assert legacy.scope == ResearchScope.security
    assert market.model_dump(mode="json") == {"scope": "market", "code": None, "name": None}
    assert market != legacy
    with pytest.raises(ValueError, match="must not carry"):
        ResearchSubject(scope="market", code="000000")
    with pytest.raises(ValueError, match="requires"):
        ResearchSubject(scope="security")
    with pytest.raises(ValueError, match="six-digit A-share"):
        ResearchSubject(scope="security", code="830001")


def test_catalog_rejects_scope_changes_mixed_teams_and_wrong_run_subject():
    product = DataProductManifest(product="bars@1", title="Bars", providers=("fixture",))
    security = AgentManifest(agent="analyst@1", title="Security", instructions="Inspect.", required_products=("bars@1",))
    market = AgentManifest(
        agent="overview@1", scope="market", title="Market", instructions="Inspect.", required_products=("bars@1",)
    )
    catalog = ManifestCatalog(
        products={"bars@1": product},
        agents={"analyst@1": security, "overview@1": market},
        teams={"security_team@1": ResearchTeam(team="security_team@1", title="Security", agents=("analyst@1",))},
        pipelines={},
        execution_policies={},
    ).validate()
    assert catalog.validate_subject_for_team("security_team@1", ResearchSubject(code="600519")).scope == ResearchScope.security
    with pytest.raises(ValueError, match="does not match"):
        catalog.validate_subject_for_team("security_team@1", ResearchSubject(scope="market"))

    mixed = ManifestCatalog(
        products={"bars@1": product},
        agents={"analyst@1": security, "overview@1": market},
        teams={"mixed@1": ResearchTeam(team="mixed@1", scope="market", title="Mixed", agents=("analyst@1", "overview@1"))},
        pipelines={},
        execution_policies={},
    )
    with pytest.raises(ValueError, match="does not match Agent"):
        mixed.validate()

    changed = ManifestCatalog(
        products={"bars@1": product},
        agents={
            "analyst@1": security,
            "analyst@2": AgentManifest(agent="analyst@2", scope="market", title="Changed", instructions="Inspect.", required_products=("bars@1",)),
        },
        teams={},
        pipelines={},
        execution_policies={},
    )
    with pytest.raises(ValueError, match="cannot change"):
        changed.validate()


def test_agent_manifest_rejects_missing_product():
    with pytest.raises(ValueError):
        AgentManifest(
            agent="market@1",
            title="Market",
            instructions="inspect market data",
            required_products=(),
        )


def test_team_manifest_rejects_duplicate_agents():
    with pytest.raises(ValueError):
        ResearchTeam(team="core@1", title="Core", agents=("market@1", "market@1"))


def test_catalog_validates_references_and_cycles(tmp_path: Path):
    for directory in ("products", "agents", "teams", "pipelines", "execution"):
        (tmp_path / "config" / "research" / directory).mkdir(parents=True)
    (tmp_path / "config" / "research" / "products" / "bars.yaml").write_text(
        """
product: {id: bars, version: 1}
title: Daily bars
providers: [sina]
result_schema: {type: object}
""",
        encoding="utf-8",
    )
    (tmp_path / "config" / "research" / "agents" / "market.yaml").write_text(
        """
agent: {id: market, version: 1}
title: Market
instructions: Inspect bars.
required_products: [{id: bars, version: 1}]
""",
        encoding="utf-8",
    )
    (tmp_path / "config" / "research" / "teams" / "core.yaml").write_text(
        """
team: {id: core, version: 1}
title: Core
agents: [{id: market, version: 1}]
""",
        encoding="utf-8",
    )
    (tmp_path / "config" / "research" / "pipelines" / "decision.yaml").write_text(
        """
pipeline: {id: decision, version: 1}
stages: [quality]
""",
        encoding="utf-8",
    )
    (tmp_path / "config" / "research" / "execution" / "codex.yaml").write_text(
        """
policy: {id: codex, version: 1}
model: gpt-test
reasoning_effort: medium
timeout_seconds: 60
max_agent_concurrency: 2
max_stage_concurrency: 2
""",
        encoding="utf-8",
    )

    catalog = load_catalog(tmp_path)
    assert str(catalog.team("core@1").team) == "core@1"
    assert str(catalog.agent("market@1").required_products[0]) == "bars@1"


def test_catalog_rejects_code_agent_path_escape():
    catalog = ManifestCatalog(
        products={"bars@1": DataProductManifest(product="bars@1", title="Bars", providers=("fixture",))},
        agents={
            "market@1": AgentManifest(
                agent="market@1",
                title="Market",
                instructions="Inspect bars.",
                required_products=("bars@1",),
                implementation="code",
                code_path="advisor/../outside.py",
            )
        },
        teams={"core@1": ResearchTeam(team="core@1", title="Core", agents=("market@1",))},
        pipelines={},
        execution_policies={},
    )
    with pytest.raises(ValueError, match="inside advisor"):
        catalog.validate()
