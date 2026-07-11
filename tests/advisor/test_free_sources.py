import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import pytest
import requests

from advisor.data_sources.contracts import MarketSourceError
from advisor.data_sources.free_sources import ConfiguredProviderRegistry, SinaDailyBarProvider


class RecordedResponse:
    def __init__(self, payload, *, content_type: str = "application/json", status_code: int = 200):
        self._payload = payload
        self.headers = {"Content-Type": content_type}
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class RecordedSession:
    def __init__(self, payload, **response_kwargs):
        self.response = RecordedResponse(payload, **response_kwargs)
        self.calls = []

    def get(self, url, *, params, timeout):
        self.calls.append((url, params, timeout))
        return self.response


def fixed_clock() -> datetime:
    return datetime.fromisoformat("2026-07-12T09:00:00+08:00")


def test_sina_provider_parses_and_bounds_daily_bars():
    session = RecordedSession(
        [
            {"day": "2023-07-11", "open": "9", "high": "10", "low": "8", "close": "9.5", "volume": "1"},
            {"day": "2023-07-12", "open": "10.00", "high": "10.80", "low": "9.80", "close": "10.50", "volume": "1000000"},
            {"day": "2026-07-10", "open": "11", "high": "12", "low": "10", "close": "11.5", "volume": "2000000"},
            {"day": "2026-07-13", "open": "12", "high": "13", "low": "11", "close": "12.5", "volume": "3"},
        ]
    )

    bars = SinaDailyBarProvider(session, fixed_clock).fetch_daily_bars(
        "600519", date(2023, 7, 12), date(2026, 7, 12)
    )

    assert [bar.trade_date for bar in bars] == [date(2023, 7, 12), date(2026, 7, 10)]
    assert bars[0].source == "sina_http"
    assert bars[0].as_of_date == bars[0].trade_date
    assert bars[0].amount == 0.0
    assert bars[0].fetched_at == "2026-07-12T09:00:00+08:00"
    normalized = {
        "amount": 0.0,
        "as_of_date": "2023-07-12",
        "close": 10.5,
        "code": "600519",
        "high": 10.8,
        "low": 9.8,
        "open": 10.0,
        "source": "sina_http",
        "trade_date": "2023-07-12",
        "volume": 1000000.0,
    }
    expected_hash = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert bars[0].content_hash == expected_hash
    url, params, timeout = session.calls[0]
    assert url == SinaDailyBarProvider.endpoint
    assert params == {"symbol": "sh600519", "scale": "240", "ma": "no", "datalen": "800"}
    assert timeout == 15


@pytest.mark.parametrize(
    ("code", "symbol"),
    [("000001", "sz000001"), ("300750", "sz300750"), ("688981", "sh688981"), ("830799", "bj830799")],
)
def test_sina_provider_uses_exchange_prefix(code: str, symbol: str):
    session = RecordedSession(
        [{"day": "2026-07-10", "open": "1", "high": "1", "low": "1", "close": "1", "volume": "0"}]
    )

    SinaDailyBarProvider(session, fixed_clock).fetch_daily_bars(code, date(2026, 7, 10), date(2026, 7, 10))

    assert session.calls[0][1]["symbol"] == symbol


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"day": "2026-07-10"},
        [{"day": "bad", "open": "1", "high": "1", "low": "1", "close": "1", "volume": "0"}],
        [{"day": "2026-07-10", "open": "nan", "high": "2", "low": "1", "close": "1", "volume": "0"}],
        [{"day": "2026-07-10", "open": "3", "high": "2", "low": "1", "close": "1", "volume": "0"}],
        [{"day": "2026-07-10", "open": "1", "high": "2", "low": "1", "close": "1", "volume": "-1"}],
    ],
)
def test_sina_provider_fails_closed_on_ambiguous_shape(payload):
    with pytest.raises(MarketSourceError):
        SinaDailyBarProvider(RecordedSession(payload), fixed_clock).fetch_daily_bars(
            "600519", date(2026, 7, 1), date(2026, 7, 12)
        )


def test_sina_provider_rejects_duplicate_or_descending_dates():
    row = {"day": "2026-07-10", "open": "1", "high": "1", "low": "1", "close": "1", "volume": "0"}

    with pytest.raises(MarketSourceError, match="dates"):
        SinaDailyBarProvider(RecordedSession([row, row]), fixed_clock).fetch_daily_bars(
            "600519", date(2026, 7, 1), date(2026, 7, 12)
        )


def test_registry_selects_sina_and_reports_unsupported_sources(tmp_path: Path):
    path = tmp_path / "data-sources.yaml"
    path.write_text(
        """
sources:
  mootdx:
    enabled: true
    rate_limit_per_second: 5
  sina:
    enabled: true
    rate_limit_per_second: 2
  eastmoney:
    enabled: false
    rate_limit_per_second: 1
""",
        encoding="utf-8",
    )

    registry = ConfiguredProviderRegistry.from_yaml(path)

    assert isinstance(registry.historical_provider, SinaDailyBarProvider)
    assert registry.rate_limit_per_second == 2
    assert registry.degraded_sources == ("mootdx",)
    assert "tushare" not in registry.historical_provider.endpoint.lower()
    assert "tushare" not in type(registry.historical_provider).__name__.lower()


def test_registry_rejects_tushare(tmp_path: Path):
    path = tmp_path / "data-sources.yaml"
    path.write_text(
        "sources:\n  tushare:\n    enabled: true\n    rate_limit_per_second: 1\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Tushare"):
        ConfiguredProviderRegistry.from_yaml(path)
