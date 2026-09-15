"""Independent adjustment factors and derived forward-adjusted research prices."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Protocol

from advisor.market_daily.contracts import AdjustmentFactor, CanonicalDailyBar


ALGORITHM_VERSION = "forward-adjustment@1"
_HASH_DOMAIN = b"a-hunter:market-daily-research-prices:v1\0"


class AdjustmentError(RuntimeError):
    """Adjustment factors cannot safely produce a research price series."""


class AdjustmentFactorProvider(Protocol):
    source: str

    def fetch_adjustment_factors(
        self, code: str, start: date, end: date
    ) -> tuple[AdjustmentFactor, ...]: ...


@dataclass(frozen=True)
class ResearchPriceBar:
    code: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    factor: float
    raw_content_hash: str
    factor_content_hash: str


@dataclass(frozen=True)
class ResearchPriceSeries:
    code: str
    bars: tuple[ResearchPriceBar, ...]
    factor_set_hash: str
    algorithm_version: str = ALGORITHM_VERSION
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.bars:
            raise AdjustmentError("研究价格序列不能为空")
        if any(bar.code != self.code for bar in self.bars):
            raise AdjustmentError("研究价格序列包含其他证券")
        dates = tuple(bar.trade_date for bar in self.bars)
        if dates != tuple(sorted(dates)) or len(dates) != len(set(dates)):
            raise AdjustmentError("研究价格日期必须升序且唯一")
        if not isinstance(self.factor_set_hash, str) or len(self.factor_set_hash) != 64:
            raise AdjustmentError("复权因子集合哈希无效")
        payload = {
            "algorithm_version": self.algorithm_version,
            "bars": [
                {
                    "close": bar.close,
                    "code": bar.code,
                    "factor_content_hash": bar.factor_content_hash,
                    "high": bar.high,
                    "low": bar.low,
                    "open": bar.open,
                    "raw_content_hash": bar.raw_content_hash,
                    "trade_date": bar.trade_date.isoformat(),
                }
                for bar in self.bars
            ],
            "code": self.code,
            "factor_set_hash": self.factor_set_hash,
        }
        object.__setattr__(
            self,
            "content_hash",
            hashlib.sha256(
                _HASH_DOMAIN
                + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        )


def normalize_forward_factors(
    code: str,
    raw_factors: Iterable[tuple[date, float]],
    *,
    source: str,
    source_at,
    fetched_at,
    algorithm_version: str = ALGORITHM_VERSION,
) -> tuple[AdjustmentFactor, ...]:
    """Scale source ratios so that the latest available date is exactly one."""

    values = tuple(raw_factors)
    if not values:
        raise AdjustmentError("复权来源没有返回因子")
    dates: list[date] = []
    normalized_input: list[tuple[date, float]] = []
    for trade_date, factor in values:
        if not isinstance(trade_date, date):
            raise AdjustmentError("复权因子日期无效")
        try:
            numeric = float(factor)
        except (TypeError, ValueError, OverflowError) as error:
            raise AdjustmentError("复权因子无效") from error
        if not math.isfinite(numeric) or numeric <= 0:
            raise AdjustmentError("复权因子必须为正有限数")
        dates.append(trade_date)
        normalized_input.append((trade_date, numeric))
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise AdjustmentError("复权因子日期必须升序且唯一")
    latest = normalized_input[-1][1]
    factors = tuple(
        AdjustmentFactor(
            code=code,
            trade_date=trade_date,
            factor=factor / latest,
            source=source,
            source_at=source_at,
            fetched_at=fetched_at,
            algorithm_version=algorithm_version,
        )
        for trade_date, factor in normalized_input
    )
    if factors[-1].factor != 1.0:
        raise AdjustmentError("最新复权因子必须为 1")
    return factors


def derive_forward_adjusted_prices(
    bars: Iterable[CanonicalDailyBar], factors: Iterable[AdjustmentFactor]
) -> ResearchPriceSeries:
    raw_bars = tuple(bars)
    factor_rows = tuple(factors)
    if not raw_bars:
        raise AdjustmentError("原始日线不能为空")
    code = raw_bars[0].code
    dates = tuple(bar.trade_date for bar in raw_bars)
    if any(bar.code != code for bar in raw_bars) or dates != tuple(sorted(dates)) or len(dates) != len(set(dates)):
        raise AdjustmentError("原始日线必须是单证券升序唯一序列")
    factor_by_date = {factor.trade_date: factor for factor in factor_rows}
    if len(factor_by_date) != len(factor_rows):
        raise AdjustmentError("复权因子日期重复")
    if set(factor_by_date) != set(dates):
        raise AdjustmentError("原始日线与复权因子日期不完整匹配")
    research_bars: list[ResearchPriceBar] = []
    for bar in raw_bars:
        factor = factor_by_date[bar.trade_date]
        if factor.code != code:
            raise AdjustmentError("复权因子与原始日线证券不匹配")
        adjusted = tuple(value * factor.factor for value in (bar.open, bar.high, bar.low, bar.close))
        open_price, high, low, close = adjusted
        if not low <= open_price <= high or not low <= close <= high:
            raise AdjustmentError("前复权价格违反 OHLC 边界")
        research_bars.append(
            ResearchPriceBar(
                code=code,
                trade_date=bar.trade_date,
                open=open_price,
                high=high,
                low=low,
                close=close,
                factor=factor.factor,
                raw_content_hash=bar.content_hash,
                factor_content_hash=factor.content_hash,
            )
        )
    factor_set_hash = hashlib.sha256(
        "\n".join(
            f"{factor.trade_date.isoformat()}:{factor.content_hash}"
            for factor in sorted(factor_rows, key=lambda item: item.trade_date)
        ).encode("utf-8")
    ).hexdigest()
    return ResearchPriceSeries(code=code, bars=tuple(research_bars), factor_set_hash=factor_set_hash)
