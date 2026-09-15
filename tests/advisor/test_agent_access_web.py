from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
import yaml

from advisor.mx.rid_authorization import RidAuthorizationStore
from advisor.web.api import create_app


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "workspace"
    catalog = root / "config" / "research"
    for directory in ("products", "agents", "teams", "pipelines", "execution"):
        (catalog / directory).mkdir(parents=True)
    (catalog / "products" / "identity.yaml").write_text(
        """
product: identity@1
title: 公司身份
providers: [fixture]
""",
        encoding="utf-8",
    )
    (catalog / "products" / "mx.yaml").write_text(
        """
product: mx_events@2
title: RID 资讯流
providers: [local-mx]
dependencies: [identity@1]
feed_scope: {kind: mx_rid_feeds, required: [rids]}
""",
        encoding="utf-8",
    )
    (catalog / "agents" / "social.yaml").write_text(
        """
agent: social@1
title: 资讯研究
instructions: 使用固定资料，不能访问未声明的数据。
required_products: [identity@1]
query_budget: 11
""",
        encoding="utf-8",
    )
    (catalog / "teams" / "core.yaml").write_text(
        """
team: core@1
title: 核心 Team
agents: [social@1]
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
    allowed = root / "config" / "allowed-rids.yaml"
    allowed.write_text("allowed_rids: [111, 222]\n", encoding="utf-8")
    return root, allowed


def _client(tmp_path: Path) -> tuple[TestClient, Path, Path]:
    root, allowed = _workspace(tmp_path)
    return (
        TestClient(
            create_app(
                state_dir=tmp_path / "state",
                db_path=tmp_path / "advisor.sqlite",
                research_root=root,
                research_config_path=root / "config" / "advisor.yaml",
                allowed_rids_path=allowed,
            )
        ),
        root,
        allowed,
    )


def test_data_catalog_and_agent_access_are_read_only_and_do_not_expose_provider_connections(tmp_path: Path):
    client, root, allowed = _client(tmp_path)

    catalog = client.get("/api/research/data-catalog")
    access = client.get("/api/research/agent-access")

    assert catalog.status_code == 200
    mx = next(item for item in catalog.json()["products"] if item["product_id"] == "mx_events")["latest"]
    assert mx == {
        "product_ref": "mx_events@2",
        "title": "RID 资讯流",
        "dependencies": ["identity@1"],
        "providers": [{"provider_id": "local-mx", "display_name": "本地 MX 资讯库"}],
        "supports_feed_scope": True,
        "feed_scope_contract": {"kind": "mx_rid_feeds", "required": ["rids"]},
    }
    assert "endpoint" not in catalog.text
    assert "sqlite" not in catalog.text
    assert access.status_code == 200
    assert access.json()["rid_version"] == RidAuthorizationStore(allowed).read().version
    social = access.json()["agents"][0]["latest"]
    assert social["agent_ref"] == "social@1"
    assert social["instructions"] == "使用固定资料，不能访问未声明的数据。"
    assert social["data_access"] == [{"product_ref": "identity@1"}]
    assert social["unassigned_rids"] == [111, 222]
    assert social["used_by_team_refs"] == ["core@1"]
    assert not (root / "reports").exists()
    assert not list(root.rglob("*.sqlite"))


def test_instruction_revision_api_is_strict_idempotent_and_leaves_team_and_access_unchanged(tmp_path: Path):
    client, root, allowed = _client(tmp_path)
    original = (root / "config" / "research" / "agents" / "social.yaml").read_bytes()
    payload = {"instructions": "使用固定资料，并逐条引用证据。"}

    created = client.post("/api/research/agents/social@1/instruction-revisions", json=payload)
    repeated = client.post("/api/research/agents/social@1/instruction-revisions", json=payload)
    directory = client.get("/api/research/agent-access")
    teams = client.get("/api/research/teams")

    assert created.status_code == 201
    assert repeated.status_code == 200
    response = created.json()
    assert response["agent"]["agent_ref"] == "social@2"
    assert response["agent"]["instructions"] == payload["instructions"]
    assert response["agent"]["data_access"] == [{"product_ref": "identity@1"}]
    assert response["impact"] == {"fixed_team_refs": ["core@1"]}
    assert teams.json()["teams"][0]["latest"]["agents"] == ["social@1"]
    assert (root / "config" / "research" / "agents" / "social.yaml").read_bytes() == original
    assert (root / "config" / "research" / "agents" / "social_v2.yaml").is_file()
    versions = directory.json()["agents"][0]
    assert versions["latest"]["agent_ref"] == "social@2"
    assert [(item["agent_ref"], item["instructions"]) for item in versions["history"]] == [
        ("social@1", "使用固定资料，不能访问未声明的数据。"),
        ("social@2", payload["instructions"]),
    ]
    assert RidAuthorizationStore(allowed).read().rids == (111, 222)
    assert not (root / "reports").exists()
    assert not list(root.rglob("*.sqlite"))


def test_instruction_revision_api_rejects_unsafe_unknown_and_stale_input_without_side_effects(tmp_path: Path):
    client, root, _allowed = _client(tmp_path)
    malformed = client.post(
        "/api/research/agents/social@1/instruction-revisions",
        json={"instructions": "有效指令。", "data_access": []},
    )
    blank = client.post(
        "/api/research/agents/social@1/instruction-revisions",
        json={"instructions": "  \n"},
    )
    unknown = client.post(
        "/api/research/agents/missing@1/instruction-revisions",
        json={"instructions": "有效指令。"},
    )
    cross_origin = client.post(
        "/api/research/agents/social@1/instruction-revisions",
        json={"instructions": "有效指令。"},
        headers={"Origin": "https://example.invalid"},
    )
    spoofed_host = client.post(
        "/api/research/agents/social@1/instruction-revisions",
        json={"instructions": "有效指令。"},
        headers={"Host": "example.invalid", "Origin": "http://example.invalid"},
    )
    remote_client = TestClient(client.app, client=("203.0.113.10", 50_000))
    remote_peer = remote_client.post(
        "/api/research/agents/social@1/instruction-revisions",
        json={"instructions": "有效指令。"},
        headers={"Host": "127.0.0.1"},
    )
    created = client.post(
        "/api/research/agents/social@1/instruction-revisions",
        json={"instructions": "第二版指令。"},
    )
    stale = client.post(
        "/api/research/agents/social@1/instruction-revisions",
        json={"instructions": "来自过期页面的指令。"},
    )

    assert [
        item.status_code
        for item in (malformed, blank, unknown, cross_origin, spoofed_host, remote_peer, created, stale)
    ] == [
        400,
        400,
        404,
        400,
        400,
        400,
        201,
        409,
    ]
    assert sorted(path.name for path in (root / "config" / "research" / "agents").glob("*.yaml")) == [
        "social.yaml",
        "social_v2.yaml",
    ]
    assert not (root / "reports").exists()
    assert not list(root.rglob("*.sqlite"))


def test_access_revision_api_is_strict_idempotent_and_leaves_team_selection_unchanged(tmp_path: Path):
    client, root, allowed = _client(tmp_path)
    version = client.get("/api/research/agent-access").json()["rid_version"]
    payload = {
        "data_access": [
            {"product": "identity@1"},
            {"product": "mx_events@2", "feed_scope": {"rids": [111]}},
        ],
        "rid_version": version,
    }

    created = client.post("/api/research/agents/social@1/access-revisions", json=payload)
    repeated = client.post("/api/research/agents/social@1/access-revisions", json=payload)
    teams = client.get("/api/research/teams")

    assert created.status_code == 201
    assert repeated.status_code == 200
    response = created.json()
    assert response["agent"]["agent_ref"] == "social@2"
    assert response["agent"]["data_access"] == [
        {"product_ref": "identity@1"},
        {"product_ref": "mx_events@2", "feed_scope": {"rids": [{"rid": 111, "authorization": "current"}]}},
    ]
    assert response["impact"] == {"fixed_team_refs": ["core@1"], "revoked_rids": []}
    assert teams.json()["teams"][0]["latest"]["agents"] == ["social@1"]
    assert (root / "config" / "research" / "agents" / "social.yaml").is_file()
    assert (root / "config" / "research" / "agents" / "social_v2.yaml").is_file()
    assert "required_products" not in (root / "config" / "research" / "agents" / "social_v2.yaml").read_text(encoding="utf-8")
    assert RidAuthorizationStore(allowed).read().rids == (111, 222)
    assert not (root / "reports").exists()


def test_access_revision_api_rejects_stale_or_unsafe_input_without_starting_research(tmp_path: Path):
    client, root, allowed = _client(tmp_path)
    version = client.get("/api/research/agent-access").json()["rid_version"]
    malformed = client.post(
        "/api/research/agents/social@1/access-revisions",
        json={"data_access": [{"product": "mx_events@2", "provider": "unsafe"}], "rid_version": version},
    )
    unknown = client.post(
        "/api/research/agents/missing@1/access-revisions",
        json={"data_access": [{"product": "identity@1"}], "rid_version": version},
    )
    cross_origin = client.post(
        "/api/research/agents/social@1/access-revisions",
        json={"data_access": [{"product": "identity@1"}], "rid_version": version},
        headers={"Origin": "https://example.invalid"},
    )
    allowed.write_text("allowed_rids: [222]\n", encoding="utf-8")
    stale = client.post(
        "/api/research/agents/social@1/access-revisions",
        json={"data_access": [{"product": "identity@1"}], "rid_version": version},
    )

    assert [item.status_code for item in (malformed, unknown, cross_origin, stale)] == [400, 404, 400, 409]
    assert not list((root / "config" / "research" / "agents").glob("social_v*.yaml"))
    assert not (root / "reports").exists()
    assert not list(root.rglob("*.sqlite"))
