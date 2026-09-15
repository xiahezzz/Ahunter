import importlib.util
import tomllib
from pathlib import Path


def test_legacy_per_code_backfill_path_is_removed_in_favor_of_market_daily_service():
    assert importlib.util.find_spec("advisor.data_sources.backfill") is None

    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert "advisor-backfill" not in project["project"]["scripts"]
    assert project["project"]["scripts"]["advisor-market-daily"] == "advisor.market_daily.cli:main"
