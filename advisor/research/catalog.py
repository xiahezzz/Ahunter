from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Generic, TypeVar

import yaml

from advisor.paths import repo_root
from advisor.research.contracts import (
    AgentManifest,
    DataProductManifest,
    DecisionPipelineManifest,
    ExecutionPolicy,
    ResearchScope,
    ResearchSubject,
    ResearchTeam,
    VersionRef,
)


T = TypeVar("T")


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"invalid manifest file: {path.name}") from error
    if not isinstance(value, dict):
        raise ValueError(f"manifest must be an object: {path.name}")
    return value


def _files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(item for item in directory.iterdir() if item.suffix in {".yaml", ".yml", ".json"})


def _load_many(directory: Path, model: type[T], label: str) -> dict[str, T]:
    result: dict[str, T] = {}
    for path in _files(directory):
        item = model.model_validate(_load_yaml(path))
        ref = getattr(item, label)
        key = str(ref)
        if key in result:
            raise ValueError(f"duplicate {label}: {key}")
        result[key] = item
    return result


@dataclass(frozen=True)
class ManifestCatalog:
    products: dict[str, DataProductManifest]
    agents: dict[str, AgentManifest]
    teams: dict[str, ResearchTeam]
    pipelines: dict[str, DecisionPipelineManifest]
    execution_policies: dict[str, ExecutionPolicy]

    def product(self, ref: str | VersionRef) -> DataProductManifest:
        key = str(VersionRef.parse(ref))
        try:
            return self.products[key]
        except KeyError as error:
            raise ValueError(f"unknown Data Product: {key}") from error

    def agent(self, ref: str | VersionRef) -> AgentManifest:
        key = str(VersionRef.parse(ref))
        try:
            return self.agents[key]
        except KeyError as error:
            raise ValueError(f"unknown Research Agent: {key}") from error

    def latest_agent(self, agent_id: str) -> AgentManifest:
        return _latest_by_id(self.agents, agent_id, "Research Agent")

    def team(self, ref: str | VersionRef) -> ResearchTeam:
        key = str(VersionRef.parse(ref))
        try:
            return self.teams[key]
        except KeyError as error:
            raise ValueError(f"unknown Research Team: {key}") from error

    def latest_team(self, team_id: str) -> ResearchTeam:
        return _latest_by_id(self.teams, team_id, "Research Team")

    def pipeline(self, ref: str | VersionRef) -> DecisionPipelineManifest:
        key = str(VersionRef.parse(ref))
        try:
            return self.pipelines[key]
        except KeyError as error:
            raise ValueError(f"unknown Decision Pipeline: {key}") from error

    def execution_policy(self, ref: str | VersionRef) -> ExecutionPolicy:
        key = str(VersionRef.parse(ref))
        try:
            return self.execution_policies[key]
        except KeyError as error:
            raise ValueError(f"unknown Codex Execution Policy: {key}") from error

    def validate(self) -> ManifestCatalog:
        for product in self.products.values():
            for dependency in product.dependencies:
                self.product(dependency)
        for agent in self.agents.values():
            for access in agent.product_accesses:
                product = self.product(access.product)
                if access.feed_scope is not None and not product.feed_scope:
                    raise ValueError(f"Data Product does not support a feed scope: {access.product}")
                if product.product.id == "mx_events" and product.product.version == 2:
                    if access.feed_scope is None:
                        raise ValueError("mx_events@2 requires an exact MX RID feed scope")
                elif access.feed_scope is not None:
                    raise ValueError(f"Data Product does not support an MX RID feed scope: {access.product}")
            if agent.implementation == "code" and agent.code_path and not _is_internal_code_path(agent.code_path):
                raise ValueError(f"code-backed Agent must stay inside advisor/: {agent.agent}")
        self._validate_stable_scopes(self.agents, "agent", "Research Agent")
        for team in self.teams.values():
            for agent in team.agents:
                member = self.agent(agent)
                if member.scope != team.scope:
                    raise ValueError(f"Research Team Scope does not match Agent Scope: {team.team}")
        self._validate_stable_scopes(self.teams, "team", "Research Team")
        self._validate_product_cycles()
        return self

    def validate_subject_for_team(
        self,
        team_ref: str | VersionRef,
        subject: ResearchSubject,
    ) -> ResearchTeam:
        """Fail closed before a shared lifecycle sees a wrong subject shape."""
        team = self.team(team_ref)
        if team.scope != subject.scope:
            raise ValueError(
                f"Research Subject Scope {subject.scope.value} does not match Team Scope {team.scope.value}: {team.team}"
            )
        return team

    @staticmethod
    def _validate_stable_scopes(items: dict[str, T], attribute: str, label: str) -> None:
        scopes: dict[str, ResearchScope] = {}
        for item in items.values():
            ref = getattr(item, attribute)
            scope = getattr(item, "scope")
            existing = scopes.setdefault(ref.id, scope)
            if existing != scope:
                raise ValueError(f"{label} Scope cannot change across versions: {ref.id}")

    def _validate_product_cycles(self) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise ValueError(f"cyclic Data Product dependency: {key}")
            if key in visited:
                return
            visiting.add(key)
            for dependency in self.products[key].dependencies:
                visit(str(dependency))
            visiting.remove(key)
            visited.add(key)

        for key in self.products:
            visit(key)


def load_catalog(root: Path | None = None, *, catalog_dir: Path | None = None) -> ManifestCatalog:
    base = (catalog_dir or ((root or repo_root()).resolve() / "config" / "research")).resolve()
    return load_catalog_from_directory(base)


def load_catalog_from_directory(base: Path) -> ManifestCatalog:
    base = base.resolve()
    catalog = ManifestCatalog(
        products=_load_many(base / "products", DataProductManifest, "product"),
        agents=_load_many(base / "agents", AgentManifest, "agent"),
        teams=_load_many(base / "teams", ResearchTeam, "team"),
        pipelines=_load_many(base / "pipelines", DecisionPipelineManifest, "pipeline"),
        execution_policies=_load_many(base / "execution", ExecutionPolicy, "policy"),
    )
    return catalog.validate()


def _is_internal_code_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and len(path.parts) >= 2
        and path.parts[0] == "advisor"
        and all(part not in {".", ".."} for part in path.parts)
    )


def _latest_by_id(items: dict[str, T], stable_id: str, label: str) -> T:
    try:
        VersionRef(id=stable_id, version=1)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid {label} ID") from error
    matches = [item for item in items.values() if getattr(item, label.split()[-1].lower()).id == stable_id]
    if not matches:
        raise ValueError(f"unknown {label}: {stable_id}")
    return max(matches, key=lambda item: getattr(item, label.split()[-1].lower()).version)
