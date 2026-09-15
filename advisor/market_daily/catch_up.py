"""Gap-aware daily catch-up over a completed Market Daily baseline."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol

from advisor.market_daily.control import (
    MarketDailyControlPlane,
    MarketDailyRequest,
    MarketDailyRun,
    RunSecurity,
)
from advisor.market_daily.engine import MarketDailyEngine
from advisor.market_daily.repository import MarketDailyRepository
from advisor.market_daily.sessions import ObservedSessionService
from advisor.market_daily.universe import HistoricalUniverseService, UniverseSnapshot


class CatchUpError(RuntimeError):
    """Daily incremental market work cannot safely be planned."""


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
class CatchUpPlan:
    target_session: date
    start_date: date
    sessions: tuple[date, ...]
    universe_hash: str
    securities: tuple[RunSecurity, ...]
    item_ranges: dict[str, tuple[date, date]]
    scope_hash: str


class CatchUpWorkflow:
    """Plan only uncovered, eligible per-security session intervals."""

    def __init__(
        self,
        repository: MarketDailyRepository,
        control: MarketDailyControlPlane,
        sessions: ObservedSessionService | _SessionService,
        universe: HistoricalUniverseService | _UniverseService,
        engine: MarketDailyEngine | _RunExecutor,
    ) -> None:
        self._repository = repository
        self._control = control
        self._sessions = sessions
        self._universe = universe
        self._engine = engine

    def submit_due(self, now: datetime) -> MarketDailyRequest | None:
        plan = self.plan_due(now)
        if plan is None:
            return None
        return self._control.submit_catch_up(
            plan.start_date,
            plan.target_session,
            now,
            scope_hash=plan.scope_hash,
        )

    def plan_due(self, now: datetime) -> CatchUpPlan | None:
        baseline = self._control.latest_run(run_type="cold_start", statuses=("complete",))
        if baseline is None:
            raise CatchUpError("冷启动基线尚未完整，不能执行日常补洞")
        # Only a short look-back must be fetched daily.  The immutable older
        # sessions were already proved while constructing the cold baseline.
        probe_start = max(baseline.start_date, now.date() - timedelta(days=10))
        self._sessions.refresh(probe_start, now.date(), now)
        target = self._sessions.latest_completed_session(now)
        return self._build_plan(target, baseline.start_date)

    def execute_claimed(self, request: MarketDailyRequest, owner_id: str, now: datetime) -> MarketDailyRun | None:
        if request.request_type != "catch_up" or request.status != "claimed":
            raise CatchUpError("只有已认领的补洞请求可以执行")
        baseline = self._control.latest_run(run_type="cold_start", statuses=("complete",))
        if baseline is None:
            raise CatchUpError("冷启动基线尚未完整，不能执行日常补洞")
        if request.target_session is None:
            raise CatchUpError("补洞请求缺少目标交易日")
        probe_start = max(baseline.start_date, now.date() - timedelta(days=10))
        self._sessions.refresh(probe_start, now.date(), now)
        sessions = self._sessions.sessions_for(baseline.start_date, request.target_session)
        if not sessions or sessions[-1] != request.target_session:
            raise CatchUpError("补洞目标没有完整的交易日证明")
        plan = self._build_plan(request.target_session, baseline.start_date)
        if plan is None:
            self._control.complete_claimed_request(
                request.request_id, owner_id, now, message="目标交易日已完整覆盖，无需补洞"
            )
            return None
        if plan.start_date < (request.start_date or plan.start_date):
            raise CatchUpError("当前缺口早于已冻结的补洞请求窗口")
        run = self._control.create_run(
            request.request_id,
            plan.universe_hash,
            plan.securities,
            now,
            item_ranges=plan.item_ranges,
        )
        if run.status == "complete":
            return run
        if run.status == "partial":
            run = self._control.resume_partial_run(run.run_id, now)
        run_sessions = tuple(
            session for session in sessions if run.start_date <= session <= run.end_date
        )
        return self._engine.execute_run(run.run_id, run_sessions, owner_id, now)

    def resume_partial(self, owner_id: str, now: datetime) -> MarketDailyRun | None:
        run = self._control.latest_run(run_type="catch_up", statuses=("pending", "partial"))
        if run is None:
            return None
        sessions = self._sessions.sessions_for(run.start_date, run.end_date)
        if not sessions or sessions[-1] != run.target_session:
            raise CatchUpError("无法在不完整交易日事实上恢复补洞")
        if run.status == "partial":
            self._control.resume_partial_run(run.run_id, now)
        return self._engine.execute_run(run.run_id, sessions, owner_id, now)

    def _build_plan(self, target: date, base_start: date) -> CatchUpPlan | None:
        sessions = self._sessions.sessions_for(base_start, target)
        if not sessions or sessions[-1] != target:
            raise CatchUpError("本地交易日事实不完整")
        source_snapshot = self._universe.refresh()
        eligible = source_snapshot.for_window(base_start, target)
        ranges: dict[str, tuple[date, date]] = {}
        selected = []
        for security in eligible:
            expected = _expected_sessions(security.list_date, security.delist_date, sessions)
            if not expected:
                continue
            covered = set(self._repository.covered_dates(security.code, expected[0], expected[-1]))
            missing = tuple(item for item in expected if item not in covered)
            if not missing:
                continue
            selected.append(security)
            ranges[security.code] = (missing[0], missing[-1])
        if not selected:
            return None
        frozen = UniverseSnapshot(tuple(selected))
        run_securities = tuple(
            RunSecurity(
                security.code,
                security.name,
                security.exchange,
                security.list_date,
                security.delist_date,
                security.status,
            )
            for security in frozen.securities
        )
        start = min(interval[0] for interval in ranges.values())
        scope_hash = _scope_hash(target, frozen.content_hash, ranges)
        return CatchUpPlan(target, start, sessions, frozen.content_hash, run_securities, ranges, scope_hash)


def _expected_sessions(
    list_date: date, delist_date: date | None, sessions: tuple[date, ...]
) -> tuple[date, ...]:
    end = delist_date if delist_date is not None else sessions[-1]
    return tuple(item for item in sessions if list_date <= item <= end)


def _scope_hash(target: date, universe_hash: str, ranges: dict[str, tuple[date, date]]) -> str:
    material = {
        "target": target.isoformat(),
        "universe_hash": universe_hash,
        "ranges": [
            [code, interval[0].isoformat(), interval[1].isoformat()]
            for code, interval in sorted(ranges.items())
        ],
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
