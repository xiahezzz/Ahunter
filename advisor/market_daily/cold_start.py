"""Five-calendar-year cold-start planning over observed market facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from advisor.market_daily.control import (
    MarketDailyControlPlane,
    MarketDailyRequest,
    MarketDailyRun,
    RunSecurity,
)
from advisor.market_daily.engine import MarketDailyEngine
from advisor.market_daily.sessions import ObservedSessionService, SessionObservationError
from advisor.market_daily.universe import HistoricalUniverseService, UniverseSnapshot


class ColdStartError(RuntimeError):
    """The five-year baseline cannot safely be planned or resumed."""


class _SessionService(Protocol):
    def refresh(self, start: date, end: date, as_of: datetime) -> object: ...

    def latest_completed_session(self, as_of: datetime) -> date: ...

    def sessions_for(self, start: date, end: date) -> tuple[date, ...]: ...


class _UniverseService(Protocol):
    def refresh(self) -> UniverseSnapshot: ...


class _RunExecutor(Protocol):
    def execute_run(
        self, run_id: str, sessions: tuple[date, ...], owner_id: str, now: datetime
    ) -> MarketDailyRun: ...


@dataclass(frozen=True)
class ColdStartPlan:
    request: MarketDailyRequest
    run: MarketDailyRun
    target_session: date
    start_date: date
    sessions: tuple[date, ...]
    universe_hash: str
    securities: tuple[RunSecurity, ...]


class ColdStartWorkflow:
    """Freeze one observed window and one historical-universe snapshot."""

    def __init__(
        self,
        control: MarketDailyControlPlane,
        sessions: ObservedSessionService | _SessionService,
        universe: HistoricalUniverseService | _UniverseService,
        engine: MarketDailyEngine | _RunExecutor,
    ) -> None:
        self._control = control
        self._sessions = sessions
        self._universe = universe
        self._engine = engine

    def submit(self, now: datetime) -> MarketDailyRequest:
        """Create an idempotent operator intent without querying any source."""

        return self._control.submit_cold_start_intent(now)

    def prepare_claimed(self, request: MarketDailyRequest, now: datetime) -> ColdStartPlan:
        if request.request_type != "cold_start" or request.status != "claimed":
            raise ColdStartError("只有已认领的冷启动请求可以冻结")
        target = request.target_session
        if target is None:
            # A short refresh establishes the target first.  The full window
            # is fetched below after the target is known.
            probe_start = _calendar_years_before(now.date(), 1)
            self._sessions.refresh(probe_start, now.date(), now)
            target = self._sessions.latest_completed_session(now)
        nominal_start = _calendar_years_before(target, 5)
        self._sessions.refresh(nominal_start, target, now)
        sessions = self._sessions.sessions_for(nominal_start, target)
        if not sessions:
            raise ColdStartError("五年窗口内没有已证明的交易日")
        start = sessions[0]
        if sessions[-1] != target:
            raise ColdStartError("目标交易日没有被完整观察")
        frozen_request = self._control.configure_claimed_request(
            request.request_id, target, start, target, now
        )
        source_snapshot = self._universe.refresh()
        eligible = source_snapshot.for_window(start, target)
        if not eligible:
            raise ColdStartError("五年窗口内没有可摄取的沪深 A 股")
        frozen_snapshot = UniverseSnapshot(eligible)
        securities = tuple(
            RunSecurity(
                code=security.code,
                name=security.name,
                exchange=security.exchange,
                list_date=security.list_date,
                delist_date=security.delist_date,
                status=security.status,
            )
            for security in frozen_snapshot.securities
        )
        run = self._control.create_run(
            frozen_request.request_id,
            frozen_snapshot.content_hash,
            securities,
            now,
        )
        return ColdStartPlan(
            request=frozen_request,
            run=run,
            target_session=target,
            start_date=start,
            sessions=sessions,
            universe_hash=frozen_snapshot.content_hash,
            securities=securities,
        )

    def execute_claimed(self, request: MarketDailyRequest, owner_id: str, now: datetime) -> MarketDailyRun:
        plan = self.prepare_claimed(request, now)
        run = plan.run
        if run.status == "complete":
            return run
        if run.status == "partial":
            run = self._control.resume_partial_run(run.run_id, now)
        if run.status not in {"pending", "running"}:
            raise ColdStartError("冷启动运行状态不能执行")
        return self._engine.execute_run(run.run_id, plan.sessions, owner_id, now)

    def resume_partial(self, owner_id: str, now: datetime) -> MarketDailyRun | None:
        run = self._control.latest_run(run_type="cold_start", statuses=("pending", "partial"))
        if run is None:
            return None
        sessions = self._sessions.sessions_for(run.start_date, run.end_date)
        if not sessions or sessions[-1] != run.target_session:
            raise ColdStartError("无法在不完整交易日事实上恢复冷启动")
        if run.status == "partial":
            self._control.resume_partial_run(run.run_id, now)
        return self._engine.execute_run(run.run_id, sessions, owner_id, now)


def _calendar_years_before(value: date, years: int) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ColdStartError("日期无效")
    if not isinstance(years, int) or isinstance(years, bool) or years <= 0:
        raise ColdStartError("回溯年数无效")
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        # The only possible invalid calendar anniversary is 29 February.
        return value.replace(year=value.year - years, day=28)
