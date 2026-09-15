"""The exact published Team versions enabled for scheduled daily batches."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import copy
import fcntl
import os
from pathlib import Path
import tempfile
from typing import Any, Iterator

import yaml

from advisor.config import AdvisorConfig, resolve_research_catalog
from advisor.research.catalog import ManifestCatalog, load_catalog_from_directory
from advisor.research.contracts import VersionRef


class DailyTeamSetError(Exception):
    """The daily Team configuration is not safe to read or change."""


class DailyTeamSetValidationError(DailyTeamSetError, ValueError):
    """A requested Team reference is invalid or not currently published."""


class DailyTeamSetNotFound(DailyTeamSetValidationError):
    """A requested exact Team version is not published in the current Catalog."""


class DailyTeamSetStorageError(DailyTeamSetError):
    """The Advisor configuration could not be atomically updated."""


@dataclass(frozen=True)
class DailyTeamSet:
    team_refs: tuple[str, ...]


class DailyTeamSetService:
    """Reads and atomically updates ``research.default_teams`` only."""

    def __init__(self, *, config_path: Path, root: Path) -> None:
        self.root = Path(root).resolve()
        self.config_path = Path(config_path).resolve()
        if not self.config_path.is_relative_to(self.root):
            raise DailyTeamSetValidationError("每日团队配置必须位于项目目录内")

    def read(self) -> DailyTeamSet:
        _payload, _config, _catalog, current = self._load_current()
        return DailyTeamSet(current)

    def enable(self, team_ref: str) -> DailyTeamSet:
        requested = _reference(team_ref)
        with self._configuration_lock():
            payload, config, catalog, current = self._load_current()
            self._published(catalog, requested)
            updated: list[str] = []
            inserted = False
            for existing_text in current:
                existing = VersionRef.parse(existing_text)
                if existing.id != requested.id:
                    updated.append(existing_text)
                    continue
                if not inserted:
                    updated.append(str(requested))
                    inserted = True
            if not inserted:
                updated.append(str(requested))
            result = tuple(updated)
            if result != current:
                self._write_and_verify(payload, config, catalog, result)
            return DailyTeamSet(result)

    def disable(self, team_ref: str) -> DailyTeamSet:
        requested = _reference(team_ref)
        with self._configuration_lock():
            payload, config, catalog, current = self._load_current()
            self._published(catalog, requested)
            result = tuple(item for item in current if item != str(requested))
            if result != current:
                self._write_and_verify(payload, config, catalog, result)
            return DailyTeamSet(result)

    def _load_current(self) -> tuple[dict[str, Any], AdvisorConfig, ManifestCatalog, tuple[str, ...]]:
        payload = _yaml_mapping(self.config_path)
        research = payload.get("research")
        if isinstance(research, dict) and isinstance(research.get("default_teams"), list):
            raw_refs = research["default_teams"]
            try:
                raw_ids = [VersionRef.parse(item).id for item in raw_refs]
            except (TypeError, ValueError) as error:
                raise DailyTeamSetValidationError("每日团队配置无效") from error
            if len(raw_ids) != len(set(raw_ids)):
                raise DailyTeamSetValidationError("同一团队不能同时启用多个版本")
        try:
            config = AdvisorConfig.model_validate(payload)
            catalog = load_catalog_from_directory(resolve_research_catalog(config, self.root))
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise DailyTeamSetValidationError("每日团队配置或目录无效") from error
        refs = tuple(str(VersionRef.parse(item)) for item in config.research.default_teams)
        stable_ids: set[str] = set()
        for reference in refs:
            parsed = VersionRef.parse(reference)
            if parsed.id in stable_ids:
                raise DailyTeamSetValidationError("同一团队不能同时启用多个版本")
            stable_ids.add(parsed.id)
            self._published(catalog, parsed)
        return payload, config, catalog, refs

    def _published(self, catalog: ManifestCatalog, reference: VersionRef) -> None:
        try:
            catalog.team(reference)
        except ValueError as error:
            raise DailyTeamSetNotFound("指定的研究团队尚未发布") from error

    def _write_and_verify(
        self,
        payload: dict[str, Any],
        config: AdvisorConfig,
        catalog: ManifestCatalog,
        team_refs: tuple[str, ...],
    ) -> None:
        updated = copy.deepcopy(payload)
        research = updated.get("research")
        if not isinstance(research, dict):
            raise DailyTeamSetValidationError("每日团队配置无效")
        research["default_teams"] = list(team_refs)
        try:
            AdvisorConfig.model_validate(updated)
            for team_ref in team_refs:
                self._published(catalog, VersionRef.parse(team_ref))
        except ValueError as error:
            raise DailyTeamSetValidationError("每日团队配置无效") from error
        _atomic_replace_yaml(self.config_path, updated)
        _payload, _config, _catalog, verified = self._load_current()
        if verified != team_refs:
            raise DailyTeamSetStorageError("每日团队配置写入后校验失败")

    @contextmanager
    def _configuration_lock(self) -> Iterator[None]:
        lock_path = self.config_path.with_name(f".{self.config_path.name}.daily-teams.lock")
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as error:
            raise DailyTeamSetStorageError("每日团队配置暂不可更新") from error


def _reference(value: object) -> VersionRef:
    try:
        return VersionRef.parse(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise DailyTeamSetValidationError("团队版本引用格式不正确") from error


def _yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise DailyTeamSetValidationError("每日团队配置不可读取") from error
    if not isinstance(value, dict):
        raise DailyTeamSetValidationError("每日团队配置无效")
    return value


def _atomic_replace_yaml(path: Path, payload: dict[str, Any]) -> None:
    descriptor = -1
    temporary = ""
    try:
        encoded = yaml.safe_dump(payload, allow_unicode=True, default_flow_style=False, sort_keys=False).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    except (OSError, yaml.YAMLError) as error:
        raise DailyTeamSetStorageError("每日团队配置暂不可更新") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


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
