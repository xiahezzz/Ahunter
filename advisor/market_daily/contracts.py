"""Immutable, normalized facts used by Market Daily ingestion."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo


_CODE_RE = re.compile(r"[036]\d{5}\Z")
_EXCHANGE_BY_PREFIX = {"6": "SH", "0": "SZ", "3": "SZ"}
_SECURITY_STATUSES = frozenset({"active", "delisted", "suspended"})
_HASH_DOMAIN = b"a-hunter:market-daily:v1\0"
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class MarketContractError(ValueError):
    """A Market Daily fact violates the repository-owned data contract."""


def _canonical_hash(kind: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        {"kind": kind, **payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(_HASH_DOMAIN + encoded).hexdigest()


def _require_code(code: object) -> str:
    if not isinstance(code, str) or not _CODE_RE.fullmatch(code):
        raise MarketContractError("code must be a six-digit Shanghai or Shenzhen code")
    return code


def exchange_for_code(code: str) -> str:
    return _EXCHANGE_BY_PREFIX[_require_code(code)[0]]


def _require_date(value: object, field_name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise MarketContractError(f"{field_name} must be a date")
    return value


def _require_time(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MarketContractError(f"{field_name} must be timezone-aware")
    return value


def _market_date(value: datetime) -> date:
    return value.astimezone(_SHANGHAI).date()


def _require_text(value: object, field_name: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise MarketContractError(f"{field_name} must be non-empty text")
    return value.strip()


def _require_number(value: object, field_name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise MarketContractError(f"{field_name} must be finite")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise MarketContractError(f"{field_name} must be finite") from error
    if not math.isfinite(number) or (positive and number <= 0):
        raise MarketContractError(f"{field_name} must be finite{' and positive' if positive else ''}")
    return number


def _require_volume(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MarketContractError("volume must be a non-negative integer number of shares")
    return value


@dataclass(frozen=True)
class MarketSecurity:
    code: str
    name: str
    exchange: str
    list_date: date
    delist_date: date | None
    status: str
    is_st: bool
    source: str
    source_at: datetime
    fetched_at: datetime
    security_type: str = "a_share"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        code = _require_code(self.code)
        if code.startswith("689"):
            raise MarketContractError("CDR codes are not eligible A-share securities")
        exchange = _require_text(self.exchange, "exchange", maximum=2).upper()
        if exchange not in {"SH", "SZ"} or exchange != exchange_for_code(code):
            raise MarketContractError("exchange must match the Shanghai or Shenzhen code")
        list_date = _require_date(self.list_date, "list_date")
        delist_date = self.delist_date
        if delist_date is not None:
            _require_date(delist_date, "delist_date")
            if delist_date < list_date:
                raise MarketContractError("delist_date cannot precede list_date")
        status = _require_text(self.status, "status", maximum=16).lower()
        if status not in _SECURITY_STATUSES:
            raise MarketContractError("invalid security status")
        if status == "delisted" and delist_date is None:
            raise MarketContractError("delisted security requires delist_date")
        if not isinstance(self.is_st, bool):
            raise MarketContractError("is_st must be boolean")
        security_type = _require_text(self.security_type, "security_type", maximum=32).lower()
        if security_type != "a_share":
            raise MarketContractError("security_type must be a_share")
        source_at = _require_time(self.source_at, "source_at")
        fetched_at = _require_time(self.fetched_at, "fetched_at")
        payload = {
            "code": code,
            "delist_date": delist_date.isoformat() if delist_date else None,
            "exchange": exchange,
            "is_st": self.is_st,
            "list_date": list_date.isoformat(),
            "name": _require_text(self.name, "name", maximum=256),
            "security_type": security_type,
            "source": _require_text(self.source, "source", maximum=128),
            "source_at": source_at.isoformat(),
            "status": status,
        }
        for field_name, value in (
            ("code", code),
            ("exchange", exchange),
            ("name", payload["name"]),
            ("status", status),
            ("security_type", security_type),
        ):
            object.__setattr__(self, field_name, value)
        object.__setattr__(self, "content_hash", _canonical_hash("security", payload))


@dataclass(frozen=True)
class CanonicalDailyBar:
    code: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float | None
    source: str
    source_at: datetime
    fetched_at: datetime
    as_of_date: date | None = None
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        code = _require_code(self.code)
        trade_date = _require_date(self.trade_date, "trade_date")
        source_at = _require_time(self.source_at, "source_at")
        fetched_at = _require_time(self.fetched_at, "fetched_at")
        source_date = _market_date(source_at)
        if trade_date > source_date:
            raise MarketContractError("trade_date cannot be after source_at")
        as_of_date = trade_date if self.as_of_date is None else _require_date(self.as_of_date, "as_of_date")
        if as_of_date < trade_date or as_of_date > source_date:
            raise MarketContractError("as_of_date must be within the observed date boundary")
        open_price = _require_number(self.open, "open", positive=True)
        high = _require_number(self.high, "high", positive=True)
        low = _require_number(self.low, "low", positive=True)
        close = _require_number(self.close, "close", positive=True)
        if not low <= open_price <= high or not low <= close <= high:
            raise MarketContractError("OHLC values violate daily bar bounds")
        volume = _require_volume(self.volume)
        amount = None if self.amount is None else _require_number(self.amount, "amount")
        if amount is not None and amount < 0:
            raise MarketContractError("amount must be non-negative when reported")
        source = _require_text(self.source, "source", maximum=128)
        payload = {
            "amount": amount,
            "as_of_date": as_of_date.isoformat(),
            "close": close,
            "code": code,
            "high": high,
            "low": low,
            "open": open_price,
            "source": source,
            "source_at": source_at.isoformat(),
            "trade_date": trade_date.isoformat(),
            "volume": volume,
        }
        for field_name, value in (
            ("code", code),
            ("trade_date", trade_date),
            ("open", open_price),
            ("high", high),
            ("low", low),
            ("close", close),
            ("volume", volume),
            ("amount", amount),
            ("source", source),
            ("as_of_date", as_of_date),
        ):
            object.__setattr__(self, field_name, value)
        object.__setattr__(self, "content_hash", _canonical_hash("daily_bar", payload))


@dataclass(frozen=True)
class AdjustmentFactor:
    code: str
    trade_date: date
    factor: float
    source: str
    source_at: datetime
    fetched_at: datetime
    algorithm_version: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        code = _require_code(self.code)
        trade_date = _require_date(self.trade_date, "trade_date")
        factor = _require_number(self.factor, "factor", positive=True)
        source_at = _require_time(self.source_at, "source_at")
        _require_time(self.fetched_at, "fetched_at")
        if trade_date > _market_date(source_at):
            raise MarketContractError("factor trade_date cannot be after source_at")
        source = _require_text(self.source, "source", maximum=128)
        algorithm_version = _require_text(self.algorithm_version, "algorithm_version", maximum=128)
        payload = {
            "algorithm_version": algorithm_version,
            "code": code,
            "factor": factor,
            "source": source,
            "source_at": source_at.isoformat(),
            "trade_date": trade_date.isoformat(),
        }
        for field_name, value in (
            ("code", code),
            ("trade_date", trade_date),
            ("factor", factor),
            ("source", source),
            ("algorithm_version", algorithm_version),
        ):
            object.__setattr__(self, field_name, value)
        object.__setattr__(self, "content_hash", _canonical_hash("adjustment_factor", payload))


@dataclass(frozen=True)
class ObservedTradingSession:
    trade_date: date
    primary_source: str
    fallback_source: str
    primary_observed_at: datetime
    fallback_observed_at: datetime
    fetched_at: datetime
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        trade_date = _require_date(self.trade_date, "trade_date")
        primary_source = _require_text(self.primary_source, "primary_source", maximum=128)
        fallback_source = _require_text(self.fallback_source, "fallback_source", maximum=128)
        if primary_source == fallback_source:
            raise MarketContractError("trading session requires two distinct sources")
        primary_observed_at = _require_time(self.primary_observed_at, "primary_observed_at")
        fallback_observed_at = _require_time(self.fallback_observed_at, "fallback_observed_at")
        _require_time(self.fetched_at, "fetched_at")
        if trade_date > _market_date(primary_observed_at) or trade_date > _market_date(fallback_observed_at):
            raise MarketContractError("session cannot be after its observations")
        payload = {
            "fallback_observed_at": fallback_observed_at.isoformat(),
            "fallback_source": fallback_source,
            "primary_observed_at": primary_observed_at.isoformat(),
            "primary_source": primary_source,
            "trade_date": trade_date.isoformat(),
        }
        for field_name, value in (
            ("trade_date", trade_date),
            ("primary_source", primary_source),
            ("fallback_source", fallback_source),
        ):
            object.__setattr__(self, field_name, value)
        object.__setattr__(self, "content_hash", _canonical_hash("trading_session", payload))


@dataclass(frozen=True)
class SessionObservationReceipt:
    """An auditable heartbeat proving that both session sources were checked."""

    observed_at: datetime
    primary_source: str
    fallback_source: str
    latest_session: date | None
    session_set_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        observed_at = _require_time(self.observed_at, "observed_at")
        primary_source = _require_text(self.primary_source, "primary_source", maximum=128)
        fallback_source = _require_text(self.fallback_source, "fallback_source", maximum=128)
        if primary_source == fallback_source:
            raise MarketContractError("session observation requires two distinct sources")
        latest_session = self.latest_session
        if latest_session is not None:
            _require_date(latest_session, "latest_session")
            if latest_session > _market_date(observed_at):
                raise MarketContractError("latest_session cannot be after observed_at")
        session_set_hash = _require_text(self.session_set_hash, "session_set_hash", maximum=64)
        if not re.fullmatch(r"[0-9a-f]{64}", session_set_hash):
            raise MarketContractError("session_set_hash must be a SHA-256 hex digest")
        payload = {
            "fallback_source": fallback_source,
            "latest_session": latest_session.isoformat() if latest_session else None,
            "observed_at": observed_at.isoformat(),
            "primary_source": primary_source,
            "session_set_hash": session_set_hash,
        }
        for field_name, value in (
            ("observed_at", observed_at),
            ("primary_source", primary_source),
            ("fallback_source", fallback_source),
            ("latest_session", latest_session),
            ("session_set_hash", session_set_hash),
        ):
            object.__setattr__(self, field_name, value)
        object.__setattr__(self, "content_hash", _canonical_hash("session_observation", payload))


@dataclass(frozen=True)
class MarketAbsence:
    code: str
    trade_date: date
    reason: str
    source: str
    source_at: datetime
    fetched_at: datetime
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        code = _require_code(self.code)
        trade_date = _require_date(self.trade_date, "trade_date")
        reason = _require_text(self.reason, "reason", maximum=32)
        if reason != "suspended":
            raise MarketContractError("only suspended absences are persistent market facts")
        source_at = _require_time(self.source_at, "source_at")
        _require_time(self.fetched_at, "fetched_at")
        if trade_date > _market_date(source_at):
            raise MarketContractError("absence trade_date cannot be after source_at")
        source = _require_text(self.source, "source", maximum=128)
        payload = {
            "code": code,
            "reason": reason,
            "source": source,
            "source_at": source_at.isoformat(),
            "trade_date": trade_date.isoformat(),
        }
        for field_name, value in (("code", code), ("trade_date", trade_date), ("reason", reason), ("source", source)):
            object.__setattr__(self, field_name, value)
        object.__setattr__(self, "content_hash", _canonical_hash("absence", payload))
