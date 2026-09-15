from datetime import date, datetime
from zoneinfo import ZoneInfo

from advisor.db.repository import connect
from advisor.market_daily.contracts import AdjustmentFactor, CanonicalDailyBar, MarketAbsence
from advisor.market_daily.control import MarketDailyControlPlane, RunSecurity
from advisor.market_daily.engine import MarketDailyEngine
from advisor.market_daily.providers.contracts import MarketProviderError
from advisor.market_daily.providers.registry import ProviderChainResult
from advisor.market_daily.repository import MarketDailyRepository


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI)
SESSIONS = (date(2026, 8, 6), date(2026, 8, 7))


def security(code: str, exchange: str) -> RunSecurity:
    return RunSecurity(code, code, exchange, date(2000, 1, 1), None, "active")


def bar(code: str, trade_date: date, *, close: float = 10.0) -> CanonicalDailyBar:
    return CanonicalDailyBar(
        code=code,
        trade_date=trade_date,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=100_000,
        amount=close * 100_000,
        source="eastmoney",
        source_at=NOW,
        fetched_at=NOW,
    )


class Bars:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def fetch_daily_bars(self, code, start, end):
        self.calls.append((code, start, end))
        outcome = self.outcomes[code]
        if isinstance(outcome, BaseException):
            raise outcome
        return ProviderChainResult(tuple(outcome), "eastmoney", ())


class Factors:
    source = "fixture_factors"

    def fetch_adjustment_factors(self, code, start, end):
        return tuple(
            AdjustmentFactor(
                code=code,
                trade_date=trade_date,
                factor=1.0,
                source=self.source,
                source_at=NOW,
                fetched_at=NOW,
                algorithm_version="fixture@1",
            )
            for trade_date in SESSIONS
            if start <= trade_date <= end
        )


class Suspensions:
    def absences_for(self, code, sessions, now):
        return tuple(
            MarketAbsence(
                code=code,
                trade_date=trade_date,
                reason="suspended",
                source="exchange_fixture",
                source_at=now,
                fetched_at=now,
            )
            for trade_date in sessions
        )


def prepared(tmp_path, securities):
    database = tmp_path / "advisor.sqlite"
    control = MarketDailyControlPlane(database)
    request = control.submit_cold_start(date(2026, 8, 7), date(2026, 8, 6), NOW)
    claimed = control.claim_next_request("service", NOW)
    assert claimed is not None
    run = control.create_run(claimed.request_id, "a" * 64, tuple(securities), NOW)
    return database, control, run.run_id


def test_per_security_commits_survive_later_failure_and_run_becomes_partial(tmp_path):
    database, control, run_id = prepared(tmp_path, (security("000001", "SZ"), security("600519", "SH")))
    fetcher = Bars(
        {
            "000001": (bar("000001", SESSIONS[0]), bar("000001", SESSIONS[1])),
            "600519": MarketProviderError("all sources down"),
        }
    )
    engine = MarketDailyEngine(MarketDailyRepository(database), control, fetcher, Factors())

    final = engine.execute_run(run_id, SESSIONS, "worker", NOW)

    assert final.status == "partial"
    assert control.item(run_id, "000001").status == "completed"
    assert control.item(run_id, "600519").status == "source_missing"
    assert len(MarketDailyRepository(database).bars_for("000001", SESSIONS[0], SESSIONS[-1])) == 2


def test_evidenced_suspension_is_coverage_but_unknown_absence_is_source_missing(tmp_path):
    database, control, run_id = prepared(tmp_path, (security("000001", "SZ"),))
    engine = MarketDailyEngine(
        MarketDailyRepository(database), control, Bars({"000001": ()}), Factors(), absences=Suspensions()
    )

    final = engine.execute_run(run_id, SESSIONS, "worker", NOW)

    assert final.status == "complete"
    assert control.item(run_id, "000001").status == "completed"
    assert MarketDailyRepository(database).covered_dates("000001", SESSIONS[0], SESSIONS[-1]) == SESSIONS


def test_explicit_repair_is_required_to_replace_conflicting_canonical_bar(tmp_path):
    database, _control, _run_id = prepared(tmp_path, (security("600519", "SH"),))
    repository = MarketDailyRepository(database)
    original = bar("600519", SESSIONS[0], close=10.0)
    changed = bar("600519", SESSIONS[0], close=11.0)
    repository.insert_bar(original)

    assert repository.insert_bar(changed) == "conflicted"
    assert repository.bar_for("600519", SESSIONS[0]).close == 10.0
    assert repository.repair_bar(changed, "修正经核验的来源差异") == "repaired"
    assert repository.bar_for("600519", SESSIONS[0]).close == 11.0
    connection = connect(repository.database_path)
    try:
        repair = connection.execute(
            """
            SELECT code, trade_date, previous_content_hash, replacement_content_hash, reason
            FROM market_daily_repairs
            """
        ).fetchone()
    finally:
        connection.close()
    assert tuple(repair) == (
        "600519",
        SESSIONS[0].isoformat(),
        original.content_hash,
        changed.content_hash,
        "修正经核验的来源差异",
    )


def test_engine_renews_the_item_lease_at_external_work_boundaries(tmp_path, monkeypatch):
    database, control, run_id = prepared(tmp_path, (security("600519", "SH"),))
    renewals: list[tuple[str, str, str, int]] = []

    def renew_item_claim(run_id, code, owner_id, now, *, lease_seconds):
        renewals.append((run_id, code, owner_id, lease_seconds))
        return True

    monkeypatch.setattr(control, "renew_item_claim", renew_item_claim)
    engine = MarketDailyEngine(
        MarketDailyRepository(database),
        control,
        Bars({"600519": (bar("600519", SESSIONS[0]), bar("600519", SESSIONS[1]))}),
        Factors(),
        clock=lambda: NOW,
        item_lease_seconds=300,
    )

    final = engine.execute_run(run_id, SESSIONS, "worker", NOW)

    assert final.status == "complete"
    assert len(renewals) >= 2
    assert {(entry[1], entry[2], entry[3]) for entry in renewals} == {("600519", "worker", 300)}
