"""TDX corporate-action fallback for complete forward-adjustment factors."""

from __future__ import annotations

import importlib
import math
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Mapping, Protocol

from advisor.market_daily.adjustments import AdjustmentError, normalize_forward_factors
from advisor.market_daily.contracts import AdjustmentFactor, CanonicalDailyBar, MarketContractError, exchange_for_code
from advisor.market_daily.providers.contracts import MarketProviderError
from advisor.market_daily.providers.tdx import DEFAULT_TDX_SERVERS, TdxDailyBarProvider, TdxServer


_ALGORITHM_VERSION = "tdx-corporate-actions@1"
_CONTEXT_DAYS = 366


class _DailyBarProvider(Protocol):
    def fetch_daily_bars(self, code: str, start: date, end: date) -> tuple[CanonicalDailyBar, ...]: ...


class _CorporateActionProvider(Protocol):
    def fetch_corporate_actions(self, market: int, code: str) -> tuple[Mapping[str, object], ...]: ...


class _TdxCorporateActionsClient(Protocol):
    def connect(self, ip: str, port: int, time_out: float = 5.0) -> object: ...

    def disconnect(self) -> Any: ...

    def get_xdxr_info(self, market: int, code: str) -> object: ...


def _default_corporate_actions_client() -> _TdxCorporateActionsClient:
    try:
        module = importlib.import_module("tdxpy.hq")
        factory = getattr(module, "TdxHq_API")
    except (ImportError, AttributeError) as error:
        raise MarketProviderError("TDX 复权依赖未安装或不可用") from error
    return factory(heartbeat=False, auto_retry=False, raise_exception=False)


class TdxCorporateActionAdapter:
    """Read one complete corporate-action response from a public TDX endpoint."""

    def __init__(
        self,
        client_factory: Callable[[], _TdxCorporateActionsClient] | None = None,
        *,
        servers: tuple[TdxServer, ...] = DEFAULT_TDX_SERVERS,
        timeout_seconds: float = 8.0,
    ) -> None:
        if not servers or any(not isinstance(server, TdxServer) for server in servers):
            raise ValueError("at least one TDX server is required")
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._client_factory = client_factory or _default_corporate_actions_client
        self.servers = servers
        self.timeout_seconds = float(timeout_seconds)

    def fetch_corporate_actions(self, market: int, code: str) -> tuple[Mapping[str, object], ...]:
        if market not in {0, 1}:
            raise MarketProviderError("TDX 复权市场无效")
        if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
            raise MarketProviderError("TDX 复权证券代码无效")
        for server in self.servers:
            client: _TdxCorporateActionsClient | None = None
            try:
                client = self._client_factory()
                if not client.connect(server.host, server.port, time_out=self.timeout_seconds):
                    continue
                raw = client.get_xdxr_info(market, code)
                if not isinstance(raw, (list, tuple)) or any(not isinstance(item, Mapping) for item in raw):
                    raise MarketProviderError("TDX 公司行动响应无效")
                return tuple(raw)
            except Exception:
                continue
            finally:
                if client is not None:
                    try:
                        client.disconnect()
                    except Exception:
                        pass
        raise MarketProviderError("TDX 公司行动连接或读取失败")


@dataclass(frozen=True)
class _CorporateAction:
    trade_date: date
    cash_per_share: float
    bonus_shares_per_share: float
    rights_shares_per_share: float
    rights_subscription_value_per_share: float


class TdxAdjustmentFactorProvider:
    """Derive a complete factor set from TDX raw bars and corporate actions."""

    source = "tdx_adjustment"

    def __init__(
        self,
        bars: _DailyBarProvider | None = None,
        actions: _CorporateActionProvider | None = None,
        clock: Callable[[], datetime] | None = None,
        *,
        context_days: int = _CONTEXT_DAYS,
    ) -> None:
        if not isinstance(context_days, int) or isinstance(context_days, bool) or context_days <= 0:
            raise ValueError("context_days must be a positive integer")
        self._bars = bars or TdxDailyBarProvider()
        self._actions = actions or TdxCorporateActionAdapter()
        self._clock = clock or (lambda: datetime.now().astimezone())
        self.context_days = context_days

    def fetch_adjustment_factors(
        self, code: str, start: date, end: date
    ) -> tuple[AdjustmentFactor, ...]:
        try:
            exchange = exchange_for_code(code)
        except MarketContractError as error:
            raise MarketProviderError("证券代码不属于沪深 A 股范围") from error
        if code.startswith("689"):
            raise MarketProviderError("CDR 不在 Market Daily 范围内")
        if not isinstance(start, date) or isinstance(start, datetime) or not isinstance(end, date) or isinstance(end, datetime) or start > end:
            raise MarketProviderError("TDX 复权请求日期范围无效")
        fetched_at = self._clock()
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None or end > fetched_at.date():
            raise MarketProviderError("TDX 复权抓取时间或日期范围无效")
        try:
            raw_bars = self._bars.fetch_daily_bars(code, start - timedelta(days=self.context_days), end)
            target_bars, history_bars = _validate_bars(raw_bars, code, start, end)
            raw_actions = self._actions.fetch_corporate_actions(1 if exchange == "SH" else 0, code)
            actions = _relevant_actions(raw_actions, start, end)
            coefficients = _event_coefficients(actions, history_bars)
            raw_factors = tuple(
                (bar.trade_date, _factor_for_date(bar.trade_date, coefficients)) for bar in target_bars
            )
            return normalize_forward_factors(
                code,
                raw_factors,
                source=self.source,
                source_at=fetched_at,
                fetched_at=fetched_at,
                algorithm_version=_ALGORITHM_VERSION,
            )
        except MarketProviderError:
            raise
        except AdjustmentError as error:
            raise MarketProviderError("TDX 复权因子规范化失败") from error
        except (OSError, TypeError, ValueError, KeyError, OverflowError) as error:
            raise MarketProviderError("TDX 复权因子生成失败") from error


def _validate_bars(
    bars: object, code: str, start: date, end: date
) -> tuple[tuple[CanonicalDailyBar, ...], tuple[CanonicalDailyBar, ...]]:
    if not isinstance(bars, tuple) or not bars:
        raise MarketProviderError("TDX 复权缺少原始日线")
    if any(not isinstance(bar, CanonicalDailyBar) or bar.code != code for bar in bars):
        raise MarketProviderError("TDX 复权原始日线无效")
    ordered = tuple(sorted(bars, key=lambda bar: bar.trade_date))
    if ordered != bars or len({bar.trade_date for bar in bars}) != len(bars):
        raise MarketProviderError("TDX 复权原始日线日期必须升序唯一")
    target = tuple(bar for bar in bars if start <= bar.trade_date <= end)
    if not target:
        raise MarketProviderError("TDX 复权没有返回请求区间日线")
    return target, bars


def _relevant_actions(
    raw_actions: object, start: date, end: date
) -> tuple[_CorporateAction, ...]:
    if not isinstance(raw_actions, tuple) or any(not isinstance(item, Mapping) for item in raw_actions):
        raise MarketProviderError("TDX 公司行动集合无效")
    actions: list[_CorporateAction] = []
    for row in raw_actions:
        action_date = _action_date(row)
        if not start < action_date <= end:
            continue
        category = _whole_number(row.get("category"), "公司行动类别")
        if category == 1:
            cash = _non_negative(row.get("fenhong"), "现金红利") / 10
            bonus = _non_negative(row.get("songzhuangu"), "送转股") / 10
            rights = _non_negative(row.get("peigu"), "配股") / 10
            rights_price = _non_negative(row.get("peigujia"), "配股价")
            if cash or bonus or rights:
                actions.append(
                    _CorporateAction(
                        action_date,
                        cash,
                        bonus,
                        rights,
                        rights * rights_price,
                    )
                )
        elif category in {4, 11}:
            description = "未知股本变动" if category == 4 else "扩缩股"
            raise MarketProviderError(f"TDX {description}无法安全推导复权因子")
    return tuple(sorted(actions, key=lambda action: action.trade_date))


def _action_date(row: Mapping[str, object]) -> date:
    try:
        return date(
            _whole_number(row.get("year"), "公司行动年份"),
            _whole_number(row.get("month"), "公司行动月份"),
            _whole_number(row.get("day"), "公司行动日期"),
        )
    except ValueError as error:
        raise MarketProviderError("TDX 公司行动日期无效") from error


def _whole_number(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise MarketProviderError(f"TDX {name}无效")
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as error:
        raise MarketProviderError(f"TDX {name}无效") from error
    if str(result) != str(value).strip() and not isinstance(value, int):
        raise MarketProviderError(f"TDX {name}无效")
    return result


def _non_negative(value: object, name: str) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        raise MarketProviderError(f"TDX {name}无效")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise MarketProviderError(f"TDX {name}无效") from error
    if not math.isfinite(result) or result < 0:
        raise MarketProviderError(f"TDX {name}无效")
    return result


def _event_coefficients(
    actions: tuple[_CorporateAction, ...], history_bars: tuple[CanonicalDailyBar, ...]
) -> tuple[tuple[date, float], ...]:
    close_by_date = {bar.trade_date: bar.close for bar in history_bars}
    dates = tuple(sorted(close_by_date))
    grouped: dict[date, list[_CorporateAction]] = {}
    for action in actions:
        grouped.setdefault(action.trade_date, []).append(action)
    results: list[tuple[date, float]] = []
    for action_date, grouped_actions in sorted(grouped.items()):
        previous_index = bisect_left(dates, action_date) - 1
        if previous_index < 0:
            raise MarketProviderError("TDX 公司行动缺少前一交易日收盘价")
        previous_close = close_by_date[dates[previous_index]]
        cash = sum(action.cash_per_share for action in grouped_actions)
        bonus = sum(action.bonus_shares_per_share for action in grouped_actions)
        rights = sum(action.rights_shares_per_share for action in grouped_actions)
        rights_value = sum(action.rights_subscription_value_per_share for action in grouped_actions)
        numerator = previous_close - cash + rights_value
        denominator = previous_close * (1 + bonus + rights)
        if not math.isfinite(numerator) or not math.isfinite(denominator) or numerator <= 0 or denominator <= 0:
            raise MarketProviderError("TDX 公司行动不能生成正复权因子")
        coefficient = numerator / denominator
        if not math.isfinite(coefficient) or coefficient <= 0:
            raise MarketProviderError("TDX 公司行动复权因子无效")
        results.append((action_date, coefficient))
    return tuple(results)


def _factor_for_date(trade_date: date, coefficients: tuple[tuple[date, float], ...]) -> float:
    factor = 1.0
    for action_date, coefficient in coefficients:
        if action_date > trade_date:
            factor *= coefficient
    if not math.isfinite(factor) or factor <= 0:
        raise MarketProviderError("TDX 累计复权因子无效")
    return factor
