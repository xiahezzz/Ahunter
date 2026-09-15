from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from advisor.db.repository import connect
from advisor.market_daily.adjustments import ALGORITHM_VERSION
from advisor.market_daily.catch_up import CatchUpWorkflow
from advisor.market_daily.cold_start import ColdStartWorkflow
from advisor.market_daily.contracts import AdjustmentFactor, CanonicalDailyBar, MarketAbsence, MarketSecurity
from advisor.market_daily.control import MarketDailyControlPlane, RunSecurity
from advisor.market_daily.engine import MarketDailyEngine
from advisor.market_daily.providers.contracts import MarketProviderError
from advisor.market_daily.providers.registry import ProviderChain
from advisor.market_daily.repository import MarketDailyRepository
from advisor.market_daily.service import MarketDailyService
from advisor.market_daily.sessions import ObservedSessionService
from advisor.market_daily.universe import UniverseSnapshot
from advisor.research.contracts import ResearchBoundary, ResearchSubject, VersionRef
from advisor.research.data_products.engine import ProductRequest
from advisor.research.providers.local import LocalMarketProvider
from advisor.web.api import create_app


SHANGHAI = ZoneInfo("Asia/Shanghai")
COLD_NOW = datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI)
NEXT_NOW = datetime(2026, 8, 10, 21, 0, tzinfo=SHANGHAI)
HOLIDAY_NOW = datetime(2026, 8, 11, 21, 0, tzinfo=SHANGHAI)
HISTORY = date(2021, 8, 9)
SESSION_1 = date(2026, 8, 6)
SESSION_2 = date(2026, 8, 7)
NEXT_SESSION = date(2026, 8, 10)


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FixtureSessions:
    def __init__(self, dates: set[date]) -> None:
        self.source = "eastmoney"  # overwritten by the fallback instance below
        self.dates = dates
        self.calls: list[tuple[date, date]] = []

    def observe_sessions(self, start: date, end: date) -> tuple[date, ...]:
        self.calls.append((start, end))
        return tuple(sorted(item for item in self.dates if start <= item <= end))


class FixtureBars:
    def __init__(self, source: str, clock: MutableClock, rows: dict[str, set[date]]) -> None:
        self.source = source
        self.endpoint = f"fixture://{source}"
        self._clock = clock
        self.rows = rows
        self.failed_codes: set[str] = set()
        self.calls: list[tuple[str, date, date]] = []

    def fetch_daily_bars(self, code: str, start: date, end: date) -> tuple[CanonicalDailyBar, ...]:
        self.calls.append((code, start, end))
        if code in self.failed_codes:
            raise MarketProviderError(f"{self.source} fixture failure")
        dates = tuple(sorted(item for item in self.rows.get(code, set()) if start <= item <= end))
        if not dates:
            raise MarketProviderError(f"{self.source} fixture has no rows")
        now = self._clock()
        return tuple(
            CanonicalDailyBar(
                code=code,
                trade_date=trade_date,
                open=10.0,
                high=11.0,
                low=9.0,
                close=10.0 + (trade_date.day / 100),
                volume=1_000,
                amount=10_000.0,
                source=self.source,
                source_at=now,
                fetched_at=now,
            )
            for trade_date in dates
        )


class FixtureFactors:
    source = "fixture_factors"

    def __init__(self, clock: MutableClock) -> None:
        self._clock = clock
        self.calls: list[tuple[str, date, date]] = []

    def fetch_adjustment_factors(self, code: str, start: date, end: date) -> tuple[AdjustmentFactor, ...]:
        self.calls.append((code, start, end))
        now = self._clock()
        # The fake uses only the dates returned by the bar provider.  All
        # fixture session windows are intentionally contiguous at the call boundary.
        dates = tuple(sorted({HISTORY, SESSION_1, SESSION_2, NEXT_SESSION}.intersection(
            {item for item in (HISTORY, SESSION_1, SESSION_2, NEXT_SESSION) if start <= item <= end}
        )))
        return tuple(
            AdjustmentFactor(code, item, 1.0, self.source, now, now, ALGORITHM_VERSION)
            for item in dates
        )


class FixtureAbsences:
    def __init__(self, clock: MutableClock) -> None:
        self._clock = clock
        self.dates: dict[str, set[date]] = {"000001": {SESSION_2}}

    def absences_for(self, code: str, sessions: tuple[date, ...], now: datetime) -> tuple[MarketAbsence, ...]:
        return tuple(
            MarketAbsence(code, item, "suspended", "fixture_absence", self._clock(), self._clock())
            for item in sessions
            if item in self.dates.get(code, set())
        )


class FixtureUniverse:
    def __init__(self, repository: MarketDailyRepository, now: datetime) -> None:
        self._repository = repository
        self.snapshot = UniverseSnapshot(
            (
                MarketSecurity("600001", "普通股", "SH", date(2000, 1, 1), None, "active", False, "fixture_exchange", now, now),
                MarketSecurity("000001", "ST 停牌股", "SZ", date(2000, 1, 1), None, "suspended", True, "fixture_exchange", now, now),
                MarketSecurity("300001", "新上市", "SZ", SESSION_1, None, "active", False, "fixture_exchange", now, now),
                MarketSecurity("600002", "窗口内退市", "SH", date(2000, 1, 1), date(2024, 1, 1), "delisted", False, "fixture_exchange", now, now),
                MarketSecurity("600003", "待恢复来源", "SH", date(2000, 1, 1), None, "active", False, "fixture_exchange", now, now),
            )
        )

    def refresh(self) -> UniverseSnapshot:
        for security in self.snapshot.securities:
            self._repository.upsert_security(security)
        return self.snapshot


def _components(database: Path):
    repository = MarketDailyRepository(database)
    control = MarketDailyControlPlane(database)
    clock = MutableClock(COLD_NOW)
    observed_dates = {HISTORY, SESSION_1, SESSION_2}
    primary_sessions = FixtureSessions(observed_dates)
    primary_sessions.source = "eastmoney"
    fallback_sessions = FixtureSessions(observed_dates)
    fallback_sessions.source = "tdx"
    sessions = ObservedSessionService(repository, primary_sessions, fallback_sessions)
    universe = FixtureUniverse(repository, COLD_NOW)
    primary = FixtureBars(
        "eastmoney",
        clock,
        {
            "600001": {HISTORY, SESSION_1, SESSION_2},
            "000001": {HISTORY, SESSION_1},
            "300001": {SESSION_1, SESSION_2},
            "600002": {HISTORY},
            "600003": {HISTORY, SESSION_1, SESSION_2},
        },
    )
    fallback = FixtureBars("tdx", clock, {"600001": {HISTORY, SESSION_1, SESSION_2}})
    primary.failed_codes.update({"600001", "600003"})
    fallback.failed_codes.add("600003")
    engine = MarketDailyEngine(
        repository,
        control,
        ProviderChain(primary, fallback),
        FixtureFactors(clock),
        absences=FixtureAbsences(clock),
    )
    cold = ColdStartWorkflow(control, sessions, universe, engine)
    catch = CatchUpWorkflow(repository, control, sessions, universe, engine)
    service = MarketDailyService(
        control,
        cold,
        catch,
        owner_id="fixture-service",
        clock=clock,
        sleep=lambda _seconds: None,
    )
    return repository, control, clock, observed_dates, primary, fallback, cold, service


def test_small_market_service_recovers_partial_cold_start_then_catches_up_and_serves_research_and_web(tmp_path: Path):
    database = tmp_path / "advisor.sqlite"
    repository, control, clock, observed_dates, primary, fallback, cold, service = _components(database)

    intent = cold.submit(COLD_NOW)
    first = service.tick()
    partial = control.latest_run(run_type="cold_start")
    assert first.action == "executed_cold_start"
    assert partial is not None and partial.status == "partial"
    assert control.item(partial.run_id, "600003").status == "source_missing"
    assert control.item(partial.run_id, "600001").selected_source == "tdx"
    assert repository.covered_dates("000001", HISTORY, SESSION_2) == (HISTORY, SESSION_1, SESSION_2)

    primary.failed_codes.remove("600003")
    clock.now = datetime(2026, 8, 8, 21, 0, tzinfo=SHANGHAI)
    second = service.tick()
    complete = control.run(partial.run_id)
    assert second.action == "resumed_cold_start"
    assert complete.status == "complete"
    assert control.run_for_request(intent.request_id).run_id == complete.run_id
    assert [call[0] for call in primary.calls].count("600003") == 3
    assert [call[0] for call in primary.calls].count("600001") == 2

    observed_dates.add(NEXT_SESSION)
    for rows in (primary.rows, fallback.rows):
        for code in ("600001", "000001", "300001", "600003"):
            rows.setdefault(code, set()).add(NEXT_SESSION)
    clock.now = NEXT_NOW
    caught_up = service.tick()
    daily = control.latest_run(run_type="catch_up")
    assert caught_up.action == "executed_catch_up"
    assert daily is not None and daily.status == "complete"
    assert repository.covered_dates("600001", HISTORY, NEXT_SESSION)[-1] == NEXT_SESSION

    clock.now = HOLIDAY_NOW
    no_op = service.tick()
    assert no_op.action == "up_to_date"
    assert control.pending_request_count() == 0
    connection = connect(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM market_daily_runs").fetchone()[0] == 2
    finally:
        connection.close()

    boundary = datetime(2026, 8, 11, 8, 30, tzinfo=SHANGHAI)
    observation = LocalMarketProvider(database).fetch(
        ProductRequest(
            VersionRef.parse("market_daily_bars@1"),
            ResearchSubject(code="600001"),
            ResearchBoundary(as_of=boundary),
        )
    )
    assert observation.quality_status == "passed"
    assert observation.payload["raw_rows"][-1]["trade_date"] == NEXT_SESSION.isoformat()
    assert len(observation.payload["research_price_series"]["factor_set_hash"]) == 64

    client = TestClient(create_app(tmp_path, db_path=database))
    status = client.get("/api/market-daily/status")
    detail = client.get(f"/api/market-daily/runs/{daily.run_id}")
    assert status.status_code == 200
    assert status.json()["state"] == "complete"
    assert status.json()["latest_observed_session"] == NEXT_SESSION.isoformat()
    assert detail.json()["completed_items"] == detail.json()["total_items"]


def test_conflicted_bar_never_overwrites_the_first_committed_fact(tmp_path: Path):
    database = tmp_path / "advisor.sqlite"
    repository = MarketDailyRepository(database)
    control = MarketDailyControlPlane(database)
    now = COLD_NOW
    security = RunSecurity("600004", "冲突证券", "SH", HISTORY, None, "active")
    request = control.submit_cold_start(SESSION_2, HISTORY, now)
    claimed = control.claim_next_request("fixture", now)
    assert claimed is not None
    run = control.create_run(request.request_id, "a" * 64, (security,), now)
    original = CanonicalDailyBar("600004", SESSION_2, 10, 11, 9, 10, 1_000, 10_000, "first", now, now)
    repository.insert_bar(original)
    connection = connect(database)
    try:
        connection.execute(
            "UPDATE market_daily SET quality_status = 'failed' WHERE code = '600004' AND trade_date = ?",
            (SESSION_2.isoformat(),),
        )
        connection.commit()
    finally:
        connection.close()
    clock = MutableClock(now)
    provider = FixtureBars("eastmoney", clock, {"600004": {SESSION_2}})
    engine = MarketDailyEngine(repository, control, ProviderChain(provider, FixtureBars("tdx", clock, {})), FixtureFactors(clock))

    final = engine.execute_run(run.run_id, (SESSION_2,), "fixture", now)

    assert final.status == "partial"
    assert control.item(run.run_id, "600004").status == "conflicted"
    connection = connect(database)
    try:
        assert connection.execute(
            "SELECT content_hash FROM market_daily WHERE code = '600004' AND trade_date = ?",
            (SESSION_2.isoformat(),),
        ).fetchone()[0] == original.content_hash
    finally:
        connection.close()
