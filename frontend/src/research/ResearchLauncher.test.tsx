import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ResearchLauncher from "./ResearchLauncher";

const REQUEST_ID = `request-${"a".repeat(32)}`;
const NOW = "2026-08-06T08:30:00+00:00";

function json(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" }, ...init });
}

function request(scope: "market" | "security", teamRef: string, code: string | null) {
  return {
    request_id: REQUEST_ID,
    team_ref: teamRef,
    scope,
    subject: { scope, code, name: null },
    origin: "web",
    requested_at: NOW,
    accepted_at: NOW,
    boundary_at: null,
    status: "queued",
    phase: "queued",
    agents_completed: 0,
    agents_total: 0,
    decision_stage: null,
    reason_code: null,
    published_at: null,
    rerun_of: null,
    last_updated_at: NOW,
    can_cancel: true,
    can_rerun: false,
  };
}

const teams = {
  teams: [
    {
      team_id: "market_overview",
      latest: { team_ref: "market_overview@1", scope: "market", title: "市场概览", agents: ["market_breadth@1"] },
      history: [{ team_ref: "market_overview@1", scope: "market", title: "市场概览", agents: ["market_breadth@1"] }],
      daily_enabled_ref: null,
    },
    {
      team_id: "normal",
      latest: { team_ref: "normal@1", scope: "security", title: "证券研究", agents: ["market@1"] },
      history: [{ team_ref: "normal@1", scope: "security", title: "证券研究", agents: ["market@1"] }],
      daily_enabled_ref: null,
    },
  ],
};

function path(input: RequestInfo | URL): string {
  return typeof input === "string" ? input : input.toString();
}

describe("Research launcher", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));

  it("submits a code-free Market Request immediately and switches to a validated Security shape", async () => {
    const submitted = vi.fn();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (path(input) === "/api/research/teams") return json(teams);
      if (path(input) === "/api/research/requests" && init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        return json({ request: request(body.scope, body.team_ref, body.code) }, { status: 202 });
      }
      throw new Error(`unexpected request ${path(input)}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<ResearchLauncher onSubmitted={submitted} />);

    expect(await screen.findByText(/沪深 A 股整体/)).toBeInTheDocument();
    expect(screen.queryByLabelText("证券代码")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "提交研究请求" }));

    expect(await screen.findByText(new RegExp(REQUEST_ID))).toBeInTheDocument();
    const marketCall = fetchMock.mock.calls.find(([input, init]) => path(input) === "/api/research/requests" && init?.method === "POST");
    const marketPayload = JSON.parse(String(marketCall?.[1]?.body));
    expect(marketPayload).toMatchObject({ team_ref: "market_overview@1", scope: "market", code: null });
    expect(marketPayload.submission_identity).toMatch(/^web-/);
    expect(submitted).toHaveBeenCalledWith(expect.objectContaining({ scope: "market" }));

    await user.selectOptions(screen.getByLabelText("已发布 Team 版本"), "normal@1");
    const code = screen.getByLabelText("证券代码");
    await user.type(code, "689009");
    await user.click(screen.getByRole("button", { name: "提交研究请求" }));
    expect(screen.getByRole("alert")).toHaveTextContent("请输入合法的六位沪深 A 股代码");
    expect(fetchMock.mock.calls.filter(([input, init]) => path(input) === "/api/research/requests" && init?.method === "POST")).toHaveLength(1);

    await user.clear(code);
    await user.type(code, "600519");
    await user.click(screen.getByRole("button", { name: "提交研究请求" }));
    const calls = fetchMock.mock.calls.filter(([input, init]) => path(input) === "/api/research/requests" && init?.method === "POST");
    expect(JSON.parse(String(calls[1]?.[1]?.body))).toMatchObject({ team_ref: "normal@1", scope: "security", code: "600519" });
  });
});
