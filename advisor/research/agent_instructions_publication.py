"""Immutable publication of owner-authored Research Agent Instructions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from advisor.research.agent_manifest_publication import (
    agent_manifest_path,
    agent_manifest_publication_lock,
    create_agent_manifest,
    fixed_team_refs,
    latest_agent,
)
from advisor.research.catalog import ManifestCatalog, load_catalog_from_directory
from advisor.research.contracts import AgentManifest, VersionRef


class AgentInstructionsPublicationError(Exception):
    """Base error for bounded Agent Instructions publication failures."""


class AgentInstructionsPublicationValidationError(AgentInstructionsPublicationError, ValueError):
    pass


class AgentInstructionsPublicationNotFound(AgentInstructionsPublicationValidationError):
    pass


class AgentInstructionsPublicationConflict(AgentInstructionsPublicationError):
    pass


class AgentInstructionsPublicationStorageError(AgentInstructionsPublicationError):
    pass


@dataclass(frozen=True)
class AgentInstructionsImpact:
    fixed_team_refs: tuple[str, ...]


@dataclass(frozen=True)
class PublishedAgentInstructions:
    agent: AgentManifest
    path: Path | None
    created: bool
    impact: AgentInstructionsImpact

    @property
    def agent_ref(self) -> str:
        return str(self.agent.agent)


class AgentInstructionsPublicationService:
    """Create one linear immutable Agent version by changing Instructions only."""

    def __init__(self, catalog_dir: Path) -> None:
        self.catalog_dir = Path(catalog_dir).resolve()
        self.agents_dir = self.catalog_dir / "agents"

    def publish(
        self,
        base_agent_ref: str | VersionRef,
        instructions: str,
    ) -> PublishedAgentInstructions:
        base_ref = _exact_ref(base_agent_ref)
        requested = _instructions(instructions)
        catalog = load_catalog_from_directory(self.catalog_dir)
        self._base(catalog, base_ref)

        try:
            with agent_manifest_publication_lock(self.agents_dir):
                catalog = load_catalog_from_directory(self.catalog_dir)
                base = self._base(catalog, base_ref)
                existing = self._matching_revision(catalog, base, requested)
                if existing is not None:
                    return self._published(catalog, existing, created=False)
                latest = latest_agent(catalog, base.agent.id)
                if latest is None or latest.agent.version != base.agent.version:
                    raise AgentInstructionsPublicationConflict("基础 Agent 已有更新版本，请刷新后重试")
                if requested == base.instructions:
                    return self._published(catalog, base, created=False)
                revised = _revised_agent(base, requested, version=base.agent.version + 1)
                try:
                    create_agent_manifest(self.agents_dir, revised)
                except FileExistsError as error:
                    raise AgentInstructionsPublicationConflict("目标 Agent 版本已存在") from error
                reloaded = load_catalog_from_directory(self.catalog_dir)
                return self._published(reloaded, reloaded.agent(revised.agent), created=True)
        except AgentInstructionsPublicationError:
            raise
        except OSError as error:
            raise AgentInstructionsPublicationStorageError("Agent 指令发布暂不可用") from error

    def _base(self, catalog: ManifestCatalog, ref: VersionRef) -> AgentManifest:
        try:
            return catalog.agent(ref)
        except ValueError as error:
            raise AgentInstructionsPublicationNotFound("未找到指定的 Agent 版本") from error

    def _matching_revision(
        self,
        catalog: ManifestCatalog,
        base: AgentManifest,
        requested: str,
    ) -> AgentManifest | None:
        for candidate in catalog.agents.values():
            if candidate.agent.id != base.agent.id or candidate.agent.version <= base.agent.version:
                continue
            if candidate.instructions != requested:
                continue
            if _non_instruction_fields(candidate) == _non_instruction_fields(base):
                return candidate
        return None

    def _published(
        self,
        catalog: ManifestCatalog,
        agent: AgentManifest,
        *,
        created: bool,
    ) -> PublishedAgentInstructions:
        path = agent_manifest_path(self.agents_dir, agent.agent)
        return PublishedAgentInstructions(
            agent=agent,
            path=path if path.is_file() else None,
            created=created,
            impact=AgentInstructionsImpact(fixed_team_refs(catalog, agent)),
        )


def _exact_ref(value: str | VersionRef) -> VersionRef:
    try:
        return VersionRef.parse(value)
    except (TypeError, ValueError) as error:
        raise AgentInstructionsPublicationValidationError("基础 Agent 版本格式无效") from error


def _instructions(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 20_000:
        raise AgentInstructionsPublicationValidationError("Agent Instructions 输入无效")
    return value


def _non_instruction_fields(agent: AgentManifest) -> tuple[object, ...]:
    return (
        agent.scope,
        agent.title,
        agent.product_accesses,
        agent.details_schema,
        agent.query_budget,
        agent.max_result_rows,
        agent.max_result_bytes,
        agent.implementation,
        agent.code_path,
    )


def _revised_agent(base: AgentManifest, instructions: str, *, version: int) -> AgentManifest:
    return AgentManifest(
        agent=VersionRef(id=base.agent.id, version=version),
        scope=base.scope,
        title=base.title,
        instructions=instructions,
        data_access=base.product_accesses,
        details_schema=base.details_schema,
        query_budget=base.query_budget,
        max_result_rows=base.max_result_rows,
        max_result_bytes=base.max_result_bytes,
        implementation=base.implementation,
        code_path=base.code_path,
    )
