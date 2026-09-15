import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

const currentState = {
  today: "2026-07-12",
  last_successful_data_update: "2026-07-12T08:32:00+08:00",
  advice_status: "passed",
  advice: [
    {
      advice_id: "advice-1",
      code: "600519",
      action: "watch",
      confidence: 0.7,
      rationale: "等待量价确认",
      evidence_ids: ["evidence-1"],
    },
  ],
  review: { status: "passed", items: [] },
  ledger: {
    status: "ok",
    cash: 89995,
    positions: [
      {
        code: "600519",
        quantity: 100,
        cost_basis: 10005,
        market_price: 101.2,
        market_value: 10120,
        unrealized_pnl: 115,
      },
    ],
    realized_pnl: 320,
    unrealized_pnl: 115,
    accounts: ["default"],
  },
  flows: {
    information: { status: "ok", count: 4 },
    capital: { status: "degraded", count: 2 },
    analyst: { status: "ok", count: 3 },
  },
  blocking_quality_checks: [],
  reports: [
    {
      report_date: "2026-07-12",
      report_type: "premarket",
      run_id: "initial",
      quality_status: "passed",
      href: "/api/reports/2026-07-12/premarket?run_id=initial",
    },
  ],
  report_list: { status: "ok", truncated: false },
  profiles: [{ code: "600519", name: "贵州茅台", href: "/api/profiles/600519" }],
  profile_list: { status: "ok" },
  charts: [
    {
      asset_id: "chart-1",
      code: "600519",
      chart_type: "kline",
      as_of: "2026-07-12",
      href: "/api/charts/chart-1",
    },
  ],
  chart_list: { status: "ok" },
  health: {
    status: "ok",
    service: "advisor-api",
    collector: "running",
    market_updater: "ok",
    advisor_scheduler: "ok",
    frontend: "unknown",
    api: "ok",
  },
};

const researchAgents = {
  agents: [
    { agent_id: "market", agent_ref: "market@1", scope: "security", title: "市场研究", summary: "研究市场数据。" },
  ],
};

const researchTeams = {
  teams: [
    {
      team_id: "core",
      latest: { team_ref: "core@1", scope: "security", title: "核心团队", agents: ["market@1"] },
      history: [{ team_ref: "core@1", scope: "security", title: "核心团队", agents: ["market@1"] }],
      daily_enabled_ref: null,
    },
  ],
};

function response(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

function requestPath(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  return input.url;
}

function resourceLinks(): HTMLAnchorElement[] {
  return screen.queryAllByRole("link").filter((link) => link.getAttribute("href")?.startsWith("/api/") ?? false) as HTMLAnchorElement[];
}

function isIndependentServiceRequest(path: string): boolean {
  return path === "/api/services" || path.startsWith("/api/market-daily/");
}

function researchTeamResponse(path: string): Response | undefined {
  if (path === "/api/research/agents") return response(researchAgents);
  if (path === "/api/research/teams") return response(researchTeams);
  return undefined;
}

function mockFetch(...responses: Array<Response | Error>) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const path = requestPath(input);
    const research = researchTeamResponse(path);
    if (research) return research;
    if (isIndependentServiceRequest(path)) {
      return response({ detail: "服务状态未配置" }, { status: 503 });
    }
    const next = responses.shift();
    if (next instanceof Error) throw next;
    if (!next) throw new Error("Unexpected fetch");
    return next;
  });
}

describe("advisor dashboard", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  it("fetches and renders the live operating state and API links", async () => {
    const fetchMock = mockFetch(response(currentState));
    render(<App />);

    expect(screen.getByText("正在读取当前状态…")).toBeInTheDocument();
    expect(await screen.findByText("2026-07-12")).toBeInTheDocument();
    expect(screen.getByText("最近数据更新 2026-07-12T08:32:00+08:00")).toBeInTheDocument();
    expect(screen.getByText("¥89,995.00")).toBeInTheDocument();
    expect(screen.getAllByText("600519").length).toBeGreaterThan(0);
    expect(screen.getByText("等待量价确认")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "MX 监听" })).toHaveAttribute("href", "/mx");
    expect(screen.getByRole("link", { name: "研究配置" })).toHaveAttribute("href", "/research");
    expect(screen.getByRole("link", { name: /2026-07-12 盘前/ })).toHaveAttribute(
      "href",
      "/api/reports/2026-07-12/premarket?run_id=initial",
    );
    expect(screen.getByRole("link", { name: /贵州茅台/ })).toHaveAttribute("href", "/api/profiles/600519");
    expect(screen.getByRole("link", { name: /600519 K线/ })).toHaveAttribute("href", "/api/charts/chart-1");
    expect(fetchMock).toHaveBeenCalledWith("/api/current-state", expect.objectContaining({ signal: expect.any(AbortSignal) }));
  });

  it("withholds advice and surfaces blocking quality checks", async () => {
    mockFetch(
      response({
        ...currentState,
        advice_status: "blocked",
        advice: [],
        review: { status: "blocked", items: [] },
        blocking_quality_checks: [
          {
            check_name: "market_stale",
            severity: "blocking",
            status: "failed",
            created_at: "2026-07-12T08:31:00+08:00",
          },
        ],
      }),
    );
    render(<App />);

    expect(await screen.findByText("建议已阻断")).toBeInTheDocument();
    expect(screen.getByText("报告 已阻断")).toBeInTheDocument();
    expect(screen.getByText("market_stale")).toBeInTheDocument();
    expect(screen.queryByText("等待量价确认")).not.toBeInTheDocument();
  });

  it("shows missing current review status instead of archive listing health", async () => {
    mockFetch(response({ ...currentState, review: { status: "missing", items: [] } }));
    render(<App />);

    expect(await screen.findByText("报告 缺失")).toBeInTheDocument();
    expect(screen.queryByText("报告 正常")).not.toBeInTheDocument();
  });

  it("fails closed when the core advice status is unknown", async () => {
    mockFetch(response({ ...currentState, advice_status: "unexpected" }));
    render(<App />);

    expect(await screen.findByText("当前状态读取失败")).toBeInTheDocument();
    expect(screen.queryByText("等待量价确认")).not.toBeInTheDocument();
  });

  it.each(["degraded", "unknown"])(
    "withholds fallback ledger metrics and positions when ledger status is %s",
    async (ledgerStatus) => {
      mockFetch(
        response({
          ...currentState,
          ledger: {
            status: ledgerStatus,
            cash: 0,
            positions: [],
            realized_pnl: 0,
            unrealized_pnl: 0,
            accounts: [],
          },
        }),
      );
      render(<App />);

      expect(await screen.findByText(ledgerStatus === "degraded" ? "账户数据已降级" : "账户数据状态未知")).toBeInTheDocument();
      expect(screen.getByText("持仓数据不可用")).toBeInTheDocument();
      expect(screen.queryByText("¥0.00")).not.toBeInTheDocument();
      expect(screen.queryByRole("table")).not.toBeInTheDocument();
    },
  );

  it("withholds ledger metrics when ledger status is omitted", async () => {
    const ledger = { ...currentState.ledger } as Partial<typeof currentState.ledger>;
    delete ledger.status;
    mockFetch(response({ ...currentState, ledger }));
    render(<App />);

    expect(await screen.findByText("账户数据状态未知")).toBeInTheDocument();
    expect(screen.queryByText("¥89,995.00")).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it.each(["unknown", "degraded"])("withholds flow counts when flow status is %s", async (flowStatus) => {
    mockFetch(response({
      ...currentState,
      flows: {
        ...currentState.flows,
        capital: { status: flowStatus, count: 2 },
      },
    }));
    render(<App />);

    const flows = await screen.findByRole("region", { name: "三流状态" });
    const capital = within(flows).getByText("资金流").parentElement as HTMLElement;
    expect(within(capital).getByText("暂无数据")).toBeInTheDocument();
    expect(within(capital).queryByText("2 条")).not.toBeInTheDocument();
  });

  it("rejects a negative flow count without rendering it", async () => {
    mockFetch(response({
      ...currentState,
      flows: { ...currentState.flows, information: { status: "ok", count: -1 } },
    }));
    render(<App />);

    expect(await screen.findByText("当前状态读取失败")).toBeInTheDocument();
    expect(screen.queryByText("-1 条")).not.toBeInTheDocument();
  });

  it.each([
    ["today", { today: "2026-02-30" }],
    ["last update", { last_successful_data_update: "2026-02-30T08:32:00+08:00" }],
    ["chart as_of", { charts: [{ ...currentState.charts[0], as_of: "2026-02-30" }] }],
  ])("rejects an invalid %s date", async (_label, override) => {
    mockFetch(response({ ...currentState, ...override }));
    render(<App />);

    expect(await screen.findByText("当前状态读取失败")).toBeInTheDocument();
    expect(resourceLinks()).toHaveLength(0);
  });

  it("rejects an unknown report quality status", async () => {
    mockFetch(response({
      ...currentState,
      reports: [{ ...currentState.reports[0], quality_status: "unexpected" }],
    }));
    render(<App />);

    expect(await screen.findByText("当前状态读取失败")).toBeInTheDocument();
    expect(resourceLinks()).toHaveLength(0);
  });

  it.each([
    ["advice code", { advice: [{ ...currentState.advice[0], code: "SH600519" }] }],
    ["negative confidence", { advice: [{ ...currentState.advice[0], confidence: -0.01 }] }],
    ["excessive confidence", { advice: [{ ...currentState.advice[0], confidence: 1.01 }] }],
  ])("rejects malformed %s without rendering advice or metrics", async (_label, override) => {
    mockFetch(response({ ...currentState, ...override }));
    render(<App />);

    expect(await screen.findByText("当前状态读取失败")).toBeInTheDocument();
    expect(screen.queryByText("等待量价确认")).not.toBeInTheDocument();
    expect(screen.queryByText("¥89,995.00")).not.toBeInTheDocument();
  });

  it.each([
    ["severity", { severity: "warning" }],
    ["status", { status: "passed" }],
    ["timestamp", { created_at: "not-a-timestamp" }],
  ])("rejects a blocking quality check with malformed %s", async (_label, checkOverride) => {
    mockFetch(response({
      ...currentState,
      advice_status: "blocked",
      advice: [],
      blocking_quality_checks: [{
        check_name: "market_stale",
        severity: "blocking",
        status: "failed",
        created_at: "2026-07-12T08:31:00+08:00",
        ...checkOverride,
      }],
    }));
    render(<App />);

    expect(await screen.findByText("当前状态读取失败")).toBeInTheDocument();
    expect(screen.queryByText("market_stale")).not.toBeInTheDocument();
    expect(screen.queryByText("¥89,995.00")).not.toBeInTheDocument();
  });

  it.each([
    ["overall status", { status: "unexpected" }],
    ["service", { service: "other-api" }],
    ["component status", { collector: "busy" }],
  ])("rejects malformed health %s", async (_label, healthOverride) => {
    mockFetch(response({ ...currentState, health: { ...currentState.health, ...healthOverride } }));
    render(<App />);

    expect(await screen.findByText("当前状态读取失败")).toBeInTheDocument();
    expect(screen.queryByText("等待量价确认")).not.toBeInTheDocument();
    expect(screen.queryByText("¥89,995.00")).not.toBeInTheDocument();
  });

  it("rejects health payloads with extra component keys", async () => {
    mockFetch(response({ ...currentState, health: { ...currentState.health, worker_token: "unknown" } }));
    render(<App />);

    expect(await screen.findByText("当前状态读取失败")).toBeInTheDocument();
    expect(screen.queryByText("等待量价确认")).not.toBeInTheDocument();
    expect(screen.queryByText("¥89,995.00")).not.toBeInTheDocument();
    expect(screen.queryByText("worker_token")).not.toBeInTheDocument();
  });

  it("localizes healthy and stopped health components with semantic status styles", async () => {
    mockFetch(response({
      ...currentState,
      health: {
        ...currentState.health,
        collector: "healthy",
        market_updater: "stopped",
        advisor_scheduler: "healthy",
        frontend: "stopped",
        api: "healthy",
      },
    }));
    render(<App />);

    const healthList = (await screen.findByText("采集器")).closest("dl") as HTMLElement;
    const health = within(healthList);
    expect(health.getByText("行情更新")).toBeInTheDocument();
    expect(health.getByText("投顾调度")).toBeInTheDocument();
    expect(health.getByText("前端")).toBeInTheDocument();
    expect(health.getByText("接口")).toBeInTheDocument();
    expect(health.getAllByText("健康")).toHaveLength(3);
    for (const status of health.getAllByText("健康")) expect(status).toHaveClass("status-ok");
    expect(health.getAllByText("已停止")).toHaveLength(2);
    for (const status of health.getAllByText("已停止")) expect(status).toHaveClass("status-warn");
    for (const identifier of ["collector", "market_updater", "advisor_scheduler", "frontend", "api"]) {
      expect(health.queryByText(identifier)).not.toBeInTheDocument();
    }
  });

  it.each([
    ["profile", { profile_list: { status: "degraded" }, profiles: currentState.profiles }],
    ["chart", { chart_list: { status: "degraded" }, charts: currentState.charts }],
  ])("suppresses %s links when the parent list is degraded", async (_label, override) => {
    mockFetch(response({ ...currentState, ...override }));
    render(<App />);

    expect(await screen.findByText("当前状态读取失败")).toBeInTheDocument();
    expect(resourceLinks()).toHaveLength(0);
  });

  it.each([
    ["missing advice containing items", { advice_status: "missing" }],
    [
      "passed advice with a blocking check",
      {
        advice_status: "passed",
        blocking_quality_checks: [
          {
            check_name: "market_stale",
            severity: "blocking",
            status: "failed",
            created_at: "2026-07-12T08:31:00+08:00",
          },
        ],
      },
    ],
  ])("rejects contradictory %s", async (_label, overrides) => {
    mockFetch(response({ ...currentState, ...overrides }));
    render(<App />);

    expect(await screen.findByText("当前状态读取失败")).toBeInTheDocument();
    expect(screen.queryByText("等待量价确认")).not.toBeInTheDocument();
  });

  it("fails closed when linked resource metadata is malformed", async () => {
    mockFetch(response({ ...currentState, reports: [{ ...currentState.reports[0], report_date: { invalid: true } }] }));
    render(<App />);

    expect(await screen.findByText("当前状态读取失败")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /盘前/ })).not.toBeInTheDocument();
  });

  it.each([
    ["report", { reports: [{ ...currentState.reports[0], href: "/api/profiles/600519" }] }],
    ["profile code", { profiles: [{ ...currentState.profiles[0], code: "60051" }] }],
    ["profile target", { profiles: [{ ...currentState.profiles[0], href: "/api/profiles/000001" }] }],
    ["chart type", { charts: [{ ...currentState.charts[0], chart_type: "thumbnail" }] }],
    ["chart code", { charts: [{ ...currentState.charts[0], code: "SH600519" }] }],
    ["chart target", { charts: [{ ...currentState.charts[0], href: "/api/reports/2026-07-12/review" }] }],
  ])("rejects a mismatched or unsafe %s link", async (_label, override) => {
    mockFetch(response({ ...currentState, ...override }));
    render(<App />);

    expect(await screen.findByText("当前状态读取失败")).toBeInTheDocument();
    expect(resourceLinks()).toHaveLength(0);
  });

  it("shows request errors and retries without reloading the page", async () => {
    const fetchMock = mockFetch(new Error("offline"), response(currentState));
    render(<App />);

    expect(await screen.findByText("当前状态读取失败")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重试读取" }));

    expect(await screen.findByText("2026-07-12")).toBeInTheDocument();
    expect(fetchMock.mock.calls.map(([input]) => requestPath(input)).filter((path) => path === "/api/current-state")).toHaveLength(2);
  });

  it("fails closed after refresh failure while retaining a stale snapshot", async () => {
    mockFetch(response(currentState), new Error("refresh offline"));
    render(<App />);
    await screen.findByText("等待量价确认");

    await userEvent.click(screen.getByRole("button", { name: "刷新当前状态" }));

    expect(await screen.findByText("状态刷新失败，先前快照已停用")).toBeInTheDocument();
    expect(screen.queryByText("等待量价确认")).not.toBeInTheDocument();
    expect(screen.queryByText("¥89,995.00")).not.toBeInTheDocument();
    expect(screen.queryByText("4 条")).not.toBeInTheDocument();
    expect(resourceLinks()).toHaveLength(0);
    expect(screen.getByText("请刷新成功后再查看建议、账户指标和资源链接")).toBeInTheDocument();
  });

  it("reserves empty resource messages for available empty lists", async () => {
    mockFetch(response({ ...currentState, reports: [], profiles: [], charts: [] }));
    render(<App />);

    expect(await screen.findByText("暂无报告")).toBeInTheDocument();
    expect(screen.getByText("暂无档案")).toBeInTheDocument();
    expect(screen.getByText("暂无图表")).toBeInTheDocument();
    expect(screen.queryByText(/列表已降级/)).not.toBeInTheDocument();
  });

  it("renders unavailable messages for degraded resource parents", async () => {
    mockFetch(response({
      ...currentState,
      reports: [],
      report_list: { status: "degraded", truncated: true },
      profiles: [],
      profile_list: { status: "degraded" },
      charts: [],
      chart_list: { status: "degraded" },
    }));
    render(<App />);

    expect(await screen.findByText("报告列表已降级，暂不可用")).toBeInTheDocument();
    expect(screen.getByText("档案列表已降级，暂不可用")).toBeInTheDocument();
    expect(screen.getByText("图表列表已降级，暂不可用")).toBeInTheDocument();
    expect(screen.queryByText("暂无报告")).not.toBeInTheDocument();
    expect(screen.queryByText("暂无档案")).not.toBeInTheDocument();
    expect(screen.queryByText("暂无图表")).not.toBeInTheDocument();
  });

  it("renders unavailable resource messages for a stale snapshot", async () => {
    mockFetch(response(currentState), new Error("refresh offline"));
    render(<App />);
    await screen.findByText("等待量价确认");

    await userEvent.click(screen.getByRole("button", { name: "刷新当前状态" }));

    expect(await screen.findByText("报告资源不可用，当前快照已失效")).toBeInTheDocument();
    expect(screen.getByText("档案资源不可用，当前快照已失效")).toBeInTheDocument();
    expect(screen.getByText("图表资源不可用，当前快照已失效")).toBeInTheDocument();
    expect(screen.queryByText("暂无报告")).not.toBeInTheDocument();
    expect(screen.queryByText("暂无档案")).not.toBeInTheDocument();
    expect(screen.queryByText("暂无图表")).not.toBeInTheDocument();
  });

  it("keeps a stale snapshot suppressed until a retry succeeds", async () => {
    let finishRetry: (result: Response) => void = () => undefined;
    const retry = new Promise<Response>((resolve) => { finishRetry = resolve; });
    let requestNumber = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = requestPath(input);
      const research = researchTeamResponse(path);
      if (research) return research;
      if (isIndependentServiceRequest(path)) {
        return response({ detail: "服务状态未配置" }, { status: 503 });
      }
      requestNumber += 1;
      if (requestNumber === 1) return response(currentState);
      if (requestNumber === 2) throw new Error("refresh offline");
      return retry;
    });
    render(<App />);
    await screen.findByText("等待量价确认");
    await userEvent.click(screen.getByRole("button", { name: "刷新当前状态" }));
    await screen.findByText("状态刷新失败，先前快照已停用");

    await userEvent.click(screen.getByRole("button", { name: "刷新当前状态" }));

    expect(screen.queryByText("等待量价确认")).not.toBeInTheDocument();
    expect(screen.getByText("状态刷新失败，先前快照已停用")).toBeInTheDocument();
    finishRetry(response(currentState));
    expect(await screen.findByText("等待量价确认")).toBeInTheDocument();
  });

  it("rejects degraded report listings that contain report links", async () => {
    mockFetch(response({ ...currentState, report_list: { status: "degraded" }, reports: currentState.reports }));
    render(<App />);

    expect(await screen.findByText("当前状态读取失败")).toBeInTheDocument();
    expect(resourceLinks()).toHaveLength(0);
  });

  it("renders an explicit unavailable last successful update", async () => {
    mockFetch(response({ ...currentState, last_successful_data_update: null }));
    render(<App />);

    expect(await screen.findByText("最近数据更新 暂无数据")).toBeInTheDocument();
  });

  it("submits a manual ledger transaction and refreshes current state", async () => {
    const refreshed = { ...currentState, ledger: { ...currentState.ledger, cash: 90995 } };
    const fetchMock = mockFetch(response(currentState), response({ ledger: refreshed.ledger }, { status: 201 }), response(refreshed));
    render(<App />);
    await screen.findByText("2026-07-12");

    const form = screen.getByRole("form", { name: "新增流水" });
    await userEvent.type(within(form).getByLabelText("流水编号"), "cash-2");
    await userEvent.type(within(form).getByLabelText("交易日期"), "2026-07-12");
    await userEvent.clear(within(form).getByLabelText("金额"));
    await userEvent.type(within(form).getByLabelText("金额"), "1000");
    await userEvent.click(within(form).getByRole("button", { name: "保存流水" }));

    expect(await screen.findByText("流水已保存，状态已刷新")).toBeInTheDocument();
    expect(screen.getByText("¥90,995.00")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/ledger/transactions",
      expect.objectContaining({ method: "POST", body: expect.stringContaining('"transaction_id":"cash-2"') }),
    );
  });

  it("preserves manual ledger fields when the API rejects the transaction", async () => {
    mockFetch(response(currentState), response({ detail: "transaction_id already exists" }, { status: 409 }));
    render(<App />);
    await screen.findByText("2026-07-12");

    const form = screen.getByRole("form", { name: "新增流水" });
    await userEvent.type(within(form).getByLabelText("流水编号"), "duplicate-1");
    await userEvent.type(within(form).getByLabelText("交易日期"), "2026-07-12");
    await userEvent.clear(within(form).getByLabelText("金额"));
    await userEvent.type(within(form).getByLabelText("金额"), "1000");
    await userEvent.click(within(form).getByRole("button", { name: "保存流水" }));

    expect(await screen.findByText("transaction_id already exists")).toBeInTheDocument();
    expect(within(form).getByLabelText("流水编号")).toHaveValue("duplicate-1");
    expect(within(form).getByLabelText("金额")).toHaveValue(1000);
  });

  it("preserves manual ledger fields and does not claim refresh success when refresh fails", async () => {
    mockFetch(response(currentState), response({}, { status: 201 }), new Error("refresh offline"));
    render(<App />);
    await screen.findByText("2026-07-12");

    const form = screen.getByRole("form", { name: "新增流水" });
    await userEvent.type(within(form).getByLabelText("流水编号"), "cash-refresh-failure");
    await userEvent.type(within(form).getByLabelText("交易日期"), "2026-07-12");
    await userEvent.clear(within(form).getByLabelText("金额"));
    await userEvent.type(within(form).getByLabelText("金额"), "1000");
    await userEvent.click(within(form).getByRole("button", { name: "保存流水" }));

    expect(await within(form).findByText("流水已保存，但状态刷新失败；输入已保留，请刷新确认")).toBeInTheDocument();
    expect(within(form).queryByText("流水已保存，状态已刷新")).not.toBeInTheDocument();
    expect(within(form).getByLabelText("流水编号")).toHaveValue("cash-refresh-failure");
    expect(within(form).getByLabelText("金额")).toHaveValue(1000);
  });

  it("imports a bounded JSON transaction list and preserves rejected text", async () => {
    const input = '[{"transaction_id":"cash-3","trade_date":"2026-07-12","transaction_type":"cash_deposit","quantity":0,"price":0,"amount":500,"fees":0}]';
    const fetchMock = mockFetch(response(currentState), response({ detail: "import conflict" }, { status: 409 }));
    render(<App />);
    await screen.findByText("2026-07-12");

    fireEvent.change(screen.getByLabelText("JSON 流水列表"), { target: { value: input } });
    await userEvent.click(screen.getByRole("button", { name: "导入 JSON" }));

    expect(await screen.findByText("import conflict")).toBeInTheDocument();
    expect(screen.getByLabelText("JSON 流水列表")).toHaveValue(input);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/ledger/import",
      expect.objectContaining({ method: "POST", body: input }),
    );
  });

  it("preserves imported JSON and does not claim refresh success when refresh fails", async () => {
    const input = '[{"transaction_id":"cash-4","trade_date":"2026-07-12","transaction_type":"cash_deposit","quantity":0,"price":0,"amount":500,"fees":0}]';
    mockFetch(response(currentState), response({}, { status: 201 }), new Error("refresh offline"));
    render(<App />);
    await screen.findByText("2026-07-12");

    const form = screen.getByRole("form", { name: "JSON 导入" });
    fireEvent.change(within(form).getByLabelText("JSON 流水列表"), { target: { value: input } });
    await userEvent.click(within(form).getByRole("button", { name: "导入 JSON" }));

    expect(await within(form).findByText("JSON 流水已导入，但状态刷新失败；内容已保留，请刷新确认")).toBeInTheDocument();
    expect(within(form).queryByText("JSON 流水已导入，状态已刷新")).not.toBeInTheDocument();
    expect(within(form).getByLabelText("JSON 流水列表")).toHaveValue(input);
  });

  it("clears imported JSON only after refresh succeeds", async () => {
    const input = '[{"transaction_id":"cash-5","trade_date":"2026-07-12","transaction_type":"cash_deposit","quantity":0,"price":0,"amount":500,"fees":0}]';
    const refreshed = { ...currentState, ledger: { ...currentState.ledger, cash: 90495 } };
    mockFetch(response(currentState), response({}, { status: 201 }), response(refreshed));
    render(<App />);
    await screen.findByText("2026-07-12");

    const form = screen.getByRole("form", { name: "JSON 导入" });
    fireEvent.change(within(form).getByLabelText("JSON 流水列表"), { target: { value: input } });
    await userEvent.click(within(form).getByRole("button", { name: "导入 JSON" }));

    expect(await within(form).findByText("JSON 流水已导入，状态已刷新")).toBeInTheDocument();
    expect(screen.getByText("¥90,495.00")).toBeInTheDocument();
    expect(within(form).getByLabelText("JSON 流水列表")).toHaveValue("");
  });

  it("renders Market Daily progress, failed securities, and the read-only service set", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = requestPath(input);
      const research = researchTeamResponse(path);
      if (research) return research;
      if (path === "/api/current-state") return response(currentState);
      if (path === "/api/market-daily/status") {
        return response({
          state: "partial",
          service_status: "running",
          lease_active: true,
          lease_expires_at: "2026-07-12T21:05:00+08:00",
          latest_observed_session: "2026-07-11",
          last_successful_update: "2026-07-11T21:12:00+08:00",
          next_scheduled_at: "2026-07-12T21:00:00+08:00",
          run_id: "mdrun-20260711",
          mode: "cold_start",
          target_session: "2026-07-11",
          total_items: 2,
          completed_items: 1,
          failed_items: 1,
          progress: 0.5,
          run_status: "partial",
        });
      }
      if (path === "/api/market-daily/runs/mdrun-20260711/failures?limit=20") {
        return response({
          items: [{
            code: "600519",
            status: "source_missing",
            attempts: 3,
            selected_source: "tdx",
            error: "缺少已证实的交易日数据",
            updated_at: "2026-07-11T21:14:00+08:00",
          }],
        });
      }
      if (path === "/api/services") {
        return response({
          服务: [
            { 编号: "market-daily", 状态: "运行中", 说明: "常驻服务持有租约", 日志: ["/tmp/market.log"] },
            { 编号: "mx-listener", 状态: "只读", 说明: "现有监听服务", 日志: ["/tmp/mx.log"], read_only: true },
          ],
        });
      }
      throw new Error(`Unexpected fetch ${path}`);
    });

    render(<App />);

    expect(await screen.findByText("部分完成")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Market Daily 进度" })).toHaveAttribute("value", "0.5");
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
    expect(screen.getByText("待排查证券")).toBeInTheDocument();
    expect(screen.getByText("来源未证明")).toBeInTheDocument();
    expect(screen.getByText("Market Daily")).toBeInTheDocument();
    expect(screen.getByText("MX Listener（只读）")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/market-daily/status", expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(fetchMock).toHaveBeenCalledWith("/api/services", expect.objectContaining({ signal: expect.any(AbortSignal) }));
  });

  it("submits one five-year cold-start intent from the Market Daily panel", async () => {
    let marketStatusReads = 0;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const path = requestPath(input);
      const research = researchTeamResponse(path);
      if (research) return research;
      if (path === "/api/current-state") return response(currentState);
      if (path === "/api/market-daily/status") {
        marketStatusReads += 1;
        return response({
          state: marketStatusReads === 1 ? "idle" : "waiting_for_cold_start",
          service_status: "running",
          lease_active: true,
          lease_expires_at: "2026-07-12T21:05:00+08:00",
          latest_observed_session: null,
          last_successful_update: null,
          next_scheduled_at: "2026-07-12T21:00:00+08:00",
          run_id: null,
          mode: null,
          target_session: null,
          total_items: 0,
          completed_items: 0,
          failed_items: 0,
          progress: 0,
          run_status: null,
        });
      }
      if (path === "/api/market-daily/cold-start") {
        expect(init).toMatchObject({ method: "POST" });
        return response({
          request_id: "mdreq-c2c28608551dc2c7684c7cee",
          request_status: "pending",
          message: "冷启动请求已在本地队列中；Market Daily 服务会在 21:00 后执行",
        }, { status: 202 });
      }
      if (path === "/api/services") return response({ 服务: [] });
      throw new Error(`Unexpected fetch ${path}`);
    });

    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: "启动五年同步" }));

    expect(await screen.findByText("冷启动请求已在本地队列中；Market Daily 服务会在 21:00 后执行")).toBeInTheDocument();
    expect(await screen.findByText("冷启动请求已排队，服务将在 21:00 后执行")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "冷启动已排队" })).toBeDisabled();
    expect(fetchMock).toHaveBeenCalledWith("/api/market-daily/cold-start", { method: "POST" });
  });

  it("shows a bounded error when the five-year cold-start request is rejected", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = requestPath(input);
      const research = researchTeamResponse(path);
      if (research) return research;
      if (path === "/api/current-state") return response(currentState);
      if (path === "/api/market-daily/status") return response({
        state: "idle",
        service_status: "offline",
        lease_active: false,
        lease_expires_at: null,
        latest_observed_session: null,
        last_successful_update: null,
        next_scheduled_at: "2026-07-12T21:00:00+08:00",
        run_id: null,
        mode: null,
        target_session: null,
        total_items: 0,
        completed_items: 0,
        failed_items: 0,
        progress: 0,
        run_status: null,
      });
      if (path === "/api/market-daily/cold-start") {
        return response({ detail: "Market Daily 冷启动请求不可提交" }, { status: 503 });
      }
      if (path === "/api/services") return response({ 服务: [] });
      throw new Error(`Unexpected fetch ${path}`);
    });

    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: "启动五年同步" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Market Daily 冷启动请求不可提交");
    expect(fetchMock).toHaveBeenCalledWith("/api/market-daily/cold-start", { method: "POST" });
  });

  it("does not claim the Market Daily status refreshed when it fails after queueing", async () => {
    let marketStatusReads = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = requestPath(input);
      const research = researchTeamResponse(path);
      if (research) return research;
      if (path === "/api/current-state") return response(currentState);
      if (path === "/api/market-daily/status") {
        marketStatusReads += 1;
        if (marketStatusReads > 1) return response({ detail: "Market Daily 状态不可读取" }, { status: 503 });
        return response({
          state: "idle",
          service_status: "running",
          lease_active: true,
          lease_expires_at: "2026-07-12T21:05:00+08:00",
          latest_observed_session: null,
          last_successful_update: null,
          next_scheduled_at: "2026-07-12T21:00:00+08:00",
          run_id: null,
          mode: null,
          target_session: null,
          total_items: 0,
          completed_items: 0,
          failed_items: 0,
          progress: 0,
          run_status: null,
        });
      }
      if (path === "/api/market-daily/cold-start") return response({
        request_id: "mdreq-c2c28608551dc2c7684c7cee",
        request_status: "pending",
        message: "冷启动请求已在本地队列中；Market Daily 服务会在 21:00 后执行",
      }, { status: 202 });
      if (path === "/api/services") return response({ 服务: [] });
      throw new Error(`Unexpected fetch ${path}`);
    });

    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: "启动五年同步" }));

    expect(await screen.findByText("冷启动请求已在本地队列中；Market Daily 服务会在 21:00 后执行，但状态刷新失败，请刷新确认")).toBeInTheDocument();
  });

  it.each([
    ["pending", "等待执行"],
    ["cancelled", "已取消"],
  ])("renders the Market Daily %s state in clear Chinese", async (state, label) => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = requestPath(input);
      const research = researchTeamResponse(path);
      if (research) return research;
      if (path === "/api/current-state") return response(currentState);
      if (path === "/api/market-daily/status") {
        return response({
          state,
          service_status: "offline",
          lease_active: false,
          lease_expires_at: null,
          latest_observed_session: null,
          last_successful_update: null,
          next_scheduled_at: "2026-07-12T21:00:00+08:00",
          run_id: null,
          mode: null,
          target_session: null,
          total_items: 0,
          completed_items: 0,
          failed_items: 0,
          progress: 0,
          run_status: null,
        });
      }
      if (path === "/api/services") return response({ 服务: [] });
      throw new Error(`Unexpected fetch ${path}`);
    });

    render(<App />);

    expect(await screen.findByText(label)).toBeInTheDocument();
    expect(screen.queryByText(state)).not.toBeInTheDocument();
  });
});
