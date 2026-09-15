from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from advisor.market_daily.control import MarketDailyControlPlane
from advisor.market_daily.engine import MarketDailyEngine
from advisor.market_daily.repository import MarketDailyRepository
from tests.advisor.market_daily.test_engine import Bars, Factors, NOW, SESSIONS, bar, prepared, security


def test_restart_reuses_committed_bars_after_crash_before_item_terminal_state(tmp_path):
    database, control, run_id = prepared(tmp_path, (security("600519", "SH"),))
    fetcher = Bars({"600519": (bar("600519", SESSIONS[0]), bar("600519", SESSIONS[1]))})
    crashed = MarketDailyEngine(
        MarketDailyRepository(database),
        control,
        fetcher,
        Factors(),
        after_commit=lambda _code: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        crashed.execute_run(run_id, SESSIONS, "worker", NOW)
    assert len(fetcher.calls) == 1
    assert control.item(run_id, "600519").status == "running"

    control.recover_expired_work(NOW + timedelta(minutes=2), lease_seconds=30)
    resumed = MarketDailyEngine(MarketDailyRepository(database), control, fetcher, Factors())
    final = resumed.execute_run(run_id, SESSIONS, "worker-two", NOW + timedelta(minutes=2))

    assert final.status == "complete"
    assert len(fetcher.calls) == 1
