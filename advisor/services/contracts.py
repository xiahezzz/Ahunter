"""Small, read-mostly contracts for independently managed A Hunter services."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceDescriptor:
    service_id: str
    label: str | None
    title: str
    log_paths: tuple[str, ...]
    mutable: bool


@dataclass(frozen=True)
class ServiceStatus:
    service_id: str
    status: str
    details: str
    log_paths: tuple[str, ...]
    extra: dict[str, object]
