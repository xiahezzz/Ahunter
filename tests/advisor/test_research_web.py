from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
import yaml

from advisor import paths as advisor_paths
from advisor.research.team_publication import TeamPublicationStorageError, TeamPublicationService
from advisor.web.api import create_app


def _research_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    catalog = root / "config" / "research"
    for directory in ("products", "agents", "teams", "pipelines", "execution"):
        (catalog / directory).mkdir(parents=True)
    (catalog / "products" / "identity.yaml").write_text(
        """
product: {id: identity, version: 1}
title: 公司信息
providers: [fixture]
""",
        encoding="utf-8",
    )
    for agent, version, title in (("market", 1, "市场研究"), ("market", 2, "市场研究二版"), ("news", 1, "资讯研究")):
        (catalog / "agents" / f"{agent}_{version}.yaml").write_text(
            f"""
agent: {{id: {agent}, version: {version}}}
title: {title}
instructions: 使用固定样例数据。
required_products: [{{id: identity, version: 1}}]
""",
            encoding="utf-8",
        )
    (catalog / "teams" / "core_v1.yaml").write_text(
        """
team: {id: core, version: 1}
title: 核心研究团队
agents: [{id: market, version: 2}]
""",
        encoding="utf-8",
    )
    (root / "config" / "advisor.yaml").write_text(
        yaml.safe_dump(
            {
                "market": {"primary": "A股"},
                "schedule": {"premarket_time": "08:30", "review_time": "22:30"},
                "storage": {"database": "data/advisor/advisor.sqlite"},
                "data_sources": {"allow_tushare": False, "free_sources": []},
                "research": {
                    "catalog_dir": "config/research",
                    "artifact_dir": "data/advisor/research-artifacts",
                    "default_teams": [],
                    "execution_policy": "codex@1",
                    "max_subjects": 20,
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return root


def test_research_cycle_api_reads_verified_cycle_navigation(tmp_path: Path, monkeypatch):
    reports = tmp_path / "reports"
    cycle = reports / "2026-08-06" / "cycle-1"
    cycle.mkdir(parents=True)
    (cycle / "cycle.json").write_text(json.dumps({"cycle_id": "cycle-1", "status": "passed"}), encoding="utf-8")
    (cycle / "index.md").write_text("# index\n", encoding="utf-8")
    (cycle / "complete.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: reports)

    client = TestClient(create_app(state_dir=tmp_path / "state", db_path=tmp_path / "advisor.sqlite"))
    response = client.get("/api/research/cycles/2026-08-06/cycle-1")

    assert response.status_code == 200
    assert response.json()["cycle_id"] == "cycle-1"


def test_research_team_api_discovers_latest_versions_publishes_and_toggles_daily_use(tmp_path: Path):
    root = _research_workspace(tmp_path)
    client = TestClient(
        create_app(
            state_dir=tmp_path / "state",
            db_path=tmp_path / "advisor.sqlite",
            research_root=root,
            research_config_path=root / "config" / "advisor.yaml",
        )
    )

    agents = client.get("/api/research/agents")
    teams = client.get("/api/research/teams")
    published = client.post(
        "/api/research/teams",
        json={"team_id": "value_style", "scope": "security", "title": "价值风格", "agent_ids": ["market", "news"]},
    )
    repeated = client.post(
        "/api/research/teams",
        json={"team_id": "value_style", "scope": "security", "title": "价值风格", "agent_ids": ["news", "market"]},
    )
    revised = client.post(
        "/api/research/teams",
        json={"team_id": "value_style", "scope": "security", "title": "价值风格二版", "agent_ids": ["news", "market"]},
    )
    enabled = client.put("/api/research/daily-teams/value_style@1")
    replaced = client.put("/api/research/daily-teams/value_style@2")
    current = client.get("/api/research/teams")
    disabled = client.delete("/api/research/daily-teams/value_style@2")

    assert agents.status_code == 200
    assert agents.json()["agents"] == [
        {"agent_id": "market", "agent_ref": "market@2", "scope": "security", "title": "市场研究二版", "summary": "使用固定样例数据。"},
        {"agent_id": "news", "agent_ref": "news@1", "scope": "security", "title": "资讯研究", "summary": "使用固定样例数据。"},
    ]
    assert teams.status_code == 200
    assert teams.json()["teams"] == [{
        "team_id": "core",
        "latest": {"team_ref": "core@1", "scope": "security", "title": "核心研究团队", "agents": ["market@2"]},
        "history": [{"team_ref": "core@1", "scope": "security", "title": "核心研究团队", "agents": ["market@2"]}],
        "daily_enabled_ref": None,
    }]
    assert published.status_code == 201
    assert published.json() == {
        "created": True,
        "team": {"team_ref": "value_style@1", "scope": "security", "title": "价值风格", "agents": ["market@2", "news@1"]},
    }
    assert repeated.status_code == 200
    assert repeated.json()["created"] is False
    assert revised.status_code == 201
    assert revised.json()["team"]["team_ref"] == "value_style@2"
    assert enabled.status_code == 200
    assert enabled.json() == {"daily_teams": ["value_style@1"], "message": "已启用每日 Team"}
    assert replaced.status_code == 200
    assert replaced.json() == {"daily_teams": ["value_style@2"], "message": "已启用每日 Team"}
    value_team = next(item for item in current.json()["teams"] if item["team_id"] == "value_style")
    assert value_team["daily_enabled_ref"] == "value_style@2"
    assert [item["team_ref"] for item in value_team["history"]] == ["value_style@1", "value_style@2"]
    assert disabled.status_code == 200
    assert disabled.json() == {"daily_teams": [], "message": "已取消每日启用"}


def test_research_team_api_returns_bounded_chinese_errors_and_never_runs_research(tmp_path: Path):
    root = _research_workspace(tmp_path)
    client = TestClient(
        create_app(
            state_dir=tmp_path / "state",
            db_path=tmp_path / "advisor.sqlite",
            research_root=root,
            research_config_path=root / "config" / "advisor.yaml",
        )
    )

    malformed = client.post("/api/research/teams", json={"team_id": "value_style"})
    unknown = client.post(
        "/api/research/teams",
        json={"team_id": "value_style", "scope": "security", "title": "价值风格", "agent_ids": ["missing"]},
    )
    first = client.post(
        "/api/research/teams",
        json={"team_id": "value_style", "scope": "security", "title": "价值风格", "agent_ids": ["market", "news"]},
    )
    conflict = client.post(
        "/api/research/teams",
        json={"team_id": "growth_style", "scope": "security", "title": "成长风格", "agent_ids": ["news", "market"]},
    )

    assert malformed.status_code == 400
    assert unknown.status_code == 404
    assert first.status_code == 201
    assert conflict.status_code == 409
    for response in (malformed, unknown, conflict):
        detail = response.json()["detail"]
        assert isinstance(detail, str) and detail and len(detail) <= 80
        assert str(root) not in detail
    assert not (root / "reports").exists()


def test_research_team_publish_api_keeps_the_catalog_unchanged_when_atomic_creation_fails(tmp_path: Path, monkeypatch):
    root = _research_workspace(tmp_path)
    client = TestClient(
        create_app(
            state_dir=tmp_path / "state",
            db_path=tmp_path / "advisor.sqlite",
            research_root=root,
            research_config_path=root / "config" / "advisor.yaml",
        )
    )

    def fail_create(self, path, team):
        raise TeamPublicationStorageError("fixture write failure")

    monkeypatch.setattr(TeamPublicationService, "_create_manifest", fail_create)
    response = client.post(
        "/api/research/teams",
        json={"team_id": "news_style", "scope": "security", "title": "资讯风格", "agent_ids": ["news"]},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "研究团队发布暂不可用"}
    assert not list((root / "config" / "research" / "teams").glob("news_style_v*.yaml"))


def test_research_request_api_only_submits_durable_work_and_exposes_current_queue(tmp_path: Path):
    root = _research_workspace(tmp_path)
    database = tmp_path / "advisor.sqlite"
    client = TestClient(
        create_app(
            state_dir=tmp_path / "state",
            db_path=database,
            research_root=root,
            research_config_path=root / "config" / "advisor.yaml",
        )
    )
    payload = {"team_ref": "core@1", "scope": "security", "code": "600519", "submission_identity": "click-1"}

    first = client.post("/api/research/requests", json=payload)
    retry = client.post("/api/research/requests", json=payload)
    independent = client.post("/api/research/requests", json={**payload, "submission_identity": "click-2"})
    malformed = client.post("/api/research/requests", json={**payload, "scope": "market", "code": None, "submission_identity": "bad"})

    assert first.status_code == retry.status_code == independent.status_code == 202
    assert first.json()["request"]["request_id"] == retry.json()["request"]["request_id"]
    assert first.json()["request"]["request_id"] != independent.json()["request"]["request_id"]
    assert malformed.status_code == 400
    assert not (root / "reports").exists()

    current = client.get("/api/research/requests/current")
    assert current.status_code == 200
    assert current.json()["service"]["state"] == "offline"
    assert [item["status"] for item in current.json()["requests"]] == ["queued", "queued"]

    request_id = first.json()["request"]["request_id"]
    cancelled = client.post(f"/api/research/requests/{request_id}/cancel", json={})
    terminal = client.get(f"/api/research/requests/{request_id}")
    rerun = client.post(f"/api/research/requests/{request_id}/rerun", json={"submission_identity": "rerun-1"})
    records = client.get("/api/research/records", params=[("status", "cancelled")])

    assert cancelled.status_code == 200
    assert cancelled.json()["request"]["status"] == "cancelled"
    assert terminal.status_code == 200
    assert terminal.json()["request"]["request_id"] == request_id
    assert terminal.json()["request"]["status"] == "cancelled"
    assert rerun.status_code == 202
    assert rerun.json()["request"]["rerun_of"] == request_id
    assert records.status_code == 200
    assert records.json()["records"][0]["request_id"] == request_id
