"""Structured public fundamental-product normalizers.

These functions accept already-fetched fixture payloads. They never infer a
missing value and never turn an HTML excerpt into an Agent-facing fact.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from advisor.research.providers.normalization import (
    NormalizationResult,
    bounded_text,
    finite_number,
    first_value,
    iso_or_none,
    parse_html_table,
    parse_public_time,
    rows_from_payload,
)


SCHEMA_VERSION = "fundamental-products@1"


def normalize_financial_statements(
    source: str | list[dict[str, Any]] | dict[str, Any],
    *,
    code: str,
    as_of: datetime,
    source_locator: str,
    fetched_at: datetime,
) -> NormalizationResult:
    rows = _source_rows(source)
    records: list[dict[str, Any]] = []
    uncovered: set[str] = set()
    for row in rows:
        report_period = bounded_text(first_value(row, "report_period", "report_date", "period", "报告期"), 32)
        disclosed = parse_public_time(first_value(row, "disclosed_at", "disclosure_at", "公告日期", "披露日期"))
        if not report_period or disclosed is None:
            uncovered.add("report_period_or_disclosed_at")
            continue
        if disclosed > as_of:
            raise ValueError("financial disclosure is after as_of")
        unit = bounded_text(first_value(row, "unit", "单位") or "CNY", 40)
        currency = bounded_text(first_value(row, "currency", "币种") or "CNY", 16)
        values: dict[str, dict[str, Any]] = {}
        for field, aliases in {
            "revenue": ("revenue", "营业收入"),
            "net_profit": ("net_profit", "归母净利润", "净利润"),
            "operating_cash_flow": ("operating_cash_flow", "经营现金流", "经营活动现金流"),
            "total_assets": ("total_assets", "总资产"),
            "total_liabilities": ("total_liabilities", "总负债"),
        }.items():
            raw = first_value(row, *aliases)
            if raw in (None, "", "--", "-"):
                uncovered.add(field)
                continue
            if not _is_cny(currency):
                uncovered.add(f"{field}:unsupported_currency")
                continue
            number = finite_number(raw)
            if number is None:
                uncovered.add(field)
                continue
            scale = _unit_scale(unit)
            values[field] = {
                "value": number * scale,
                "reported_value": number,
                "unit": "CNY",
                "reported_unit": unit,
                "scale_to_cny": scale,
                "currency": currency,
                "report_period": report_period,
                "disclosed_at": disclosed.isoformat(),
                "source": source_locator,
            }
        records.append(
            {
                "code": code,
                "report_period": report_period,
                "period_type": bounded_text(first_value(row, "period_type", "frequency", "频率") or "unknown", 24),
                "disclosed_at": disclosed.isoformat(),
                "unit": unit,
                "currency": currency,
                "values": values,
                "source": source_locator,
            }
        )
    records.sort(key=lambda item: (item["report_period"], item["disclosed_at"]))
    status = "passed" if records else "empty"
    return NormalizationResult(
        {
            "status": status,
            "code": code,
            "items": records,
            "uncovered_fields": sorted(uncovered),
            "schema_version": SCHEMA_VERSION,
            "fetched_at": fetched_at.isoformat(),
        },
        "passed" if records or not rows else "warning",
        None if records or not rows else "financial rows were present but not parseable",
    )


def normalize_earnings_forecast(
    source: str | list[dict[str, Any]] | dict[str, Any],
    *,
    code: str,
    as_of: datetime,
    source_locator: str,
    fetched_at: datetime,
) -> NormalizationResult:
    rows = _source_rows(source)
    records: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        published = parse_public_time(first_value(row, "published_at", "forecast_published_at", "公告日期", "披露日期"))
        forecast_year = first_value(row, "forecast_year", "year", "预测年度")
        if published is None or forecast_year in (None, ""):
            skipped += 1
            continue
        if published > as_of:
            continue
        institution_count = finite_number(first_value(row, "institution_count", "机构数", "机构数量"), allow_negative=False)
        record = {
            "code": code,
            "forecast_year": int(forecast_year) if str(forecast_year).isdigit() else bounded_text(forecast_year, 16),
            "published_at": published.isoformat(),
            "eps": finite_number(first_value(row, "eps", "预测每股收益")),
            "revenue": finite_number(first_value(row, "revenue", "预测营业收入")),
            "institution_count": int(institution_count) if institution_count is not None else None,
            "unit": bounded_text(first_value(row, "unit", "单位") or "CNY", 40),
            "source": source_locator,
        }
        records.append(record)
    records.sort(key=lambda item: (str(item["forecast_year"]), item["published_at"]))
    return NormalizationResult(
        {
            "status": "passed" if records else "empty",
            "code": code,
            "items": records,
            "skipped_rows": skipped,
            "schema_version": SCHEMA_VERSION,
            "fetched_at": fetched_at.isoformat(),
        },
        "passed" if records or not rows else "warning",
        None if records or not rows else "forecast rows were present but not parseable",
    )


def normalize_industry_context(
    source: str | list[dict[str, Any]] | dict[str, Any],
    *,
    code: str,
    as_of: datetime,
    source_locator: str,
    fetched_at: datetime,
) -> NormalizationResult:
    rows = _source_rows(source)
    records: list[dict[str, Any]] = []
    for row in rows:
        sample_date = parse_public_time(first_value(row, "sample_date", "as_of", "统计日期", "日期"))
        if sample_date is None or sample_date > as_of:
            continue
        records.append(
            {
                "code": code,
                "industry": bounded_text(first_value(row, "industry", "行业", "industry_name") or "", 120),
                "classification_version": bounded_text(first_value(row, "classification_version", "分类版本") or "public-unknown", 80),
                "sample_date": sample_date.date().isoformat(),
                "target_rank": _integer(first_value(row, "target_rank", "rank", "排名")),
                "sample_size": _integer(first_value(row, "sample_size", "count", "样本数")),
                "metrics": {
                    key: finite_number(value)
                    for key, value in {
                        "pe_ttm": first_value(row, "pe_ttm", "行业市盈率"),
                        "pb": first_value(row, "pb", "行业市净率"),
                        "change_pct": first_value(row, "change_pct", "涨跌幅"),
                    }.items()
                    if finite_number(value) is not None
                },
                "source": source_locator,
            }
        )
    records.sort(key=lambda item: (item["sample_date"], item["industry"], item["target_rank"] or 0))
    return NormalizationResult(
        {
            "status": "passed" if records else "empty",
            "code": code,
            "items": records,
            "schema_version": SCHEMA_VERSION,
            "fetched_at": fetched_at.isoformat(),
        }
    )


def normalize_insider_activity(
    source: str | list[dict[str, Any]] | dict[str, Any],
    *,
    code: str,
    as_of: datetime,
    source_locator: str,
    fetched_at: datetime,
) -> NormalizationResult:
    rows = _source_rows(source)
    records: list[dict[str, Any]] = []
    for row in rows:
        disclosed = parse_public_time(first_value(row, "disclosed_at", "announcement_at", "公告日期", "披露日期"))
        event_date = parse_public_time(first_value(row, "event_date", "trade_date", "变动日期", "交易日期"))
        if disclosed is None or disclosed > as_of or (event_date is not None and event_date > as_of):
            continue
        records.append(
            {
                "code": code,
                "holder": bounded_text(first_value(row, "holder", "股东", "股东名称") or "", 160),
                "direction": _direction(first_value(row, "direction", "变动方向", "操作")),
                "shares": finite_number(first_value(row, "shares", "变动数量", "持股变动"), allow_negative=False),
                "shares_unit": bounded_text(first_value(row, "shares_unit", "单位") or "share", 40),
                "event_date": event_date.date().isoformat() if event_date else None,
                "disclosed_at": disclosed.isoformat(),
                "source": source_locator,
            }
        )
    records.sort(key=lambda item: (item["disclosed_at"], item["holder"], item["direction"]))
    return NormalizationResult(
        {
            "status": "passed" if records else "empty",
            "code": code,
            "items": records,
            "schema_version": SCHEMA_VERSION,
            "fetched_at": fetched_at.isoformat(),
        }
    )


def _source_rows(source: str | list[dict[str, Any]] | dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(source, str):
        return parse_html_table(source)
    return rows_from_payload(source)


def _integer(value: Any) -> int | None:
    try:
        number = int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _direction(value: Any) -> str:
    text = bounded_text(value, 40).lower()
    if any(token in text for token in ("buy", "增持", "买入", "increase")):
        return "buy"
    if any(token in text for token in ("sell", "减持", "卖出", "decrease")):
        return "sell"
    return "unknown"


def _unit_scale(unit: str) -> float:
    text = unit.lower().replace(" ", "")
    if any(token in text for token in ("trillion", "万亿")):
        return 1_000_000_000_000.0
    if any(token in text for token in ("billion", "十亿元", "亿元")):
        return 100_000_000.0
    if any(token in text for token in ("million", "百万元", "百万")):
        return 1_000_000.0
    if any(token in text for token in ("thousand", "千元")):
        return 1_000.0
    if any(token in text for token in ("ten-thousand", "tenthousand", "万元")):
        return 10_000.0
    return 1.0


def _is_cny(currency: str) -> bool:
    return currency.strip().lower() in {"cny", "rmb", "人民币", "元", "人民币元"}
