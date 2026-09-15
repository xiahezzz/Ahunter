from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from advisor.mx.rid_authorization import RidAuthorizationStore
from advisor.research.agent_access_publication import (
    AgentAccessPublicationConflict,
    AgentAccessPublicationNotFound,
    AgentAccessPublicationService,
    AgentAccessPublicationValidationError,
)
from advisor.research.catalog import load_catalog_from_directory


def _catalog(tmp_path: Path) -> tuple[Path, Path, Path]:
    catalog = tmp_path / "catalog"
    for directory in ("products", "agents", "teams", "pipelines", "execution"):
        (catalog / directory).mkdir(parents=True)
    (catalog / "products" / "identity.yaml").write_text(
        """
product: identity@1
title: 身份资料
providers: [fixture]
""",
        encoding="utf-8",
    )
    (catalog / "products" / "mx.yaml").write_text(
        """
product: mx_events@2
title: RID MX 资讯
providers: [local-mx]
feed_scope: {kind: mx_rid_feeds}
""",
        encoding="utf-8",
    )
    base = catalog / "agents" / "social.yaml"
    base.write_text(
        """
agent: social@1
title: 资讯研究
instructions: |
  只使用固定输入。
required_products: [identity@1]
details_schema: {type: object, properties: {attention: {type: string}}}
query_budget: 17
max_result_rows: 123
max_result_bytes: 45678
implementation: declarative
""".lstrip(),
        encoding="utf-8",
    )
    (catalog / "teams" / "core.yaml").write_text(
        """
team: core@1
title: 核心
agents: [social@1]
""".lstrip(),
        encoding="utf-8",
    )
    allowed = tmp_path / "allowed-rids.yaml"
    allowed.write_text("allowed_rids: [111, 222]\n", encoding="utf-8")
    return catalog, allowed, base


def test_access_revision_preserves_legacy_manifest_and_all_non_access_contract_fields(tmp_path: Path):
    catalog_dir, allowed, base_path = _catalog(tmp_path)
    original = base_path.read_bytes()
    catalog = load_catalog_from_directory(catalog_dir)
    base = catalog.agent("social@1")

    published = AgentAccessPublicationService(catalog_dir, allowed_rids_path=allowed).publish(
        "social@1",
        [{"product": "mx_events@2", "feed_scope": {"rids": [111]}}],
        expected_rid_version=RidAuthorizationStore(allowed).read().version,
    )

    assert published.created is True
    assert published.agent_ref == "social@2"
    assert base_path.read_bytes() == original
    assert published.path.name == "social_v2.yaml"
    assert published.agent.required_products is None
    assert [str(access.product) for access in published.agent.product_accesses] == ["mx_events@2"]
    assert published.agent.product_accesses[0].feed_scope.rids == (111,)
    assert (
        published.agent.title,
        published.agent.instructions,
        published.agent.details_schema,
        published.agent.query_budget,
        published.agent.max_result_rows,
        published.agent.max_result_bytes,
        published.agent.implementation,
        published.agent.code_path,
    ) == (
        base.title,
        base.instructions,
        base.details_schema,
        base.query_budget,
        base.max_result_rows,
        base.max_result_bytes,
        base.implementation,
        base.code_path,
    )
    assert published.impact.fixed_team_refs == ("core@1",)
    assert "required_products" not in published.path.read_text(encoding="utf-8")


def test_access_publication_is_idempotent_and_conflicts_on_a_different_stale_revision(tmp_path: Path):
    catalog_dir, allowed, _base = _catalog(tmp_path)
    service = AgentAccessPublicationService(catalog_dir, allowed_rids_path=allowed)
    version = RidAuthorizationStore(allowed).read().version

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda _item: service.publish(
                "social@1",
                [{"product": "mx_events@2", "feed_scope": {"rids": [111]}}],
                expected_rid_version=version,
            ),
            range(2),
        ))
    assert {item.agent_ref for item in results} == {"social@2"}
    assert sum(item.created for item in results) == 1

    with pytest.raises(AgentAccessPublicationConflict, match="基础 Agent"):
        service.publish(
            "social@1",
            [{"product": "mx_events@2", "feed_scope": {"rids": [222]}}],
            expected_rid_version=version,
        )
    assert sorted(path.name for path in (catalog_dir / "agents").glob("*.yaml")) == ["social.yaml", "social_v2.yaml"]


@pytest.mark.parametrize(
    ("base_ref", "access", "error"),
    [
        ("missing@1", [{"product": "mx_events@2", "feed_scope": {"rids": [111]}}], AgentAccessPublicationNotFound),
        ("social@1", [], AgentAccessPublicationValidationError),
        ("social@1", [{"product": "missing@1"}], AgentAccessPublicationValidationError),
        ("social@1", [{"product": "mx_events@2", "feed_scope": {"rids": [333]}}], AgentAccessPublicationValidationError),
        ("social@1", [{"product": "mx_events@2"}], AgentAccessPublicationValidationError),
    ],
)
def test_invalid_access_publication_never_writes_a_manifest(
    tmp_path: Path,
    base_ref: str,
    access: list[dict[str, object]],
    error: type[Exception],
):
    catalog_dir, allowed, base = _catalog(tmp_path)
    original = base.read_bytes()
    service = AgentAccessPublicationService(catalog_dir, allowed_rids_path=allowed)

    with pytest.raises(error):
        service.publish(base_ref, access)

    assert base.read_bytes() == original
    assert not list((catalog_dir / "agents").glob("*_v*.yaml"))
    assert not list(tmp_path.rglob("*.sqlite"))
    assert not (tmp_path / "artifacts").exists()


def test_impact_marks_a_revoked_exact_rid_without_mutating_teams(tmp_path: Path):
    catalog_dir, allowed, _base = _catalog(tmp_path)
    service = AgentAccessPublicationService(catalog_dir, allowed_rids_path=allowed)
    published = service.publish("social@1", [{"product": "mx_events@2", "feed_scope": {"rids": [111]}}])
    allowed.write_text("allowed_rids: [222]\n", encoding="utf-8")

    impact = service.impact(published.agent_ref)

    assert impact.fixed_team_refs == ("core@1",)
    assert impact.revoked_rids == (111,)
    assert load_catalog_from_directory(catalog_dir).team("core@1").agents[0].version == 1
