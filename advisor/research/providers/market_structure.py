"""Bounded normalizers for public hot-money and market-structure rows."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from advisor.research.providers.normalization import (
    NormalizationResult,
    bounded_text,
    finite_number,
    first_value,
    parse_public_time,
    rows_from_payload,
    stable_key,
)


SCHEMA_VERSION = "market-structure-products@1"
_PRODUCT_LIMITS = {
    "hot_stocks": 50,
    "capital_flows": 100,
    "concepts": 100,
    "dragon_tiger": 100,
    "lockup_calendar": 100,
}


def normalize_structure_rows(
    product: str,
    source: list[dict[str, Any]] | dict[str, Any],
    *,
    code: str,
    as_of: datetime,
    source_locator: str,
    fetched_at: datetime,
) -> NormalizationResult:
    rows = rows_from_payload(source)
    if not rows:
        return NormalizationResult(
            {"status": "empty", "product": product, "code": code, "items": [], "schema_version": SCHEMA_VERSION, "fetched_at": fetched_at.isoformat()}
        )
    normalized: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        if product == "lockup_calendar":
            raw_plan_date = parse_public_time(first_value(row, "plan_date", "解禁日期", "date"))
            raw_announcement = parse_public_time(first_value(row, "announcement_at", "公告日期", "披露日期"))
            # A future plan is a valid observation outside this Snapshot, not
            # a source-format failure. It must not turn an otherwise empty
            # window into a warning.
            if raw_plan_date is not None and raw_plan_date > as_of and (raw_announcement is None or raw_announcement > as_of):
                continue
            if raw_announcement is not None and raw_announcement > as_of:
                continue
        value = _normalize_one(product, row, code=code, as_of=as_of, source_locator=source_locator)
        if value is None:
            skipped += 1
            continue
        normalized.append(value)
    normalized.sort(key=lambda item: _sort_key(product, item))
    deduplicated: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for item in normalized:
        key = stable_key(item.get("trade_date"), item.get("plan_date"), item.get("name"), item.get("concept"), item.get("holder"), item.get("reason"), item.get("rank"))
        if key in seen:
            existing = deduplicated[seen[key]]
            existing_sources = set(existing.get("source_locators", ()))
            existing_sources.add(source_locator)
            existing["source_locators"] = sorted(existing_sources)
            continue
        seen[key] = len(deduplicated)
        item["source_locators"] = [source_locator]
        deduplicated.append(item)
    limit = _PRODUCT_LIMITS.get(product, 100)
    deduplicated = deduplicated[:limit]
    quality_status = "passed" if normalized or not rows else "warning"
    message = None if not skipped else f"{skipped} source rows were outside the bounded schema"
    return NormalizationResult(
        {
            "status": "passed" if deduplicated else ("warning" if skipped else "empty"),
            "product": product,
            "code": code,
            "items": deduplicated,
            "skipped_rows": skipped,
            "schema_version": SCHEMA_VERSION,
            "fetched_at": fetched_at.isoformat(),
        },
        quality_status if not skipped or deduplicated else "warning",
        message,
    )


def _normalize_one(product: str, row: dict[str, Any], *, code: str, as_of: datetime, source_locator: str) -> dict[str, Any] | None:
    if product == "hot_stocks":
        rank = _nonnegative_int(first_value(row, "rank", "排名"))
        name = bounded_text(first_value(row, "name", "股票名称", "证券名称") or "", 160)
        if rank is None or not name:
            return None
        return {
            "code": code,
            "rank": rank,
            "name": name,
            "change_pct": finite_number(first_value(row, "change_pct", "涨跌幅")),
            "observed_date": _bounded_date(first_value(row, "observed_date", "trade_date", "日期"), as_of),
            "source": source_locator,
        }
    if product == "capital_flows":
        trade_date = _bounded_date(first_value(row, "trade_date", "日期", "交易日"), as_of)
        if trade_date is None:
            return None
        direction = _direction(first_value(row, "direction", "资金方向", "方向"))
        amount = finite_number(first_value(row, "amount", "净额", "资金净流入"))
        if amount is None:
            return None
        amount_unit = bounded_text(first_value(row, "amount_unit", "单位") or "CNY 10,000", 40)
        return {
            "code": code,
            "trade_date": trade_date,
            "direction": direction,
            "amount": amount,
            "amount_unit": amount_unit,
            **_amount_metadata(amount, amount_unit),
            "frequency": bounded_text(first_value(row, "frequency") or "daily", 20),
            "source": source_locator,
        }
    if product == "concepts":
        sample_date = _bounded_date(first_value(row, "sample_date", "trade_date", "日期"), as_of)
        concept = bounded_text(first_value(row, "concept", "概念", "概念名称") or "", 160)
        if sample_date is None or not concept:
            return None
        return {
            "code": code,
            "concept": concept,
            "industry": bounded_text(first_value(row, "industry", "行业") or "", 160),
            "classification_version": bounded_text(first_value(row, "classification_version", "分类版本") or "public-unknown", 80),
            "sample_date": sample_date,
            "source": source_locator,
        }
    if product == "dragon_tiger":
        trade_date = _bounded_date(first_value(row, "trade_date", "日期", "上榜日"), as_of)
        if trade_date is None:
            return None
        buy = finite_number(first_value(row, "buy_amount", "买入额"), allow_negative=False)
        sell = finite_number(first_value(row, "sell_amount", "卖出额"), allow_negative=False)
        net = finite_number(first_value(row, "net_amount", "净额"))
        if net is None and buy is not None and sell is not None:
            net = buy - sell
        if buy is None and sell is None and net is None:
            return None
        amount_unit = bounded_text(first_value(row, "amount_unit", "单位") or "CNY 10,000", 40)
        return {
            "code": code,
            "trade_date": trade_date,
            "reason": bounded_text(first_value(row, "reason", "上榜原因") or "unknown", 240),
            "seat_type": bounded_text(first_value(row, "seat_type", "席位类型") or "unknown", 80),
            "broker": bounded_text(first_value(row, "broker", "营业部") or "", 160),
            "buy_amount": buy,
            "sell_amount": sell,
            "net_amount": net,
            "amount_unit": amount_unit,
            "buy_amount_cny": _scaled_amount(buy, amount_unit),
            "sell_amount_cny": _scaled_amount(sell, amount_unit),
            "net_amount_cny": _scaled_amount(net, amount_unit),
            "source": source_locator,
        }
    if product == "lockup_calendar":
        raw_plan_date = parse_public_time(first_value(row, "plan_date", "解禁日期", "date"))
        announcement = parse_public_time(first_value(row, "announcement_at", "公告日期", "披露日期"))
        if raw_plan_date is None:
            return None
        if announcement is not None and announcement > as_of:
            return None
        if raw_plan_date > as_of and announcement is None:
            return None
        return {
            "code": code,
            "plan_date": raw_plan_date.date().isoformat(),
            "announcement_at": announcement.isoformat() if announcement else None,
            "shares": finite_number(first_value(row, "shares", "解禁数量"), allow_negative=False),
            "shares_unit": bounded_text(first_value(row, "shares_unit", "单位") or "share", 40),
            "ratio_pct": finite_number(first_value(row, "ratio_pct", "占总股本比例")),
            "source": source_locator,
        }
    return None


def _bounded_date(value: Any, as_of: datetime) -> str | None:
    parsed = parse_public_time(value)
    if parsed is None or parsed > as_of:
        return None
    return parsed.date().isoformat()


def _nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _direction(value: Any) -> str:
    text = bounded_text(value, 40).lower()
    if any(token in text for token in ("in", "流入", "buy", "净买")):
        return "inflow"
    if any(token in text for token in ("out", "流出", "sell", "净卖")):
        return "outflow"
    return "neutral"


def _amount_metadata(amount: float, unit: str) -> dict[str, Any]:
    return {
        "reported_amount": amount,
        "amount_scale_to_cny": _amount_scale(unit),
        "amount_cny": _scaled_amount(amount, unit),
    }


def _scaled_amount(amount: float | None, unit: str) -> float | None:
    scale = _amount_scale(unit)
    return None if amount is None or scale is None else amount * scale


def _amount_scale(unit: str) -> float | None:
    text = unit.lower().replace(" ", "").replace(",", "")
    if any(token in text for token in ("trillion", "万亿")):
        return 1_000_000_000_000.0
    if any(token in text for token in ("billion", "十亿元", "亿元")):
        return 100_000_000.0
    if any(token in text for token in ("million", "百万元", "百万")):
        return 1_000_000.0
    if any(token in text for token in ("thousand", "千元")):
        return 1_000.0
    if any(token in text for token in ("10000", "万元")):
        return 10_000.0
    if text in {"cny", "rmb", "元", "人民币", "cny元"}:
        return 1.0
    return None


def _sort_key(product: str, item: dict[str, Any]) -> tuple[Any, ...]:
    if product == "lockup_calendar":
        return (item.get("plan_date") or "", item.get("announcement_at") or "")
    return (item.get("trade_date") or item.get("sample_date") or item.get("observed_date") or "", item.get("name") or item.get("concept") or "")
