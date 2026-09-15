import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AgentAccessPanel from "./AgentAccessPanel";

const RID_VERSION = "a".repeat(64);
const mxRidScopeContract = {
  kind: "mx_rid_feeds",
  required: ["rids"],
  rids: { type: "integer", minimum: 1, maximum: Number.MAX_SAFE_INTEGER, min_items: 1, max_items: 100 },
};

function json(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" }, ...init });
}

function path(input: RequestInfo | URL): string {
  return typeof input === "string" ? input : input.toString();
}

const products = {
  products: [
    {
      product_id: "identity",
      latest: { product_ref: "identity@1", title: "公司身份", dependencies: [], providers: [{ provider_id: "fixture", display_name: "Fixture" }], supports_feed_scope: false, feed_scope_contract: null },
      history: [{ product_ref: "identity@1", title: "公司身份", dependencies: [], providers: [{ provider_id: "fixture", display_name: "Fixture" }], supports_feed_scope: false, feed_scope_contract: null }],
    },
    {
      product_id: "mx_events",
      latest: { product_ref: "mx_events@2", title: "RID 资讯流", dependencies: ["identity@1"], providers: [{ provider_id: "local-mx", display_name: "本地 MX 已接收资讯" }], supports_feed_scope: true, feed_scope_contract: mxRidScopeContract },
      history: [{ product_ref: "mx_events@2", title: "RID 资讯流", dependencies: ["identity@1"], providers: [{ provider_id: "local-mx", display_name: "本地 MX 已接收资讯" }], supports_feed_scope: true, feed_scope_contract: mxRidScopeContract }],
    },
  ],
};

function agent(
  ref = "social@1",
  access: Array<{ product_ref: string; feed_scope?: { rids: Array<{ rid: number; authorization: "current" | "revoked" }> } }> = [{ product_ref: "identity@1" }],
  instructions = "研究固定资料。",
) {
  return {
    agent_ref: ref,
    scope: "security",
    title: "资讯研究",
    summary: "研究固定资料。",
    instructions,
    data_access: access,
    used_by_team_refs: ref === "social@1" ? ["core@1"] : [],
    unassigned_rids: [111, 222],
    blocked_reasons: [],
    read_only: { query_budget: 11, max_result_rows: 2000, implementation: "declarative" },
  };
}

function directory() {
  return { rid_version: RID_VERSION, agents: [{ agent_id: "social", latest: agent(), history: [agent()] }] };
}

describe("Agent access panel", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));

  it("publishes only explicit data access and RID version", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = path(input);
      if (request === "/api/research/data-catalog") return json(products);
      if (request === "/api/research/agent-access") return json(directory());
      if (request === "/api/research/agents/social%401/access-revisions" && init?.method === "POST") {
        return json({
          created: true,
          agent: agent("social@2", [
            { product_ref: "identity@1" },
            { product_ref: "mx_events@2", feed_scope: { rids: [{ rid: 111, authorization: "current" }] } },
          ]),
          impact: { fixed_team_refs: ["core@1"], revoked_rids: [] },
          message: "已发布新的 Agent 数据访问版本",
        }, { status: 201 });
      }
      throw new Error(`unexpected ${request}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    const reviseTeam = vi.fn();
    render(<AgentAccessPanel onReviseTeam={reviseTeam} />);

    expect(await screen.findByText("资讯研究")).toBeInTheDocument();
    expect(screen.getByText("构建需要这些依赖，Agent 不会自动获得它们的可见权限。")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "修订数据访问" }));
    await user.selectOptions(screen.getByLabelText("添加 Data Product"), "mx_events@2");
    await user.click(screen.getByRole("button", { name: "添加" }));
    await user.click(screen.getByLabelText("RID 111"));
    await user.click(screen.getByRole("button", { name: "发布新的 Agent Access 版本" }));

    expect(await screen.findByText("已发布 social@2；Team 和每日启用未改变")).toBeInTheDocument();
    const request = fetchMock.mock.calls.find(([input, init]) => path(input) === "/api/research/agents/social%401/access-revisions" && init?.method === "POST");
    expect(JSON.parse(String(request?.[1]?.body))).toEqual({
      data_access: [
        { product: "identity@1" },
        { product: "mx_events@2", feed_scope: { rids: [111] } },
      ],
      rid_version: RID_VERSION,
    });
    expect(String(request?.[1]?.body)).not.toContain("instructions");
    expect(String(request?.[1]?.body)).not.toContain("implementation");
    await user.click(screen.getByRole("button", { name: /基于 core@1 修订 Team/ }));
    expect(reviseTeam).toHaveBeenCalledWith("core@1");
  });

  it("keeps a revoked Feed blocked until it is removed", async () => {
    const blocked = { ...agent("social@1", [
      { product_ref: "identity@1" },
      { product_ref: "mx_events@2", feed_scope: { rids: [{ rid: 111, authorization: "revoked" }] } },
    ]), blocked_reasons: ["RID 111 的授权已撤销"] };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const request = path(input);
      if (request === "/api/research/data-catalog") return json(products);
      if (request === "/api/research/agent-access") return json({ rid_version: RID_VERSION, agents: [{ agent_id: "social", latest: blocked, history: [blocked] }] });
      throw new Error(`unexpected ${request}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<AgentAccessPanel onReviseTeam={() => undefined} />);
    expect(await screen.findByText(/RID 111 的授权已撤销/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "修订数据访问" }));
    expect(screen.getByRole("alert")).toHaveTextContent("草稿引用已撤销 RID：111");
    await user.click(screen.getAllByRole("button", { name: "移除" })[1]);
    expect(screen.queryByText(/草稿引用已撤销 RID：111/)).not.toBeInTheDocument();
  });

  it("reviews and publishes only Agent Instructions without changing Teams", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = path(input);
      if (request === "/api/research/data-catalog") return json(products);
      if (request === "/api/research/agent-access") return json(directory());
      if (request === "/api/research/agents/social%401/instruction-revisions" && init?.method === "POST") {
        return json({
          created: true,
          agent: agent("social@2", [{ product_ref: "identity@1" }], "研究固定资料。\n逐条引用证据。"),
          impact: { fixed_team_refs: ["core@1"] },
          message: "已发布新的 Agent Instructions 版本",
        }, { status: 201 });
      }
      throw new Error(`unexpected ${request}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<AgentAccessPanel onReviseTeam={() => undefined} />);

    const reviseInstructions = await screen.findByRole("button", { name: "修订指令" });
    await user.click(reviseInstructions);
    const editor = screen.getByRole("textbox", { name: "Agent Instructions" });
    await user.clear(editor);
    await user.type(editor, "研究固定资料。{enter}逐条引用证据。");

    const unload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);

    await user.click(screen.getByRole("button", { name: "审阅并发布" }));
    expect(screen.getByText(/基础版本 social@1 · 目标版本 social@2/)).toBeInTheDocument();
    expect(screen.getByText(/受影响但不会自动升级的 Team：core@1/)).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Agent Instructions 逐行差异" })).toHaveTextContent("逐条引用证据。");
    expect(fetchMock.mock.calls.some(([input]) => path(input).includes("instruction-revisions"))).toBe(false);

    await user.click(screen.getByRole("button", { name: "确认发布 social@2" }));
    expect(await screen.findByText("已发布 social@2；Team 和每日启用未改变")).toBeInTheDocument();
    const request = fetchMock.mock.calls.find(([input, init]) => path(input) === "/api/research/agents/social%401/instruction-revisions" && init?.method === "POST");
    expect(JSON.parse(String(request?.[1]?.body))).toEqual({ instructions: "研究固定资料。\n逐条引用证据。" });
    expect(String(request?.[1]?.body)).not.toContain("data_access");
  });

  it("restores historical text from the latest base into the next linear version", async () => {
    const first = agent("social@1", [{ product_ref: "identity@1" }], "第一版指令。");
    const second = agent("social@2", [{ product_ref: "identity@1" }], "第二版指令。");
    const historicalDirectory = { rid_version: RID_VERSION, agents: [{ agent_id: "social", latest: second, history: [first, second] }] };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = path(input);
      if (request === "/api/research/data-catalog") return json(products);
      if (request === "/api/research/agent-access") return json(historicalDirectory);
      if (request === "/api/research/agents/social%402/instruction-revisions" && init?.method === "POST") {
        return json({
          created: true,
          agent: agent("social@3", [{ product_ref: "identity@1" }], "第一版指令。"),
          impact: { fixed_team_refs: ["core@1"] },
          message: "已发布新的 Agent Instructions 版本",
        }, { status: 201 });
      }
      throw new Error(`unexpected ${request}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<AgentAccessPanel onReviseTeam={() => undefined} />);

    await screen.findByText("第二版指令。");
    await user.click(screen.getByRole("button", { name: "查看历史版本" }));
    expect(screen.getByText("第一版指令。")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "恢复此版本指令" }));
    expect(screen.getByRole("textbox", { name: "Agent Instructions" })).toHaveValue("第一版指令。");
    expect(screen.getByText(/发布基准仍是最新版 social@2/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "审阅并发布" }));
    await user.click(screen.getByRole("button", { name: "确认发布 social@3" }));

    expect(await screen.findByText("已发布 social@3；Team 和每日启用未改变")).toBeInTheDocument();
    const request = fetchMock.mock.calls.find(([_input, init]) => init?.method === "POST");
    expect(path(request?.[0] as RequestInfo)).toBe("/api/research/agents/social%402/instruction-revisions");
    expect(JSON.parse(String(request?.[1]?.body))).toEqual({ instructions: "第一版指令。" });
  });

  it("does not replace a changed Instructions draft without discard confirmation", async () => {
    const first = agent("social@1", [{ product_ref: "identity@1" }], "第一版指令。");
    const second = agent("social@2", [{ product_ref: "identity@1" }], "第二版指令。");
    const historicalDirectory = { rid_version: RID_VERSION, agents: [{ agent_id: "social", latest: second, history: [first, second] }] };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const request = path(input);
      if (request === "/api/research/data-catalog") return json(products);
      if (request === "/api/research/agent-access") return json(historicalDirectory);
      throw new Error(`unexpected ${request}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const confirmMock = vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(true);
    vi.stubGlobal("confirm", confirmMock);
    const user = userEvent.setup();
    render(<AgentAccessPanel onReviseTeam={() => undefined} />);

    await user.click(await screen.findByRole("button", { name: "修订指令" }));
    const editor = screen.getByRole("textbox", { name: "Agent Instructions" });
    await user.type(editor, "未发布补充");
    await user.click(screen.getByRole("button", { name: "查看历史版本" }));
    await user.click(screen.getByRole("button", { name: "恢复此版本指令" }));
    expect(editor).toHaveValue("第二版指令。未发布补充");

    await user.click(screen.getByRole("button", { name: "恢复此版本指令" }));
    expect(screen.getByRole("textbox", { name: "Agent Instructions" })).toHaveValue("第一版指令。");
    expect(confirmMock).toHaveBeenCalledTimes(2);
  });
});
