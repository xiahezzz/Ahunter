from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from advisor.paths import repo_root


class MarketConfig(BaseModel):
    primary: str = "A股"


class ScheduleConfig(BaseModel):
    premarket_time: str = "08:30"
    review_time: str = "22:30"


class StorageConfig(BaseModel):
    market_db: str
    advisor_db: str
    chart_dir: str = "data/advisor/charts"
    profile_dir: str = "data/advisor/profiles"


class DataSourceConfig(BaseModel):
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


class AdvisorConfig(BaseModel):
    market: MarketConfig
    schedule: ScheduleConfig
    storage: StorageConfig
    data_sources: DataSourceConfig
    quality: QualityConfig = Field(default_factory=QualityConfig)


def load_advisor_config(path: Path | None = None) -> AdvisorConfig:
    filename = path or repo_root() / "config" / "advisor.yaml"
    with filename.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    return AdvisorConfig.model_validate(payload)
