import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

const currentState = {
  today: "2026-07-12",
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
  charts: [
    {
      asset_id: "chart-1",
      code: "600519",
      chart_type: "kline",
      as_of: "2026-07-12",
      href: "/api/charts/chart-1",
    },
  ],
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

function response(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

function mockFetch(...responses: Array<Response | Error>) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
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
    expect(screen.getByText("¥89,995.00")).toBeInTheDocument();
    expect(screen.getAllByText("600519").length).toBeGreaterThan(0);
    expect(screen.getByText("等待量价确认")).toBeInTheDocument();
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
    expect(screen.getByText("market_stale")).toBeInTheDocument();
    expect(screen.queryByText("等待量价确认")).not.toBeInTheDocument();
  });

  it("fails closed when the core advice status is unknown", async () => {
    mockFetch(response({ ...currentState, advice_status: "unexpected" }));
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

  it("shows request errors and retries without reloading the page", async () => {
    const fetchMock = mockFetch(new Error("offline"), response(currentState));
    render(<App />);

    expect(await screen.findByText("当前状态读取失败")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重试读取" }));

    expect(await screen.findByText("2026-07-12")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
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
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
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

  it("imports a bounded JSON transaction list and preserves rejected text", async () => {
    const input = '[{"transaction_id":"cash-3","trade_date":"2026-07-12","transaction_type":"cash_deposit","quantity":0,"price":0,"amount":500,"fees":0}]';
    const fetchMock = mockFetch(response(currentState), response({ detail: "import conflict" }, { status: 409 }));
    render(<App />);
    await screen.findByText("2026-07-12");

    fireEvent.change(screen.getByLabelText("JSON 流水列表"), { target: { value: input } });
    await userEvent.click(screen.getByRole("button", { name: "导入 JSON" }));

    expect(await screen.findByText("import conflict")).toBeInTheDocument();
    expect(screen.getByLabelText("JSON 流水列表")).toHaveValue(input);
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/ledger/import",
      expect.objectContaining({ method: "POST", body: input }),
    );
  });
});
