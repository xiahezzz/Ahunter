from pathlib import Path

import pytest

from advisor.config import load_advisor_config


def test_load_default_advisor_config():
    config = load_advisor_config()
    assert config.market.primary == "A股"
    assert config.schedule.premarket_time == "08:30"
    assert config.schedule.review_time == "22:30"
    assert config.data_sources.allow_tushare is False
    assert "mootdx" in config.data_sources.free_sources
    assert config.storage.market_db.endswith("data/advisor/market.sqlite")


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
  market_db: data/advisor/market.sqlite
  advisor_db: data/advisor/advisor.sqlite
data_sources:
  allow_tushare: true
  free_sources: [mootdx]
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Tushare"):
        load_advisor_config(filename)
