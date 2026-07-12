from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable

import requests
import yaml

from advisor.data_sources.contracts import DailyBar, MarketSourceError


_CODE_RE = re.compile(r"\d{6}\Z")
_PREFIXES = {"0": "sz", "3": "sz", "4": "bj", "6": "sh", "8": "bj"}


class SinaDailyBarProvider:
    source = "sina_http"
    endpoint = (
        "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData"
    )

    def __init__(
        self,
        session: requests.Session | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session if session is not None else requests.Session()
        self._clock = clock or (lambda: datetime.now().astimezone())
        self.rate_limit_per_second = 1.0

    def fetch_daily_bars(self, code: str, start: date, end: date) -> list[DailyBar]:
        prefix = _symbol_prefix(code)
        if not isinstance(start, date) or not isinstance(end, date) or start > end:
            raise MarketSourceError("invalid requested date range")
        params = {
            "symbol": f"{prefix}{code}",
            "scale": "240",
            "ma": "no",
            "datalen": "800",
        }
        try:
            response = self._session.get(self.endpoint, params=params, timeout=15)
            response.raise_for_status()
        except requests.RequestException as error:
            raise MarketSourceError("Sina request failed") from error
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"application/json", "text/json", "text/plain"}:
            raise MarketSourceError("Sina returned an ambiguous content type")
        try:
            payload = json.loads(response.text)
        except (TypeError, json.JSONDecodeError) as error:
            raise MarketSourceError("Sina returned invalid JSON") from error
        if not isinstance(payload, list) or not payload or len(payload) > 800:
            raise MarketSourceError("Sina returned an invalid row collection")

        fetched_at = self._clock().isoformat()
        parsed = [_parse_row(code, row, fetched_at) for row in payload]
        dates = [bar.trade_date for bar in parsed]
        if dates != sorted(dates) or len(dates) != len(set(dates)):
            raise MarketSourceError("Sina row dates must be unique and ascending")
        bounded = [bar for bar in parsed if start <= bar.trade_date <= end]
        if not bounded:
            raise MarketSourceError("Sina returned no rows in the requested interval")
        return bounded


@dataclass(frozen=True)
class ConfiguredProviderRegistry:
    historical_provider: SinaDailyBarProvider
    rate_limit_per_second: float
    degraded_sources: tuple[str, ...]

    @classmethod
    def from_yaml(cls, path: Path) -> ConfiguredProviderRegistry:
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise ValueError("invalid data source configuration") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("sources"), dict):
            raise ValueError("data source configuration requires a sources mapping")

        enabled: list[tuple[str, float]] = []
        for raw_name, settings in payload["sources"].items():
            name = str(raw_name).lower()
            if name == "tushare":
                raise ValueError("Tushare is not allowed for required advisor data paths")
            if not isinstance(settings, dict) or not isinstance(settings.get("enabled"), bool):
                raise ValueError(f"invalid source configuration for {name}")
            rate = settings.get("rate_limit_per_second")
            if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate <= 0:
                raise ValueError(f"invalid rate limit for {name}")
            if settings["enabled"]:
                enabled.append((name, float(rate)))

        sina = next(((name, rate) for name, rate in enabled if name == "sina"), None)
        if sina is None:
            raise MarketSourceError("no supported historical free source is enabled")
        provider = SinaDailyBarProvider()
        provider.rate_limit_per_second = sina[1]
        degraded = tuple(name for name, _ in enabled if name != "sina")
        return cls(provider, sina[1], degraded)


def _symbol_prefix(code: str) -> str:
    if not isinstance(code, str) or not _CODE_RE.fullmatch(code) or code[0] not in _PREFIXES:
        raise MarketSourceError("code must be a supported six-digit A-share code")
    return _PREFIXES[code[0]]


def _parse_row(code: str, row: object, fetched_at: str) -> DailyBar:
    if not isinstance(row, dict):
        raise MarketSourceError("Sina row must be an object")
    try:
        trade_date = date.fromisoformat(row["day"])
        open_price = float(row["open"])
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        volume = float(row["volume"])
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise MarketSourceError("Sina row has invalid fields") from error
    prices = (open_price, high, low, close)
    if not all(math.isfinite(value) and value > 0 for value in prices):
        raise MarketSourceError("Sina row has invalid OHLC values")
    if not low <= open_price <= high or not low <= close <= high:
        raise MarketSourceError("Sina row violates OHLC bounds")
    if not math.isfinite(volume) or volume < 0:
        raise MarketSourceError("Sina row has invalid volume")
    normalized = {
        "amount": 0.0,
        "as_of_date": trade_date.isoformat(),
        "close": close,
        "code": code,
        "high": high,
        "low": low,
        "open": open_price,
        "source": SinaDailyBarProvider.source,
        "trade_date": trade_date.isoformat(),
        "volume": volume,
    }
    content_hash = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return DailyBar(
        code=code,
        trade_date=trade_date,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        amount=0.0,
        source=SinaDailyBarProvider.source,
        fetched_at=fetched_at,
        as_of_date=trade_date,
        content_hash=content_hash,
    )
