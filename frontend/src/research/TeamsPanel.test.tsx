import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import TeamsPanel from "./TeamsPanel";

type TeamVersion = { team_ref: string; scope: "market" | "security"; title: string; agents: string[] };
type Team = { team_id: string; latest: TeamVersion; history: TeamVersion[]; daily_enabled_ref: string | null };

const agents = [
  { agent_id: "market", agent_ref: "market@2", scope: "security", title: "市场研究", summary: "研究市场数据。" },
  { agent_id: "news", agent_ref: "news@1", scope: "security", title: "资讯研究", summary: "研究公开资讯。" },
];

function json(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

function requestPath(input: RequestInfo | URL): string {
  return typeof input === "string" ? input : input.toString();
}

function mockTeamApi(initial: Team[] = [{
  team_id: "core",
  latest: { team_ref: "core@1", scope: "security", title: "核心团队", agents: ["market@2"] },
  history: [{ team_ref: "core@1", scope: "security", title: "核心团队", agents: ["market@2"] }],
  daily_enabled_ref: null,
}]) {
  let teams = structuredClone(initial);
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = requestPath(input);
    if (path === "/api/research/agents") return json({ agents });
    if (path === "/api/research/teams" && (!init?.method || init.method === "GET")) return json({ teams });
    if (path === "/api/research/teams" && init?.method === "POST") {
      const payload = JSON.parse(String(init.body)) as { team_id: string; scope: "market" | "security"; title: string; agent_ids: string[] };
      const existing = teams.find((item) => item.team_id === payload.team_id);
      const nextAgents = payload.agent_ids.map((id) => agents.find((agent) => agent.agent_id === id)?.agent_ref).sort() as string[];
      if (existing && existing.latest.title === payload.title && JSON.stringify(existing.latest.agents) === JSON.stringify(nextAgents)) {
        return json({ created: false, team: existing.latest });
      }
      const version = existing ? existing.history.length + 1 : 1;
      const created = { team_ref: `${payload.team_id}@${version}`, scope: payload.scope, title: payload.title, agents: nextAgents };
      if (existing) {
        existing.history = [...existing.history, created];
        existing.latest = created;
      } else {
        teams = [...teams, { team_id: payload.team_id, latest: created, history: [created], daily_enabled_ref: null }];
      }
      return json({ created: true, team: created }, { status: 201 });
    }
    if (path.startsWith("/api/research/daily-teams/") && init?.method === "PUT") {
      const segments = path.split("/");
      const teamRef = decodeURIComponent(segments[segments.length - 1] ?? "");
      teams = teams.map((team) => team.team_id === teamRef.split("@", 1)[0]
        ? { ...team, daily_enabled_ref: teamRef }
        : team);
      return json({ daily_teams: teams.flatMap((team) => team.daily_enabled_ref ? [team.daily_enabled_ref] : []), message: "已启用每日 Team" });
    }
    if (path.startsWith("/api/research/daily-teams/") && init?.method === "DELETE") {
      const segments = path.split("/");
      const teamRef = decodeURIComponent(segments[segments.length - 1] ?? "");
      teams = teams.map((team) => team.daily_enabled_ref === teamRef ? { ...team, daily_enabled_ref: null } : team);
      return json({ daily_teams: teams.flatMap((team) => team.daily_enabled_ref ? [team.daily_enabled_ref] : []), message: "已取消每日启用" });
    }
    throw new Error(`Unexpected request: ${path}`);
  });
}

describe("research Teams panel", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  it("creates, revises, expands history, and enables a published Team separately", async () => {
    const fetchMock = mockTeamApi();
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<TeamsPanel />);

    expect(await screen.findByText("核心团队")).toBeInTheDocument();
    expect(screen.getByText("当前未启用每日 Team")).toBeInTheDocument();
    await user.type(screen.getByLabelText("团队 ID"), "value_style");
    await user.type(screen.getByLabelText("中文名称"), "价值风格");
    await user.click(screen.getByLabelText("选择 市场研究"));
    await user.click(screen.getByLabelText("选择 资讯研究"));
    await user.click(screen.getByRole("button", { name: "发布 Team" }));

    expect(await screen.findByText("已发布 value_style@1")).toBeInTheDocument();
    expect(screen.getByText("当前未启用每日 Team")).toBeInTheDocument();
    const valueCard = screen.getByRole("article", { name: "价值风格 value_style@1" });
    expect(within(valueCard).getByText("market@2")).toBeInTheDocument();
    expect(within(valueCard).getByText("news@1")).toBeInTheDocument();
    expect(within(valueCard).getByRole("button", { name: "每日启用" })).toBeInTheDocument();

    await user.click(within(valueCard).getByRole("button", { name: "基于此版本新建" }));
    expect(screen.getByLabelText("团队 ID")).toHaveValue("value_style");
    expect(screen.getByLabelText("团队 ID")).toBeDisabled();
    fireEvent.change(screen.getByLabelText("中文名称"), { target: { value: "价值风格二版" } });
    await user.click(screen.getByRole("button", { name: "发布 Team" }));

    expect(await screen.findByText("已发布 value_style@2")).toBeInTheDocument();
    const revisedCard = screen.getByRole("article", { name: "价值风格二版 value_style@2" });
    await user.click(within(revisedCard).getByRole("button", { name: "查看历史版本" }));
    expect(within(revisedCard).getByText("value_style@1")).toBeInTheDocument();
    await user.click(within(revisedCard).getAllByRole("button", { name: "每日启用" })[1]);
    expect(await screen.findByText("已启用每日 Team")).toBeInTheDocument();
    const afterOldVersionEnabled = screen.getByRole("article", { name: "价值风格二版 value_style@2" });
    await user.click(within(afterOldVersionEnabled).getByRole("button", { name: "每日启用" }));
    expect(await screen.findByText("已启用每日 Team，已替换同一团队的旧版本")).toBeInTheDocument();
    const activeCard = screen.getByRole("article", { name: "价值风格二版 value_style@2" });
    await user.click(within(activeCard).getByRole("button", { name: "取消每日启用" }));
    expect(await screen.findByText("已取消每日启用")).toBeInTheDocument();
    expect(screen.getByText("当前未启用每日 Team")).toBeInTheDocument();

    const publication = fetchMock.mock.calls.find(([input, init]) => requestPath(input) === "/api/research/teams" && init?.method === "POST");
    expect(JSON.parse(String(publication?.[1]?.body))).toEqual({
      team_id: "value_style", scope: "security", title: "价值风格", agent_ids: ["market", "news"],
    });
  });

  it("rejects an empty browser-side selection and drops an unpublished draft on remount", async () => {
    const fetchMock = mockTeamApi();
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    const rendered = render(<TeamsPanel />);
    await screen.findByText("核心团队");
    await user.type(screen.getByLabelText("团队 ID"), "value_style");
    await user.type(screen.getByLabelText("中文名称"), "价值风格");
    await user.click(screen.getByRole("button", { name: "发布 Team" }));

    expect(screen.getByText("请至少选择一个研究 Agent")).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([input, init]) => requestPath(input) === "/api/research/teams" && init?.method === "POST")).toHaveLength(0);
    rendered.unmount();
    render(<TeamsPanel />);
    await screen.findByText("核心团队");
    expect(screen.getByLabelText("团队 ID")).toHaveValue("");
    expect(screen.getByLabelText("中文名称")).toHaveValue("");
  });

  it("shows the idempotent publication result without enabling the Team", async () => {
    const fetchMock = mockTeamApi([{
      team_id: "value_style",
      latest: { team_ref: "value_style@1", scope: "security", title: "价值风格", agents: ["market@2"] },
      history: [{ team_ref: "value_style@1", scope: "security", title: "价值风格", agents: ["market@2"] }],
      daily_enabled_ref: null,
    }]);
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<TeamsPanel />);
    await screen.findByText("价值风格");
    await user.type(screen.getByLabelText("团队 ID"), "value_style");
    await user.type(screen.getByLabelText("中文名称"), "价值风格");
    await user.click(screen.getByLabelText("选择 市场研究"));
    await user.click(screen.getByRole("button", { name: "发布 Team" }));

    expect(await screen.findByText("已确认已发布 value_style@1")).toBeInTheDocument();
    expect(screen.getByText("当前未启用每日 Team")).toBeInTheDocument();
  });

  it("shows a bounded Chinese error returned by the publication API", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      if (path === "/api/research/agents") return json({ agents });
      if (path === "/api/research/teams" && (!init?.method || init.method === "GET")) return json({ teams: [] });
      if (path === "/api/research/teams" && init?.method === "POST") {
        return json({ detail: "研究团队输入无效" }, { status: 400 });
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<TeamsPanel />);
    await screen.findByText("暂无已发布 Team");
    await user.type(screen.getByLabelText("团队 ID"), "value_style");
    await user.type(screen.getByLabelText("中文名称"), "价值风格");
    await user.click(screen.getByLabelText("选择 市场研究"));
    await user.click(screen.getByRole("button", { name: "发布 Team" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("研究团队输入无效");
  });

  it("does not expose a transport error in Team feedback", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = requestPath(input);
      if (path === "/api/research/agents") return json({ agents });
      if (path === "/api/research/teams" && (!init?.method || init.method === "GET")) return json({ teams: [] });
      if (path === "/api/research/teams" && init?.method === "POST") throw new Error("network unavailable");
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<TeamsPanel />);
    await screen.findByText("暂无已发布 Team");
    await user.type(screen.getByLabelText("团队 ID"), "value_style");
    await user.type(screen.getByLabelText("中文名称"), "价值风格");
    await user.click(screen.getByLabelText("选择 市场研究"));
    await user.click(screen.getByRole("button", { name: "发布 Team" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("研究团队发布失败");
    expect(alert).not.toHaveTextContent("network unavailable");
  });
});
