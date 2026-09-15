"""Scope-aware Market Daily availability checks for research and scans."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from advisor.market_daily.control import MarketDailyControlPlane, RunSecurity
from advisor.market_daily.repository import MarketDailyRepository


@dataclass(frozen=True)
class SecurityReadiness:
    code: str
    ready: bool
    missing_sessions: tuple[date, ...]
    missing_factors: tuple[date, ...]
    reason: str | None = None


@dataclass(frozen=True)
class MarketReadiness:
    target_session: date
    ready: bool
    run_id: str | None
    reason: str | None = None


class MarketDailyReadiness:
    def __init__(self, repository: MarketDailyRepository, control: MarketDailyControlPlane) -> None:
        self._repository = repository
        self._control = control

    def security(
        self, security: RunSecurity, sessions: tuple[date, ...], start: date, end: date
    ) -> SecurityReadiness:
        if not sessions:
            return SecurityReadiness(security.code, False, (), (), "没有已证明的交易日")
        effective_end = min(end, security.delist_date) if security.delist_date else end
        expected = tuple(item for item in sessions if max(start, security.list_date) <= item <= effective_end)
        if not expected:
            return SecurityReadiness(security.code, True, (), ())
        covered = set(self._repository.covered_dates(security.code, expected[0], expected[-1]))
        missing_sessions = tuple(item for item in expected if item not in covered)
        bars = self._repository.bars_for(security.code, expected[0], expected[-1])
        factor_dates = set(self._repository.factor_dates(security.code, expected[0], expected[-1]))
        missing_factors = tuple(bar.trade_date for bar in bars if bar.trade_date not in factor_dates)
        ready = not missing_sessions and not missing_factors
        reason = None
        if missing_sessions:
            reason = "存在未证明的交易日覆盖"
        elif missing_factors:
            reason = "存在缺失的复权因子"
        return SecurityReadiness(security.code, ready, missing_sessions, missing_factors, reason)

    def market(self, target_session: date) -> MarketReadiness:
        run = self._control.latest_run_for_target(target_session)
        if run is None:
            return MarketReadiness(target_session, False, None, "没有目标交易日的 Market Daily 运行")
        if run.status != "complete":
            return MarketReadiness(target_session, False, run.run_id, "全市场运行尚未完整")
        return MarketReadiness(target_session, True, run.run_id)
