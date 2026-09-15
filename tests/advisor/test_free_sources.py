import importlib.util
from pathlib import Path

import yaml


def test_legacy_free_market_provider_module_is_removed():
    assert importlib.util.find_spec("advisor.data_sources.free_sources") is None


def test_runtime_market_data_configuration_is_sina_only():
    root = Path(__file__).resolve().parents[2]
    payload = yaml.safe_load((root / "config" / "data-sources.yaml").read_text(encoding="utf-8"))

    assert payload == {
        "market_daily": {
            "source": "sina",
            "daily_refresh_time": "21:00",
        }
    }
