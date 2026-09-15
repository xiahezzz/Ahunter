"""Normalization for public market and technical observations."""

from __future__ import annotations

from datetime import date, datetime
import math
from typing import Any

from advisor.research.market_time import a_share_date
from advisor.research.providers.normalization import finite_number, parse_public_time


SCHEMA_VERSION = "market-products@1"


def normalize_daily_rows(
    rows: list[dict[str, Any]],
    *,
    code: str,
    as_of: datetime,
    source: str,
    fetched_at: datetime,
) -> list[dict[str, Any]]:
    """Normalize and validate unadjusted daily OHLCV rows.

    Rows are returned in ascending trading-date order. A duplicate, malformed,
    future, or logically impossible bar is rejected rather than silently
    repaired.
    """
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError("daily bar row is not an object")
        raw_date = raw.get("trade_date", raw.get("day", raw.get("date")))
        trade_time = parse_public_time(raw_date)
        if trade_time is None:
            raise ValueError("daily bar trade_date is invalid")
        trade_date = a_share_date(trade_time)
        date_key = trade_date.isoformat()
        if trade_date > a_share_date(as_of):
            raise ValueError("daily bar is after as_of")
        if date_key in seen:
            raise ValueError("daily bars are not unique")
        seen.add(date_key)
        values: dict[str, float] = {}
        for field in ("open", "high", "low", "close", "volume", "amount"):
            parsed = finite_number(raw.get(field), allow_negative=False)
            if parsed is None:
                if field == "amount" and raw.get(field) in (None, "", "--", "-"):
                    parsed = 0.0
                else:
                    raise ValueError(f"daily bar {field} is invalid")
            values[field] = parsed
        if any(values[field] <= 0 for field in ("open", "high", "low", "close")):
            raise ValueError("daily bar OHLC values must be positive")
        if values["high"] < max(values["open"], values["close"]) or values["low"] > min(values["open"], values["close"]):
            raise ValueError("daily bar OHLC logic is invalid")
        normalized.append(
            {
                "code": code,
                "trade_date": date_key,
                **values,
                "adjustment": str(raw.get("adjustment") or "unadjusted"),
                "price_unit": "CNY/share",
                "volume_unit": str(raw.get("volume_unit") or "share"),
                "amount_unit": str(raw.get("amount_unit") or "CNY"),
                "exchange": str(raw.get("exchange") or _exchange_for(code)),
                "source": source,
                "source_time": date_key,
                "fetched_at": fetched_at.isoformat(),
            }
        )
    normalized.sort(key=lambda item: item["trade_date"])
    return normalized


def normalize_quote(
    values: dict[str, Any],
    *,
    code: str,
    source: str,
    fetched_at: datetime,
    source_time: datetime | None = None,
) -> dict[str, Any]:
    """Normalize a quote row while preserving legitimate zero values."""
    payload = {
        "status": "passed",
        "code": code,
        "name": str(values.get("name") or ""),
        "price": _number(values.get("price")),
        "last_close": _number(values.get("last_close")),
        "open": _number(values.get("open")),
        "change_pct": _number(values.get("change_pct")),
        "high": _number(values.get("high")),
        "low": _number(values.get("low")),
        "turnover_pct": _number(values.get("turnover_pct")),
        "pe_ttm": _number(values.get("pe_ttm")),
        "market_cap_yi": _number(values.get("market_cap_yi")),
        "float_market_cap_yi": _number(values.get("float_market_cap_yi")),
        "pb": _number(values.get("pb")),
        "limit_up": _number(values.get("limit_up")),
        "limit_down": _number(values.get("limit_down")),
        "currency": "CNY",
        "market_cap_unit": "CNY 100 million",
        "source": source,
        "source_time": (source_time or fetched_at).isoformat(),
        "fetched_at": fetched_at.isoformat(),
    }
    return payload


def derive_indicators(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [float(row["close"]) for row in rows if _finite(row.get("close"))]
    volumes = [float(row["volume"]) for row in rows if _finite(row.get("volume"))]

    def average(window: int, values: list[float]) -> float | None:
        if len(values) < window:
            return None
        return sum(values[-window:]) / window

    last = closes[-1] if closes else None
    previous = closes[-2] if len(closes) > 1 else None
    return {
        "status": "passed" if closes else "empty",
        "latest_close": last,
        "change_pct": ((last / previous) - 1) * 100 if last is not None and previous else None,
        "sma_5": average(5, closes),
        "sma_20": average(20, closes),
        "sma_60": average(60, closes),
        "volume_5": average(5, volumes),
        "volume_20": average(20, volumes),
        "observations": len(closes),
        "algorithm_version": "market-indicators@1",
    }


def _exchange_for(code: str) -> str:
    if code.startswith("6"):
        return "SSE"
    if code.startswith(("0", "3")):
        return "SZSE"
    return "BSE"


def _number(value: Any) -> float | None:
    if value in (None, "", "--", "-"):
        return None
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False
