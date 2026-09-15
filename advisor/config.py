from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from advisor.paths import repo_root


class MarketConfig(BaseModel):
    primary: str = "A股"


class ScheduleConfig(BaseModel):
    premarket_time: str = "08:30"
    review_time: str = "22:30"


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database: str
    chart_dir: str = "data/advisor/charts"
    profile_dir: str = "data/advisor/profiles"


class DataSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_tushare: bool = False
    free_sources: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def reject_tushare(self):
        sources = {value.lower() for value in self.free_sources}
        if self.allow_tushare or "tushare" in sources:
            raise ValueError("Tushare is not allowed for required advisor data paths")
        return self


class QualityConfig(BaseModel):
    max_market_data_staleness_minutes: int = 1440
    require_trading_calendar: bool = True


class ResearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog_dir: str = "config/research"
    artifact_dir: str = "data/advisor/research-artifacts"
    default_teams: list[str] = Field(default_factory=lambda: ["a_share_core@1"])
    execution_policy: str = "codex@1"
    max_subjects: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def validate_research_refs(self):
        from advisor.research.contracts import VersionRef

        for value in (self.catalog_dir, self.artifact_dir):
            path = Path(value)
            if not value.strip() or path.is_absolute() or ".." in path.parts:
                raise ValueError("research paths must remain inside the repository")
        teams = [str(VersionRef.parse(item)) for item in self.default_teams]
        if len(teams) != len(set(teams)):
            raise ValueError("default Research Teams must be unique")
        if len({VersionRef.parse(item).id for item in teams}) != len(teams):
            raise ValueError("only one default Research Team version is allowed per Team ID")
        self.default_teams[:] = teams
        self.execution_policy = str(VersionRef.parse(self.execution_policy))
        return self


class AdvisorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: MarketConfig
    schedule: ScheduleConfig
    storage: StorageConfig
    data_sources: DataSourceConfig
    quality: QualityConfig = Field(default_factory=QualityConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)


def load_advisor_config(path: Path | None = None) -> AdvisorConfig:
    filename = path or repo_root() / "config" / "advisor.yaml"
    with filename.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    return AdvisorConfig.model_validate(payload)


def _resolve_storage_path(configured_path: str, root: Path, field_name: str) -> Path:
    configured = Path(configured_path)
    if configured.is_absolute():
        raise ValueError(f"storage {field_name} must be relative to the repository root")
    resolved_root = root.resolve()
    resolved = (resolved_root / configured).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValueError(f"storage {field_name} must remain within the repository root")
    return resolved


def resolve_state_db(config: AdvisorConfig, root: Path) -> Path:
    return _resolve_storage_path(config.storage.database, root, "database")


def resolve_chart_dir(config: AdvisorConfig, root: Path) -> Path:
    return _resolve_storage_path(config.storage.chart_dir, root, "chart_dir")


def resolve_profile_dir(config: AdvisorConfig, root: Path) -> Path:
    return _resolve_storage_path(config.storage.profile_dir, root, "profile_dir")


def resolve_research_catalog(config: AdvisorConfig, root: Path) -> Path:
    return _resolve_storage_path(config.research.catalog_dir, root, "catalog_dir")


def resolve_research_artifact_dir(config: AdvisorConfig, root: Path) -> Path:
    return _resolve_storage_path(config.research.artifact_dir, root, "artifact_dir")
