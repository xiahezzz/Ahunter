"""Resumable, per-security Market Daily ingestion independent of scheduling and UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Protocol

from advisor.market_daily.adjustments import AdjustmentFactorProvider
from advisor.market_daily.contracts import AdjustmentFactor, CanonicalDailyBar, MarketAbsence
from advisor.market_daily.control import (
    MarketDailyControlPlane,
    MarketDailyRun,
    RunSecurity,
    bounded_error_message,
)
from advisor.market_daily.providers.contracts import MarketProviderError
from advisor.market_daily.providers.registry import ProviderChainResult
from advisor.market_daily.repository import MarketDailyRepository


class IngestionError(RuntimeError):
    """An ingestion request cannot be safely completed."""


class DailyObservationFetcher(Protocol):
    def fetch_daily_bars(self, code: str, start: date, end: date) -> ProviderChainResult: ...


class SuspensionEvidenceProvider(Protocol):
    def absences_for(
        self, code: str, sessions: tuple[date, ...], now: datetime
    ) -> tuple[MarketAbsence, ...]: ...


class NoSuspensionEvidence:
    def absences_for(
        self, _code: str, _sessions: tuple[date, ...], _now: datetime
    ) -> tuple[MarketAbsence, ...]:
        return ()


@dataclass(frozen=True)
class SecurityIngestionResult:
    code: str
    status: str
    expected_sessions: int
    covered_sessions: int
    selected_source: str | None
    message: str | None = None


class MarketDailyEngine:
    """Commit one security at a time; completed facts survive any later failure."""

    def __init__(
        self,
        repository: MarketDailyRepository,
        control: MarketDailyControlPlane,
        bars: DailyObservationFetcher,
        factors: AdjustmentFactorProvider,
        *,
        absences: SuspensionEvidenceProvider | None = None,
        after_commit: Callable[[str], None] | None = None,
        heartbeat: Callable[[str], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        item_lease_seconds: int = 300,
    ) -> None:
        if not isinstance(item_lease_seconds, int) or isinstance(item_lease_seconds, bool) or item_lease_seconds <= 0:
            raise ValueError("item_lease_seconds must be a positive integer")
        self._repository = repository
        self._control = control
        self._bars = bars
        self._factors = factors
        self._absences = absences or NoSuspensionEvidence()
        self._after_commit = after_commit
        self._heartbeat = heartbeat
        self._clock = clock
        self._item_lease_seconds = item_lease_seconds

    def execute_run(
        self,
        run_id: str,
        sessions: tuple[date, ...],
        owner_id: str,
        now: datetime,
    ) -> MarketDailyRun:
        run = self._control.run(run_id)
        if run.status == "pending":
            run = self._control.start_run(run_id, self._work_now(now))
        if run.status != "running":
            raise IngestionError("只有待运行或运行中的任务可以摄取")
        normalized_sessions = _validate_sessions(sessions, run.start_date, run.end_date)
        for security in self._control.run_securities(run_id):
            if self._heartbeat is not None:
                self._heartbeat(owner_id)
            item = self._control.item(run_id, security.code)
            if item.status in {"completed", "source_missing", "conflicted", "skipped"}:
                continue
            self.ingest_security_interval(run_id, security, normalized_sessions, owner_id, now)
            if self._heartbeat is not None:
                self._heartbeat(owner_id)
        return self._control.finalize_run(run_id, self._work_now(now))

    def ingest_security_interval(
        self,
        run_id: str,
        security: RunSecurity,
        sessions: tuple[date, ...],
        owner_id: str,
        now: datetime,
    ) -> SecurityIngestionResult:
        claim_now = self._work_now(now)
        if not self._control.claim_item(
            run_id,
            security.code,
            owner_id,
            claim_now,
            lease_seconds=self._item_lease_seconds,
        ):
            current = self._control.item(run_id, security.code)
            return SecurityIngestionResult(
                code=security.code,
                status=current.status,
                expected_sessions=0,
                covered_sessions=0,
                selected_source=current.selected_source,
            )
        run = self._control.run(run_id)
        item = self._control.item(run_id, security.code)
        expected = _expected_sessions(
            security,
            sessions,
            max(run.start_date, item.start_date),
            min(run.end_date, item.end_date),
        )
        if not expected:
            self._control.mark_item(
                run_id, security.code, "skipped", self._work_now(now), selected_source="listing_interval"
            )
            return SecurityIngestionResult(security.code, "skipped", 0, 0, "listing_interval")
        try:
            covered = set(self._repository.covered_dates(security.code, expected[0], expected[-1]))
            missing = tuple(session for session in expected if session not in covered)
            selected_source: str | None = None
            if missing:
                self._renew_work_lease(run_id, security.code, owner_id, now)
                observation = self._bars.fetch_daily_bars(security.code, missing[0], missing[-1])
                selected_source = observation.selected_source
                bars = _select_expected_bars(observation.bars, security.code, expected, set(missing))
                received_dates = {bar.trade_date for bar in bars}
                unresolved = tuple(session for session in missing if session not in received_dates)
                self._renew_work_lease(run_id, security.code, owner_id, now)
                absences = _validate_absences(
                    self._absences.absences_for(security.code, unresolved, self._work_now(now)),
                    security.code,
                    set(unresolved),
                )
                absent_dates = {absence.trade_date for absence in absences}
                self._renew_work_lease(run_id, security.code, owner_id, now)
                factors = self._fetch_factors(security.code, bars, self._work_now(now))
                result = self._repository.commit_security_observations(bars, factors, absences)
                if result.conflicted_bars or result.conflicted_absences:
                    self._control.mark_item(
                        run_id, security.code, "conflicted", self._work_now(now), selected_source=selected_source,
                        error="已有行情或停牌事实与新来源冲突",
                    )
                    return SecurityIngestionResult(
                        security.code, "conflicted", len(expected), len(covered), selected_source
                    )
                if self._after_commit is not None:
                    self._after_commit(security.code)
                unresolved = tuple(session for session in unresolved if session not in absent_dates)
                if unresolved:
                    self._control.mark_item(
                        run_id, security.code, "source_missing", self._work_now(now), selected_source=selected_source,
                        error="来源未证明该交易日存在日线或停牌",
                    )
                    return SecurityIngestionResult(
                        security.code,
                        "source_missing",
                        len(expected),
                        len(expected) - len(unresolved),
                        selected_source,
                    )
            factor_status = self._ensure_factor_coverage(
                run_id, security.code, expected, owner_id, now
            )
            if factor_status is not None:
                self._control.mark_item(
                    run_id, security.code, "source_missing", self._work_now(now), selected_source=selected_source,
                    error=factor_status,
                )
                return SecurityIngestionResult(
                    security.code, "source_missing", len(expected), len(expected), selected_source, factor_status
                )
            self._control.mark_item(
                run_id,
                security.code,
                "completed",
                self._work_now(now),
                selected_source=selected_source or "existing_coverage",
            )
            return SecurityIngestionResult(
                security.code,
                "completed",
                len(expected),
                len(expected),
                selected_source or "existing_coverage",
            )
        except (MarketProviderError, IngestionError, ValueError, KeyError) as error:
            self._control.mark_item(
                run_id,
                security.code,
                "source_missing",
                self._work_now(now),
                error=_safe_error(error),
            )
            return SecurityIngestionResult(
                security.code, "source_missing", len(expected), 0, None, _safe_error(error)
            )

    def _fetch_factors(
        self, code: str, bars: tuple[CanonicalDailyBar, ...], now: datetime
    ) -> tuple[AdjustmentFactor, ...]:
        if not bars:
            return ()
        received = self._factors.fetch_adjustment_factors(code, bars[0].trade_date, bars[-1].trade_date)
        by_date = {factor.trade_date: factor for factor in received if factor.code == code}
        expected_dates = {bar.trade_date for bar in bars}
        if set(by_date) != expected_dates or len(by_date) != len(received):
            raise IngestionError("复权来源没有完整覆盖已接收日线")
        return tuple(by_date[bar.trade_date] for bar in bars)

    def _ensure_factor_coverage(
        self,
        run_id: str,
        code: str,
        expected: tuple[date, ...],
        owner_id: str,
        now: datetime,
    ) -> str | None:
        raw_bars = self._repository.bars_for(code, expected[0], expected[-1])
        if not raw_bars:
            return None
        required_dates = {bar.trade_date for bar in raw_bars}
        existing_dates = set(self._repository.factor_dates(code, expected[0], expected[-1]))
        missing_dates = tuple(sorted(required_dates - existing_dates))
        if not missing_dates:
            return None
        try:
            self._renew_work_lease(run_id, code, owner_id, now)
            received = self._factors.fetch_adjustment_factors(code, missing_dates[0], missing_dates[-1])
        except Exception as error:
            return _safe_error(error)
        selected = {factor.trade_date: factor for factor in received if factor.code == code}
        if not set(missing_dates).issubset(selected):
            return "复权来源没有完整覆盖已有日线"
        self._repository.commit_security_observations((), tuple(selected[item] for item in missing_dates), ())
        return None

    def _work_now(self, fallback: datetime) -> datetime:
        moment = self._clock() if self._clock is not None else fallback
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            raise IngestionError("摄取时钟必须带时区")
        return moment

    def _renew_work_lease(self, run_id: str, code: str, owner_id: str, fallback: datetime) -> None:
        moment = self._work_now(fallback)
        if self._heartbeat is not None:
            self._heartbeat(owner_id)
        if not self._control.renew_item_claim(
            run_id,
            code,
            owner_id,
            moment,
            lease_seconds=self._item_lease_seconds,
        ):
            raise IngestionError("逐证券任务租约已失效")


def _validate_sessions(sessions: object, start: date, end: date) -> tuple[date, ...]:
    if not isinstance(sessions, tuple) or not sessions:
        raise IngestionError("交易日集合不能为空")
    if any(not isinstance(session, date) or isinstance(session, datetime) for session in sessions):
        raise IngestionError("交易日集合无效")
    if sessions != tuple(sorted(sessions)) or len(sessions) != len(set(sessions)):
        raise IngestionError("交易日集合必须升序且唯一")
    if sessions[0] < start or sessions[-1] > end:
        raise IngestionError("交易日集合超出运行区间")
    return sessions


def _expected_sessions(
    security: RunSecurity, sessions: tuple[date, ...], run_start: date, run_end: date
) -> tuple[date, ...]:
    start = max(run_start, security.list_date)
    end = min(run_end, security.delist_date) if security.delist_date else run_end
    return tuple(session for session in sessions if start <= session <= end)


def _select_expected_bars(
    bars: object, code: str, expected: tuple[date, ...], missing: set[date]
) -> tuple[CanonicalDailyBar, ...]:
    if not isinstance(bars, tuple):
        raise IngestionError("行情源返回无效日线集合")
    expected_set = set(expected)
    selected: list[CanonicalDailyBar] = []
    seen: set[date] = set()
    for bar in bars:
        if not isinstance(bar, CanonicalDailyBar) or bar.code != code:
            raise IngestionError("行情源返回其他证券日线")
        if bar.trade_date not in expected_set:
            raise IngestionError("行情源返回非交易日日线")
        if bar.trade_date in seen:
            raise IngestionError("行情源返回重复日线")
        seen.add(bar.trade_date)
        if bar.trade_date in missing:
            selected.append(bar)
    return tuple(sorted(selected, key=lambda bar: bar.trade_date))


def _validate_absences(
    absences: object, code: str, unresolved: set[date]
) -> tuple[MarketAbsence, ...]:
    if not isinstance(absences, tuple):
        raise IngestionError("停牌证据返回无效")
    seen: set[date] = set()
    for absence in absences:
        if not isinstance(absence, MarketAbsence) or absence.code != code or absence.trade_date not in unresolved:
            raise IngestionError("停牌证据超出未覆盖交易日")
        if absence.trade_date in seen:
            raise IngestionError("停牌证据日期重复")
        seen.add(absence.trade_date)
    return tuple(sorted(absences, key=lambda absence: absence.trade_date))


def _safe_error(error: Exception) -> str:
    return bounded_error_message(str(error), fallback=f"{type(error).__name__}：数据处理失败")
