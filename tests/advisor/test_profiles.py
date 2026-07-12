import json
import sqlite3
from datetime import datetime

from advisor.profiles.service import StockProfile, render_profile_markdown, upsert_profile


def test_render_profile_contains_required_dimensions():
    profile = StockProfile(
        code="600519",
        name="贵州茅台",
        industry="白酒",
        thesis="高端白酒品牌力和现金流",
        information_flow=["MX 事件提到放量"],
        capital_flow=["成交额放大"],
        analyst_flow=["Market analyst: trend positive"],
        risks=["估值偏高"],
        assets=["reports/2026-07-11/assets/600519-kline.png"],
    )
    text = render_profile_markdown(profile)
    assert "# 600519 贵州茅台" in text
    assert "- Industry: 白酒" in text
    assert "- Thesis: 高端白酒品牌力和现金流" in text
    assert "## Information Flow" in text
    assert "- MX 事件提到放量" in text
    assert "## Capital Flow" in text
    assert "- 成交额放大" in text
    assert "## Analyst Flow" in text
    assert "- Market analyst: trend positive" in text
    assert "## Risks" in text
    assert "- 估值偏高" in text
    assert "## Assets" in text
    assert "- reports/2026-07-11/assets/600519-kline.png" in text


def test_render_profile_uses_empty_list_fallback():
    profile = StockProfile("600519", "贵州茅台", "白酒", "品牌力", [], [], [], [], [])

    text = render_profile_markdown(profile)

    assert text.count("- No current entries") == 5


def test_upsert_profile_writes_and_replaces_structured_state(tmp_path):
    database_path = tmp_path / "advisor.sqlite"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE stock_profiles (
          code TEXT PRIMARY KEY,
          thesis_json TEXT NOT NULL,
          information_flow_json TEXT NOT NULL,
          capital_flow_json TEXT NOT NULL,
          fundamentals_json TEXT NOT NULL,
          analyst_flow_json TEXT NOT NULL,
          ledger_exposure_json TEXT NOT NULL,
          assets_json TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )
    first_profile = StockProfile(
        "600519",
        "贵州茅台",
        "白酒",
        "品牌力",
        ["MX 初始"],
        ["成交额初始"],
        ["Analyst 初始"],
        ["估值初始"],
        ["assets/initial.png"],
    )
    upsert_profile(connection, first_profile)

    second_profile = StockProfile(
        "600519",
        "贵州茅台更新",
        "消费白酒",
        "品牌力更新",
        ["MX 更新"],
        ["成交额更新"],
        ["Analyst 更新"],
        ["估值更新"],
        ["assets/updated.png"],
    )
    upsert_profile(connection, second_profile)

    durable_connection = sqlite3.connect(database_path)
    row = durable_connection.execute(
        """
        SELECT code, thesis_json, information_flow_json, capital_flow_json,
               fundamentals_json, analyst_flow_json, ledger_exposure_json,
               assets_json, updated_at
        FROM stock_profiles
        """
    ).fetchone()

    assert row[0] == "600519"
    assert json.loads(row[1]) == {
        "name": "贵州茅台更新",
        "industry": "消费白酒",
        "thesis": "品牌力更新",
    }
    assert json.loads(row[2]) == ["MX 更新"]
    assert json.loads(row[3]) == ["成交额更新"]
    assert json.loads(row[4]) == {}
    assert json.loads(row[5]) == ["Analyst 更新"]
    assert json.loads(row[6]) == {}
    assert json.loads(row[7]) == ["assets/updated.png"]
    assert row[8]
    datetime.fromisoformat(row[8])

    assert durable_connection.execute("SELECT COUNT(*) FROM stock_profiles").fetchone()[0] == 1
    durable_connection.close()
    connection.close()
