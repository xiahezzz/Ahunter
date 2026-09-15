import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

const VERSION = "a".repeat(64);

function json(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" }, ...init });
}

function path(input: RequestInfo | URL): string {
  return typeof input === "string" ? input : input.toString();
}

const listener = {
  service_id: "mx-listener", status: "待命", details: "MX Listener 未加载", launchagent_loaded: false,
  liveness: "offline", readiness: "stopping", health: "failed", reason_code: "not_started",
  connected_at: null, last_frame_at: null, last_accepted_event_at: null, lease_expires_at: null,
  rid_count: 0, collection_enabled: false, rid_config_valid: true,
};

const catalog = { products: [{ product_id: "identity", latest: { product_ref: "identity@1", title: "公司身份", dependencies: [], providers: [{ provider_id: "fixture", display_name: "Fixture" }], supports_feed_scope: false, feed_scope_contract: null }, history: [{ product_ref: "identity@1", title: "公司身份", dependencies: [], providers: [{ provider_id: "fixture", display_name: "Fixture" }], supports_feed_scope: false, feed_scope_contract: null }] }] };
const access = { rid_version: VERSION, agents: [{ agent_id: "social", latest: { agent_ref: "social@1", scope: "security", title: "资讯研究", summary: "研究固定资料。", instructions: "研究固定资料。", data_access: [{ product_ref: "identity@1" }], used_by_team_refs: [], unassigned_rids: [], blocked_reasons: [], read_only: { query_budget: 1, max_result_rows: 1, implementation: "declarative" } }, history: [{ agent_ref: "social@1", scope: "security", title: "资讯研究", summary: "研究固定资料。", instructions: "研究固定资料。", data_access: [{ product_ref: "identity@1" }], used_by_team_refs: [], unassigned_rids: [], blocked_reasons: [], read_only: { query_budget: 1, max_result_rows: 1, implementation: "declarative" } }] }] };
const emptyResearchCurrent = { service: { state: "offline", heartbeat_at: null, active_request_id: null, queued_count: 0, reason_code: null }, requests: [] };
const emptyResearchRecords = { records: [], limit: 20, offset: 0 };

describe("top-level navigation", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/mx");
    vi.stubGlobal("fetch", vi.fn());
    vi.stubGlobal("confirm", vi.fn(() => true));
  });

  it("opens MX directly and switches to research without loading overview state", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = path(input);
      if (request === "/api/mx/listener/status") return json(listener);
      if (request === "/api/mx/rids") return json({ rids: [], version: VERSION, collection_enabled: false });
      if (request.startsWith("/api/mx/events?")) return json({ events: [], next_cursor: null, limit: 50 });
      if (request === "/api/research/data-catalog") return json(catalog);
      if (request === "/api/research/agent-access") return json(access);
      if (request === "/api/research/agents") return json({ agents: [{ agent_id: "social", agent_ref: "social@1", scope: "security", title: "资讯研究", summary: "研究固定资料。" }] });
      if (request === "/api/research/teams" && (!init?.method || init.method === "GET")) return json({ teams: [] });
      if (request === "/api/research/requests/current") return json(emptyResearchCurrent);
      if (request.startsWith("/api/research/records?")) return json(emptyResearchRecords);
      throw new Error(`unexpected ${request}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByText("MX 历史资讯")).toBeInTheDocument();
    expect(fetchMock.mock.calls.map(([input]) => path(input))).not.toContain("/api/current-state");
    await user.click(screen.getByRole("link", { name: "研究配置" }));
    expect(await screen.findByText("研究数据源")).toBeInTheDocument();
    expect(await screen.findByText("研究团队")).toBeInTheDocument();
    expect(window.location.pathname).toBe("/research");
    expect(fetchMock.mock.calls.map(([input]) => path(input))).not.toContain("/api/current-state");
  });

  it("does not leave Research Configuration with an unpublished Instructions draft without confirmation", async () => {
    window.history.replaceState({}, "", "/research");
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = path(input);
      if (request === "/api/research/data-catalog") return json(catalog);
      if (request === "/api/research/agent-access") return json(access);
      if (request === "/api/research/agents") return json({ agents: [{ agent_id: "social", agent_ref: "social@1", scope: "security", title: "资讯研究", summary: "研究固定资料。" }] });
      if (request === "/api/research/teams" && (!init?.method || init.method === "GET")) return json({ teams: [] });
      if (request === "/api/research/requests/current") return json(emptyResearchCurrent);
      if (request.startsWith("/api/research/records?")) return json(emptyResearchRecords);
      if (request === "/api/mx/listener/status") return json(listener);
      if (request === "/api/mx/rids") return json({ rids: [], version: VERSION, collection_enabled: false });
      if (request.startsWith("/api/mx/events?")) return json({ events: [], next_cursor: null, limit: 50 });
      throw new Error(`unexpected ${request}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const confirmMock = vi.fn()
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true);
    vi.stubGlobal("confirm", confirmMock);
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "修订指令" }));
    const editor = screen.getByRole("textbox", { name: "Agent Instructions" });
    await user.type(editor, "新增要求");
    await user.click(screen.getByRole("link", { name: "MX 监听" }));
    expect(window.location.pathname).toBe("/research");
    expect(screen.getByRole("textbox", { name: "Agent Instructions" })).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "MX 监听" }));
    expect(window.location.pathname).toBe("/mx");
    expect(await screen.findByText("MX 历史资讯")).toBeInTheDocument();
    expect(confirmMock).toHaveBeenCalledTimes(2);
  });
});
