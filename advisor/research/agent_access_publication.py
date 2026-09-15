"""Immutable publication of an Agent's explicit Data Product access grants."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from advisor.mx.rid_authorization import (
    RidAuthorizationStore,
    RidAuthorizationUnavailable,
    RidAuthorizationValidationError,
)
from advisor.research.agent_manifest_publication import (
    agent_manifest_path,
    agent_manifest_publication_lock,
    create_agent_manifest,
    fixed_team_refs,
    latest_agent,
)
from advisor.research.catalog import ManifestCatalog, load_catalog_from_directory
from advisor.research.contracts import AgentDataAccess, AgentManifest, ProductAccess, VersionRef


class AgentAccessPublicationError(Exception):
    """Base error for bounded Agent access publication failures."""


class AgentAccessPublicationValidationError(AgentAccessPublicationError, ValueError):
    pass


class AgentAccessPublicationNotFound(AgentAccessPublicationValidationError):
    pass


class AgentAccessPublicationConflict(AgentAccessPublicationError):
    pass


class AgentAccessPublicationStorageError(AgentAccessPublicationError):
    pass


@dataclass(frozen=True)
class AgentAccessImpact:
    fixed_team_refs: tuple[str, ...]
    revoked_rids: tuple[int, ...]


@dataclass(frozen=True)
class PublishedAgentAccess:
    agent: AgentManifest
    path: Path
    created: bool
    impact: AgentAccessImpact

    @property
    def agent_ref(self) -> str:
        return str(self.agent.agent)


class AgentAccessPublicationService:
    """Creates the next immutable Agent version from one exact base version."""

    def __init__(self, catalog_dir: Path, *, allowed_rids_path: Path | None = None) -> None:
        self.catalog_dir = Path(catalog_dir).resolve()
        self.agents_dir = self.catalog_dir / "agents"
        self.allowed_rids_path = Path(allowed_rids_path).expanduser() if allowed_rids_path is not None else None

    def publish(
        self,
        base_agent_ref: str | VersionRef,
        data_access: Sequence[ProductAccess | dict[str, object]],
        *,
        expected_rid_version: str | None = None,
    ) -> PublishedAgentAccess:
        base_ref = _exact_ref(base_agent_ref)
        requested = _access(data_access)
        # Validate before creating any lock or manifest file.
        catalog = load_catalog_from_directory(self.catalog_dir)
        base = self._base(catalog, base_ref)
        authorization = self._authorize_access(catalog, requested, expected_rid_version)

        try:
            with agent_manifest_publication_lock(self.agents_dir):
                catalog = load_catalog_from_directory(self.catalog_dir)
                base = self._base(catalog, base_ref)
                authorization = self._authorize_access(catalog, requested, expected_rid_version)
                existing = self._matching_revision(catalog, base, requested)
                if existing is not None:
                    return PublishedAgentAccess(
                        existing,
                        self._manifest_path(existing.agent),
                        False,
                        self._impact(catalog, existing, authorization.rids if authorization else ()),
                    )
                latest = latest_agent(catalog, base.agent.id)
                if latest is None or latest.agent.version != base.agent.version:
                    raise AgentAccessPublicationConflict("基础 Agent 已有更新版本，请刷新后重试")
                next_agent = _revised_agent(base, requested, version=base.agent.version + 1)
                path = self._manifest_path(next_agent.agent)
                self._create_manifest(path, next_agent)
                reloaded = load_catalog_from_directory(self.catalog_dir)
                published = reloaded.agent(next_agent.agent)
                return PublishedAgentAccess(
                    published,
                    path,
                    True,
                    self._impact(reloaded, published, authorization.rids if authorization else ()),
                )
        except AgentAccessPublicationError:
            raise
        except OSError as error:
            raise AgentAccessPublicationStorageError("Agent 访问发布暂不可用") from error

    def impact(self, agent_ref: str | VersionRef) -> AgentAccessImpact:
        catalog = load_catalog_from_directory(self.catalog_dir)
        agent = self._base(catalog, _exact_ref(agent_ref))
        authorization = self._authorization_if_needed(agent.product_accesses)
        return self._impact(catalog, agent, authorization.rids if authorization else ())

    def _base(self, catalog: ManifestCatalog, ref: VersionRef) -> AgentManifest:
        try:
            return catalog.agent(ref)
        except ValueError as error:
            raise AgentAccessPublicationNotFound("未找到指定的 Agent 版本") from error

    def _authorize_access(
        self,
        catalog: ManifestCatalog,
        access: tuple[ProductAccess, ...],
        expected_rid_version: str | None,
    ):
        for item in access:
            try:
                product = catalog.product(item.product)
            except ValueError as error:
                raise AgentAccessPublicationValidationError("Data Product 不存在") from error
            if item.feed_scope is None:
                if product.product.id == "mx_events" and product.product.version == 2:
                    raise AgentAccessPublicationValidationError("MX 资讯必须逐项选择 RID")
                continue
            if product.product.id != "mx_events" or product.product.version != 2 or not product.feed_scope:
                raise AgentAccessPublicationValidationError("该 Data Product 不支持 RID Feed")
        authorization = self._authorization_if_needed(access, required=expected_rid_version is not None)
        if authorization is not None:
            if expected_rid_version is not None and authorization.version != expected_rid_version:
                raise AgentAccessPublicationConflict("RID 配置已更新，请刷新后再提交")
            granted = set(authorization.rids)
            requested = {
                rid
                for item in access
                if item.feed_scope is not None
                for rid in item.feed_scope.rids
            }
            if not requested.issubset(granted):
                raise AgentAccessPublicationValidationError("MX RID 已撤销或未获授权")
        return authorization

    def _authorization_if_needed(
        self,
        access: Sequence[ProductAccess],
        *,
        required: bool = False,
    ):
        needs_authorization = required or any(item.feed_scope is not None for item in access)
        if not needs_authorization:
            return None
        if self.allowed_rids_path is None:
            raise AgentAccessPublicationStorageError("RID 授权配置暂不可读取")
        try:
            return RidAuthorizationStore(self.allowed_rids_path).read()
        except (RidAuthorizationUnavailable, RidAuthorizationValidationError) as error:
            raise AgentAccessPublicationStorageError("RID 授权配置暂不可读取") from error

    def _matching_revision(
        self,
        catalog: ManifestCatalog,
        base: AgentManifest,
        requested: tuple[ProductAccess, ...],
    ) -> AgentManifest | None:
        for candidate in catalog.agents.values():
            if candidate.agent.id != base.agent.id or candidate.agent.version <= base.agent.version:
                continue
            if candidate.product_accesses != requested:
                continue
            if _immutable_agent_fields(candidate) == _immutable_agent_fields(base):
                return candidate
        return None

    def _impact(
        self,
        catalog: ManifestCatalog,
        agent: AgentManifest,
        authorized_rids: Sequence[int],
    ) -> AgentAccessImpact:
        fixed = fixed_team_refs(catalog, agent)
        allowed = set(authorized_rids)
        revoked = tuple(sorted({
            rid
            for access in agent.product_accesses
            if access.feed_scope is not None
            for rid in access.feed_scope.rids
            if rid not in allowed
        }))
        return AgentAccessImpact(fixed, revoked)

    def _manifest_path(self, agent: VersionRef) -> Path:
        return agent_manifest_path(self.agents_dir, agent)

    def _create_manifest(self, path: Path, agent: AgentManifest) -> None:
        try:
            created = create_agent_manifest(self.agents_dir, agent)
            if created != path:
                raise OSError("unexpected Agent Manifest path")
        except FileExistsError as error:
            raise AgentAccessPublicationConflict("目标 Agent 版本已存在") from error
        except OSError as error:
            raise AgentAccessPublicationStorageError("Agent 访问发布暂不可用") from error


def _exact_ref(value: str | VersionRef) -> VersionRef:
    try:
        return VersionRef.parse(value)
    except (TypeError, ValueError) as error:
        raise AgentAccessPublicationValidationError("基础 Agent 版本格式无效") from error


def _access(value: Sequence[ProductAccess | dict[str, object]]) -> tuple[ProductAccess, ...]:
    if isinstance(value, (str, bytes)):
        raise AgentAccessPublicationValidationError("Data Access 输入无效")
    try:
        return AgentDataAccess(products=tuple(ProductAccess.model_validate(item) for item in value)).products
    except (TypeError, ValueError) as error:
        raise AgentAccessPublicationValidationError("Data Access 输入无效") from error


def _immutable_agent_fields(agent: AgentManifest) -> tuple[object, ...]:
    return (
        agent.scope,
        agent.title,
        agent.instructions,
        agent.details_schema,
        agent.query_budget,
        agent.max_result_rows,
        agent.max_result_bytes,
        agent.implementation,
        agent.code_path,
    )


def _revised_agent(base: AgentManifest, access: tuple[ProductAccess, ...], *, version: int) -> AgentManifest:
    return AgentManifest(
        agent=VersionRef(id=base.agent.id, version=version),
        scope=base.scope,
        title=base.title,
        instructions=base.instructions,
        data_access=access,
        details_schema=base.details_schema,
        query_budget=base.query_budget,
        max_result_rows=base.max_result_rows,
        max_result_bytes=base.max_result_bytes,
        implementation=base.implementation,
        code_path=base.code_path,
    )
