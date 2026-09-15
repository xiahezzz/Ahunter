"""Immutable, repository-owned publication of Research Team manifests."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import re
import tempfile
from typing import Iterator, Sequence

import yaml

from advisor.research.catalog import ManifestCatalog, load_catalog_from_directory
from advisor.research.contracts import ResearchScope, ResearchTeam, VersionRef


_CHINESE_TEXT = re.compile(r"[\u3400-\u9fff]")


class TeamPublicationError(Exception):
    """A Team Manifest could not be published safely."""


class TeamPublicationValidationError(TeamPublicationError, ValueError):
    """The caller supplied an invalid Team identity or member selection."""


class TeamPublicationNotFound(TeamPublicationValidationError):
    """A requested stable Agent identity is absent from the current Catalog."""


class TeamPublicationConflict(TeamPublicationError):
    """The requested publication conflicts with an immutable Team definition."""


class TeamPublicationStorageError(TeamPublicationError):
    """The repository could not atomically create a new immutable manifest."""


@dataclass(frozen=True)
class PublishedTeam:
    team: ResearchTeam
    path: Path
    created: bool

    @property
    def team_ref(self) -> str:
        return str(self.team.team)


class TeamPublicationService:
    """Publishes one new immutable Team version without starting research work."""

    def __init__(self, catalog_dir: Path) -> None:
        self.catalog_dir = Path(catalog_dir).resolve()
        self.teams_dir = self.catalog_dir / "teams"

    def publish(
        self,
        team_id: str,
        title: str,
        agent_ids: Sequence[str],
        *,
        scope: ResearchScope | str | None,
    ) -> PublishedTeam:
        normalized_team_id = _stable_id(team_id, label="团队 ID")
        normalized_title = _title(title)
        normalized_agent_ids = _agent_ids(agent_ids)
        normalized_scope = _scope(scope)
        # Validate all current Catalog references before creating even a lock file.
        self._resolved_agents(load_catalog_from_directory(self.catalog_dir), normalized_agent_ids, normalized_scope)

        with self._publication_lock():
            catalog = load_catalog_from_directory(self.catalog_dir)
            agents = self._resolved_agents(catalog, normalized_agent_ids, normalized_scope)
            self._reject_duplicate_style(catalog, normalized_team_id, agents, normalized_scope)
            latest = _latest_team(catalog, normalized_team_id)
            if latest is not None and latest.scope != normalized_scope:
                raise TeamPublicationValidationError("同一研究团队 ID 不能改变研究范围")
            if latest is not None and latest.title == normalized_title and latest.agents == agents:
                return PublishedTeam(latest, self._manifest_path(latest.team), created=False)

            version = _next_team_version(catalog, normalized_team_id)
            team = ResearchTeam(
                team=VersionRef(id=normalized_team_id, version=version),
                scope=normalized_scope,
                title=normalized_title,
                agents=agents,
            )
            path = self._manifest_path(team.team)
            self._create_manifest(path, team)

            reloaded = load_catalog_from_directory(self.catalog_dir)
            return PublishedTeam(reloaded.team(team.team), path, created=True)

    def _resolved_agents(
        self,
        catalog: ManifestCatalog,
        agent_ids: tuple[str, ...],
        scope: ResearchScope,
    ) -> tuple[VersionRef, ...]:
        latest: list[VersionRef] = []
        for agent_id in agent_ids:
            versions = [item for item in catalog.agents.values() if item.agent.id == agent_id]
            if not versions:
                raise TeamPublicationNotFound("未找到指定的研究 Agent")
            current = max(versions, key=lambda item: item.agent.version)
            if current.scope != scope:
                raise TeamPublicationValidationError("研究团队只能选择相同研究范围的 Agent")
            latest.append(current.agent)
        return tuple(latest)

    def _reject_duplicate_style(
        self,
        catalog: ManifestCatalog,
        team_id: str,
        agents: tuple[VersionRef, ...],
        scope: ResearchScope,
    ) -> None:
        requested = frozenset(agent.id for agent in agents)
        for existing in catalog.teams.values():
            if existing.team.id == team_id:
                continue
            if existing.scope == scope and frozenset(agent.id for agent in existing.agents) == requested:
                raise TeamPublicationConflict("其他研究团队已使用相同成员组合")

    @contextmanager
    def _publication_lock(self) -> Iterator[None]:
        try:
            self.teams_dir.mkdir(parents=True, exist_ok=True)
            lock_path = self.teams_dir / ".team-publication.lock"
            with lock_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as error:
            raise TeamPublicationStorageError("研究团队发布暂不可用") from error

    def _manifest_path(self, team: VersionRef) -> Path:
        return self.teams_dir / f"{team.id}_v{team.version}.yaml"

    def _create_manifest(self, path: Path, team: ResearchTeam) -> None:
        payload = yaml.safe_dump(
            team.model_dump(mode="json"),
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        descriptor = -1
        temporary = ""
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=str(self.teams_dir)
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, path)
            _sync_directory(self.teams_dir)
        except FileExistsError as error:
            raise TeamPublicationConflict("目标团队版本已存在") from error
        except OSError as error:
            raise TeamPublicationStorageError("研究团队发布暂不可用") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass


def _stable_id(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise TeamPublicationValidationError(f"{label}格式不正确")
    try:
        return VersionRef(id=value, version=1).id
    except (TypeError, ValueError) as error:
        raise TeamPublicationValidationError(f"{label}必须为小写下划线名称") from error


def _title(value: object) -> str:
    if not isinstance(value, str):
        raise TeamPublicationValidationError("团队名称格式不正确")
    normalized = value.strip()
    if not normalized or len(normalized) > 200 or _CHINESE_TEXT.search(normalized) is None:
        raise TeamPublicationValidationError("团队名称必须包含中文")
    return normalized


def _agent_ids(value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence) or not value:
        raise TeamPublicationValidationError("请至少选择一个研究 Agent")
    ids = tuple(_stable_id(item, label="研究 Agent ID") for item in value)
    if len(ids) != len(set(ids)):
        raise TeamPublicationValidationError("同一团队不能重复选择研究 Agent")
    return tuple(sorted(ids))


def _scope(value: object) -> ResearchScope:
    if value is None:
        raise TeamPublicationValidationError("创建研究团队时必须选择研究范围")
    try:
        return ResearchScope(value)
    except (TypeError, ValueError) as error:
        raise TeamPublicationValidationError("研究范围无效") from error


def _latest_team(catalog: ManifestCatalog, team_id: str) -> ResearchTeam | None:
    teams = [item for item in catalog.teams.values() if item.team.id == team_id]
    return max(teams, key=lambda item: item.team.version) if teams else None


def _next_team_version(catalog: ManifestCatalog, team_id: str) -> int:
    versions = [item.team.version for item in catalog.teams.values() if item.team.id == team_id]
    return max(versions, default=0) + 1


def _sync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
