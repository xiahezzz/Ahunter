"""Bounded whole-result fallback for Market Daily adjustment factors."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from advisor.market_daily.contracts import AdjustmentFactor
from advisor.market_daily.providers.contracts import MarketProviderError


class AdjustmentFactorProvider(Protocol):
    source: str

    def fetch_adjustment_factors(
        self, code: str, start: date, end: date
    ) -> tuple[AdjustmentFactor, ...]: ...


class AdjustmentFactorProviderChain:
    """Use the configured primary result whole, then one complete fallback result."""

    def __init__(self, primary: AdjustmentFactorProvider, fallback: AdjustmentFactorProvider) -> None:
        if primary.source == fallback.source:
            raise ValueError("primary and fallback adjustment sources must be different")
        self.primary = primary
        self.fallback = fallback

    def fetch_adjustment_factors(
        self, code: str, start: date, end: date
    ) -> tuple[AdjustmentFactor, ...]:
        try:
            return self._complete_result(self.primary, code, start, end)
        except Exception as primary_error:
            try:
                return self._complete_result(self.fallback, code, start, end)
            except Exception as fallback_error:
                raise MarketProviderError("主备复权来源均未返回完整因子") from fallback_error

    @staticmethod
    def _complete_result(
        provider: AdjustmentFactorProvider, code: str, start: date, end: date
    ) -> tuple[AdjustmentFactor, ...]:
        factors = provider.fetch_adjustment_factors(code, start, end)
        if not isinstance(factors, tuple) or not factors:
            raise MarketProviderError("复权来源没有返回完整因子集合")
        dates: set[date] = set()
        for factor in factors:
            if not isinstance(factor, AdjustmentFactor):
                raise MarketProviderError("复权来源返回了非规范因子")
            if factor.code != code or factor.trade_date < start or factor.trade_date > end:
                raise MarketProviderError("复权来源返回了不属于请求的因子")
            if factor.source != provider.source:
                raise MarketProviderError("复权来源混入了其他来源因子")
            if factor.trade_date in dates:
                raise MarketProviderError("复权来源返回了重复因子日期")
            dates.add(factor.trade_date)
        ordered = tuple(sorted(factors, key=lambda factor: factor.trade_date))
        if ordered != factors:
            raise MarketProviderError("复权来源因子未按日期升序排列")
        return factors
