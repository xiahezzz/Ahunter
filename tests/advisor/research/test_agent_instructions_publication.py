from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from advisor.mx.rid_authorization import RidAuthorizationStore
from advisor.research.agent_access_publication import (
    AgentAccessPublicationConflict,
    AgentAccessPublicationService,
)
from advisor.research.agent_instructions_publication import (
    AgentInstructionsPublicationConflict,
    AgentInstructionsPublicationNotFound,
    AgentInstructionsPublicationService,
    AgentInstructionsPublicationValidationError,
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
""".lstrip(),
        encoding="utf-8",
    )
    (catalog / "products" / "mx.yaml").write_text(
        """
product: mx_events@2
title: RID MX 资讯
providers: [local-mx]
feed_scope: {kind: mx_rid_feeds}
""".lstrip(),
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


def test_instruction_revision_preserves_history_and_every_non_instruction_field(tmp_path: Path):
    catalog_dir, _allowed, base_path = _catalog(tmp_path)
    original = base_path.read_bytes()
    base = load_catalog_from_directory(catalog_dir).agent("social@1")

    published = AgentInstructionsPublicationService(catalog_dir).publish(
        "social@1",
        "只使用固定输入，并逐条引用证据。",
    )

    assert published.created is True
    assert published.agent_ref == "social@2"
    assert published.path.name == "social_v2.yaml"
    assert base_path.read_bytes() == original
    assert published.agent.instructions == "只使用固定输入，并逐条引用证据。"
    assert published.agent.required_products is None
    assert (
        published.agent.title,
        published.agent.product_accesses,
        published.agent.details_schema,
        published.agent.query_budget,
        published.agent.max_result_rows,
        published.agent.max_result_bytes,
        published.agent.implementation,
        published.agent.code_path,
    ) == (
        base.title,
        base.product_accesses,
        base.details_schema,
        base.query_budget,
        base.max_result_rows,
        base.max_result_bytes,
        base.implementation,
        base.code_path,
    )
    assert published.impact.fixed_team_refs == ("core@1",)
    assert load_catalog_from_directory(catalog_dir).team("core@1").agents[0].version == 1
    assert "required_products" not in published.path.read_text(encoding="utf-8")


def test_instruction_publication_is_idempotent_and_rejects_a_different_stale_revision(tmp_path: Path):
    catalog_dir, _allowed, _base = _catalog(tmp_path)
    service = AgentInstructionsPublicationService(catalog_dir)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda _item: service.publish("social@1", "新的研究指令。"),
            range(2),
        ))

    assert {item.agent_ref for item in results} == {"social@2"}
    assert sum(item.created for item in results) == 1
    with pytest.raises(AgentInstructionsPublicationConflict, match="基础 Agent"):
        service.publish("social@1", "另一个过期草稿。")
    assert sorted(path.name for path in (catalog_dir / "agents").glob("*.yaml")) == ["social.yaml", "social_v2.yaml"]


def test_same_instructions_are_a_noop_and_historical_text_is_restored_as_the_next_version(tmp_path: Path):
    catalog_dir, _allowed, _base = _catalog(tmp_path)
    service = AgentInstructionsPublicationService(catalog_dir)
    original = load_catalog_from_directory(catalog_dir).agent("social@1").instructions

    unchanged = service.publish("social@1", original)
    second = service.publish("social@1", "第二版指令。")
    restored = service.publish("social@2", original)

    assert (unchanged.agent_ref, unchanged.created) == ("social@1", False)
    assert unchanged.path is None
    assert (second.agent_ref, restored.agent_ref) == ("social@2", "social@3")
    assert restored.agent.instructions == original
    assert sorted(path.name for path in (catalog_dir / "agents").glob("*.yaml")) == [
        "social.yaml",
        "social_v2.yaml",
        "social_v3.yaml",
    ]


@pytest.mark.parametrize(
    ("base_ref", "instructions", "error"),
    [
        ("missing@1", "有效指令。", AgentInstructionsPublicationNotFound),
        ("social@1", "", AgentInstructionsPublicationValidationError),
        ("social@1", "   \n", AgentInstructionsPublicationValidationError),
        ("social@1", "x" * 20_001, AgentInstructionsPublicationValidationError),
    ],
)
def test_invalid_instruction_publication_never_writes_a_manifest(
    tmp_path: Path,
    base_ref: str,
    instructions: str,
    error: type[Exception],
):
    catalog_dir, _allowed, base = _catalog(tmp_path)
    original = base.read_bytes()

    with pytest.raises(error):
        AgentInstructionsPublicationService(catalog_dir).publish(base_ref, instructions)

    assert base.read_bytes() == original
    assert not list((catalog_dir / "agents").glob("*_v*.yaml"))


def test_access_and_instruction_revisions_share_one_agent_version_lock(tmp_path: Path):
    catalog_dir, allowed, _base = _catalog(tmp_path)
    rid_version = RidAuthorizationStore(allowed).read().version

    def publish_access():
        return AgentAccessPublicationService(catalog_dir, allowed_rids_path=allowed).publish(
            "social@1",
            [{"product": "mx_events@2", "feed_scope": {"rids": [111]}}],
            expected_rid_version=rid_version,
        )

    def publish_instructions():
        return AgentInstructionsPublicationService(catalog_dir).publish("social@1", "新的研究指令。")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(publish_access), executor.submit(publish_instructions)]
    outcomes: list[object] = []
    errors: list[BaseException] = []
    for future in futures:
        try:
            outcomes.append(future.result())
        except BaseException as error:  # noqa: BLE001 - assert the typed race outcome
            errors.append(error)

    assert len(outcomes) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], (AgentAccessPublicationConflict, AgentInstructionsPublicationConflict))
    assert sorted(path.name for path in (catalog_dir / "agents").glob("*.yaml")) == ["social.yaml", "social_v2.yaml"]
