import sqlite3

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
    assert "## Information Flow" in text
    assert "## Capital Flow" in text
    assert "## Analyst Flow" in text


def test_upsert_profile_writes_structured_state(tmp_path):
    connection = sqlite3.connect(tmp_path / "advisor.sqlite")
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
    profile = StockProfile("600519", "贵州茅台", "白酒", "品牌力", [], [], [], [], [])
    upsert_profile(connection, profile)
    row = connection.execute("SELECT code FROM stock_profiles").fetchone()
    assert row[0] == "600519"
