import json
from datetime import datetime
from zoneinfo import ZoneInfo

from advisor.market_daily import cli
from advisor.market_daily.control import MarketDailyControlPlane
from advisor.market_daily.providers.registry import SingleProvider


def test_cold_start_cli_only_submits_one_durable_intent_without_building_service(tmp_path, capsys, monkeypatch):
    database = tmp_path / "advisor.sqlite"
    monkeypatch.setattr(cli, "_build_live_service", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()))

    assert cli.main(["--db", str(database), "cold-start"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert cli.main(["--db", str(database), "cold-start"]) == 0
    second = json.loads(capsys.readouterr().out)

    assert first["状态"] == "已提交"
    assert first["请求编号"] == second["请求编号"]
    assert MarketDailyControlPlane(database).pending_request_count() == 1


def test_status_cli_is_read_only_and_uses_clear_chinese_fields(tmp_path, capsys):
    database = tmp_path / "advisor.sqlite"
    control = MarketDailyControlPlane(database)
    now = datetime(2026, 8, 7, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    control.submit_cold_start_intent(now)

    assert cli.main(["--db", str(database), "status"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["服务状态"] == "未运行"
    assert payload["最近运行状态"] == "尚无运行"
    assert control.pending_request_count() == 1


def test_validate_cli_returns_a_read_only_chinese_failure_before_any_complete_run(tmp_path, capsys):
    database = tmp_path / "advisor.sqlite"
    control = MarketDailyControlPlane(database)

    assert cli.main(["--db", str(database), "validate"]) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["状态"] == "未通过"
    assert payload["检查"][0]["项目"] == "运行状态"
    assert control.pending_request_count() == 0


def test_live_service_wires_one_sina_adapter_for_bars_factors_and_benchmarks(tmp_path):
    service = cli._build_live_service(tmp_path / "advisor.sqlite", poll_seconds=15)

    engine = service._cold_start._engine
    sessions = service._cold_start._sessions
    assert isinstance(engine._bars, SingleProvider)
    assert engine._bars.provider.source == "sina"
    assert engine._factors is engine._bars.provider
    assert sessions._primary.source == "sina_sh_index"
    assert sessions._fallback.source == "sina_sz_index"
    assert sessions._primary._provider is engine._bars.provider
    assert sessions._fallback._provider is engine._bars.provider
    assert len(service._cold_start._universe._adapters) == 1
    assert service._cold_start._universe._adapters[0].source == "sina_universe"
    assert service._cold_start._universe._adapters[0]._provider is engine._bars.provider
