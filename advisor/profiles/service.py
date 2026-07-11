import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class StockProfile:
    code: str
    name: str
    industry: str
    thesis: str
    information_flow: list[str]
    capital_flow: list[str]
    analyst_flow: list[str]
    risks: list[str]
    assets: list[str]


def render_profile_markdown(profile: StockProfile) -> str:
    def bullet(values: list[str]) -> str:
        return "\n".join(f"- {value}" for value in values) if values else "- No current entries"

    return "\n".join(
        [
            f"# {profile.code} {profile.name}",
            "",
            f"- Industry: {profile.industry}",
            f"- Thesis: {profile.thesis}",
            "",
            "## Information Flow",
            bullet(profile.information_flow),
            "",
            "## Capital Flow",
            bullet(profile.capital_flow),
            "",
            "## Analyst Flow",
            bullet(profile.analyst_flow),
            "",
            "## Risks",
            bullet(profile.risks),
            "",
            "## Assets",
            bullet(profile.assets),
            "",
        ]
    )


def upsert_profile(connection: sqlite3.Connection, profile: StockProfile) -> None:
    now = datetime.now().isoformat()
    connection.execute(
        """
        INSERT INTO stock_profiles (
          code, thesis_json, information_flow_json, capital_flow_json,
          fundamentals_json, analyst_flow_json, ledger_exposure_json,
          assets_json, updated_at
        ) VALUES (?, ?, ?, ?, '{}', ?, '{}', ?, ?)
        ON CONFLICT(code) DO UPDATE SET
          thesis_json = excluded.thesis_json,
          information_flow_json = excluded.information_flow_json,
          capital_flow_json = excluded.capital_flow_json,
          analyst_flow_json = excluded.analyst_flow_json,
          assets_json = excluded.assets_json,
          updated_at = excluded.updated_at
        """,
        (
            profile.code,
            json.dumps({"name": profile.name, "industry": profile.industry, "thesis": profile.thesis}, ensure_ascii=False),
            json.dumps(profile.information_flow, ensure_ascii=False),
            json.dumps(profile.capital_flow, ensure_ascii=False),
            json.dumps(profile.analyst_flow, ensure_ascii=False),
            json.dumps(profile.assets, ensure_ascii=False),
            now,
        ),
    )
    connection.commit()
