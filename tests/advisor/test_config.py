from pathlib import Path

import pytest
import yaml

from advisor.config import (
    AdvisorConfig,
    load_advisor_config,
    resolve_chart_dir,
    resolve_profile_dir,
    resolve_state_db,
)


def configured_for(database: str) -> AdvisorConfig:
    return AdvisorConfig.model_validate(
        {
            "market": {"primary": "A股"},
            "schedule": {"premarket_time": "08:30", "review_time": "22:30"},
            "storage": {"database": database},
            "data_sources": {"allow_tushare": False, "free_sources": []},
        }
    )


def test_load_default_advisor_config_uses_one_database():
    config = load_advisor_config()

    assert config.market.primary == "A股"
    assert config.schedule.premarket_time == "08:30"
    assert config.schedule.review_time == "22:30"
    assert config.data_sources.allow_tushare is False
    assert config.data_sources.free_sources == []
    assert config.storage.database == "data/advisor/advisor.sqlite"
    assert not hasattr(config.storage, "market_db")
    assert not hasattr(config.storage, "advisor_db")


def test_research_engine_config_selects_catalog_teams_and_policy(tmp_path):
    payload = configured_for("data/advisor/advisor.sqlite").model_dump()
    payload["research"].update(default_teams=["a_share_core@2", "normal@2"], execution_policy="codex@3")
    filename = tmp_path / "advisor.yaml"
    filename.write_text(yaml.safe_dump(payload))
    config = load_advisor_config(filename)

    assert config.research.catalog_dir == "config/research"
    assert config.research.artifact_dir == "data/advisor/research-artifacts"
    assert config.research.default_teams == ["a_share_core@2", "normal@2"]
    assert config.research.execution_policy == "codex@3"


def test_unknown_runtime_configuration_is_rejected():
    payload = configured_for("data/advisor/advisor.sqlite").model_dump()
    payload["legacy_runtime"] = {"path": "/tmp/outside"}

    with pytest.raises(ValueError, match="legacy_runtime"):
        AdvisorConfig.model_validate(payload)


def test_legacy_external_research_configuration_is_rejected():
    payload = configured_for("data/advisor/advisor.sqlite").model_dump()
    payload["data_sources"]["trading_agents"] = {"repository": "/outside"}

    with pytest.raises(ValueError, match="trading_agents"):
        AdvisorConfig.model_validate(payload)


def test_storage_uses_one_operational_database(tmp_path: Path):
    config = configured_for("data/advisor/advisor.sqlite")

    assert resolve_state_db(config, tmp_path) == tmp_path / "data/advisor/advisor.sqlite"


def test_storage_rejects_legacy_split_database_paths():
    payload = configured_for("data/advisor/advisor.sqlite").model_dump()
    payload["storage"]["market_db"] = "data/advisor/market.sqlite"

    with pytest.raises(ValueError, match="market_db"):
        AdvisorConfig.model_validate(payload)


@pytest.mark.parametrize("database", ["../outside.sqlite", "/tmp/outside.sqlite"])
def test_operational_database_cannot_escape_repository(tmp_path: Path, database: str):
    config = configured_for(database)

    with pytest.raises(ValueError, match="database"):
        resolve_state_db(config, tmp_path)


def test_chart_dir_cannot_escape_repository(tmp_path: Path):
    payload = configured_for("data/advisor/advisor.sqlite").model_dump()
    payload["storage"]["chart_dir"] = "../outside"
    config = AdvisorConfig.model_validate(payload)

    with pytest.raises(ValueError, match="chart_dir"):
        resolve_chart_dir(config, tmp_path)


def test_profile_dir_cannot_be_absolute_outside_repository(tmp_path: Path):
    payload = configured_for("data/advisor/advisor.sqlite").model_dump()
    payload["storage"]["profile_dir"] = str(tmp_path.parent / "outside-profiles")
    config = AdvisorConfig.model_validate(payload)

    with pytest.raises(ValueError, match="profile_dir"):
        resolve_profile_dir(config, tmp_path)


def test_storage_directory_resolvers_return_root_contained_paths(tmp_path: Path):
    config = configured_for("data/advisor/advisor.sqlite")

    assert resolve_chart_dir(config, tmp_path) == tmp_path / "data/advisor/charts"
    assert resolve_profile_dir(config, tmp_path) == tmp_path / "data/advisor/profiles"


def test_rejects_tushare_enabled(tmp_path: Path):
    filename = tmp_path / "advisor.yaml"
    filename.write_text(
        """
market:
  primary: A股
schedule:
  premarket_time: "08:30"
  review_time: "22:30"
storage:
  database: data/advisor/advisor.sqlite
data_sources:
  allow_tushare: true
  free_sources: []
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Tushare"):
        load_advisor_config(filename)
