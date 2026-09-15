"""Shared atomic write boundary for immutable Agent Manifest versions."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import tempfile
from typing import Iterator

import yaml

from advisor.research.catalog import ManifestCatalog
from advisor.research.contracts import AgentManifest, VersionRef


@contextmanager
def agent_manifest_publication_lock(agents_dir: Path) -> Iterator[None]:
    """Serialize every kind of Agent Manifest publication in one catalog."""

    agents_dir.mkdir(parents=True, exist_ok=True)
    lock_path = agents_dir / ".agent-manifest-publication.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def agent_manifest_path(agents_dir: Path, agent: VersionRef) -> Path:
    return agents_dir / f"{agent.id}_v{agent.version}.yaml"


def create_agent_manifest(agents_dir: Path, agent: AgentManifest) -> Path:
    """Durably create one Manifest without ever replacing an existing path."""

    path = agent_manifest_path(agents_dir, agent.agent)
    payload = yaml.safe_dump(
        agent_manifest_payload(agent),
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    descriptor = -1
    temporary = ""
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(agents_dir)
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        _sync_directory(agents_dir)
        return path
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def agent_manifest_payload(agent: AgentManifest) -> dict[str, object]:
    return {
        "agent": agent.agent.model_dump(mode="json"),
        "scope": agent.scope.value,
        "title": agent.title,
        "instructions": agent.instructions,
        "data_access": [item.model_dump(mode="json") for item in agent.product_accesses],
        "details_schema": agent.details_schema,
        "query_budget": agent.query_budget,
        "max_result_rows": agent.max_result_rows,
        "max_result_bytes": agent.max_result_bytes,
        "implementation": agent.implementation,
        "code_path": agent.code_path,
    }


def latest_agent(catalog: ManifestCatalog, agent_id: str) -> AgentManifest | None:
    candidates = [item for item in catalog.agents.values() if item.agent.id == agent_id]
    return max(candidates, key=lambda item: item.agent.version) if candidates else None


def fixed_team_refs(catalog: ManifestCatalog, agent: AgentManifest) -> tuple[str, ...]:
    """Return Team versions that remain pinned away from this Agent version."""

    return tuple(sorted(
        str(team.team)
        for team in catalog.teams.values()
        if any(member.id == agent.agent.id and member.version != agent.agent.version for member in team.agents)
    ))


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
