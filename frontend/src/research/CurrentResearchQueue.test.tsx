import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CurrentResearchQueue from "./CurrentResearchQueue";
import type { ResearchRequest } from "./contracts";

const NOW = "2026-08-06T08:30:00+00:00";
const RUNNING_ID = `request-${"b".repeat(32)}`;
const QUEUED_ID = `request-${"c".repeat(32)}`;

function json(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" }, ...init });
}

function queuedRequest(id: string, status: "running" | "queued"): ResearchRequest {
  return {
    request_id: id,
    team_ref: status === "running" ? "market_overview@1" : "normal@1",
    scope: status === "running" ? "market" : "security",
    subject: status === "running" ? { scope: "market", code: null, name: null } : { scope: "security", code: "600519", name: null },
    origin: status === "running" ? "scheduled" : "web",
    requested_at: NOW,
    accepted_at: NOW,
    boundary_at: status === "running" ? NOW : null,
    status,
    phase: status === "running" ? "agents" : "queued",
    agents_completed: status === "running" ? 1 : 0,
    agents_total: status === "running" ? 3 : 0,
    decision_stage: status === "running" ? "市场研究" : null,
    reason_code: null,
    published_at: null,
    rerun_of: null,
    last_updated_at: NOW,
    can_cancel: true,
    can_rerun: false,
  };
}

function terminalRequest(id: string, status: "passed" | "blocked", reasonCode: string | null): ResearchRequest {
  return {
    ...queuedRequest(id, "running"),
    status,
    phase: "complete",
    agents_completed: 3,
    reason_code: reasonCode,
    published_at: status === "passed" ? NOW : null,
    can_cancel: false,
    can_rerun: true,
  };
}

function path(input: RequestInfo | URL): string { return typeof input === "string" ? input : input.toString(); }

describe("Current research queue", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));

  it("shows the single running request before queued work and gives explicit cancellation feedback", async () => {
    let cancelled = false;
    const onTerminal = vi.fn();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = path(input);
      if (request === "/api/research/requests/current") {
        return json({
          service: { state: "running", heartbeat_at: NOW, active_request_id: RUNNING_ID, queued_count: cancelled ? 0 : 1, reason_code: null },
          requests: cancelled ? [queuedRequest(RUNNING_ID, "running")] : [queuedRequest(RUNNING_ID, "running"), queuedRequest(QUEUED_ID, "queued")],
        });
      }
      if (request === `/api/research/requests/${QUEUED_ID}/cancel` && init?.method === "POST") {
        cancelled = true;
        return json({ request: { ...queuedRequest(QUEUED_ID, "queued"), status: "cancelled", phase: "cancelled", can_cancel: false, can_rerun: true } });
      }
      if (request === `/api/research/requests/${QUEUED_ID}` && !init?.method) {
        return json({ request: { ...queuedRequest(QUEUED_ID, "queued"), status: "cancelled", phase: "cancelled", reason_code: "cancelled", can_cancel: false, can_rerun: true } });
      }
      throw new Error(`unexpected request ${request}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<CurrentResearchQueue onTerminal={onTerminal} />);

    expect(await screen.findByText(/服务执行中/)).toBeInTheDocument();
    expect(screen.getByText("market_overview@1")).toBeInTheDocument();
    expect(screen.getByText("normal@1")).toBeInTheDocument();
    expect(screen.getByText("Agent 1/3")).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: "取消" })[1]);

    expect(await screen.findByText("已取消等待中的研究请求。")).toBeInTheDocument();
    expect(screen.queryByText("normal@1")).not.toBeInTheDocument();
    expect(onTerminal).toHaveBeenCalledTimes(1);
  });

  it("turns a disappeared running request into an explicit no-report terminal receipt", async () => {
    let finished = false;
    const onTerminal = vi.fn();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = path(input);
      if (request === "/api/research/requests/current") {
        return json({
          service: { state: finished ? "idle" : "running", heartbeat_at: NOW, active_request_id: finished ? null : RUNNING_ID, queued_count: 0, reason_code: null },
          requests: finished ? [] : [queuedRequest(RUNNING_ID, "running")],
        });
      }
      if (request === `/api/research/requests/${RUNNING_ID}` && !init?.method) {
        return json({ request: terminalRequest(RUNNING_ID, "blocked", "snapshot_unavailable") });
      }
      throw new Error(`unexpected request ${request}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<CurrentResearchQueue onTerminal={onTerminal} />);

    expect(await screen.findByText("market_overview@1")).toBeInTheDocument();
    finished = true;
    await user.click(screen.getByRole("button", { name: "刷新当前研究" }));

    expect(await screen.findByText(/研究已阻断，未生成报告/)).toBeInTheDocument();
    expect(screen.getByText(/数据快照不可用/)).toBeInTheDocument();
    expect(screen.getByText(/全部终态/)).toBeInTheDocument();
    expect(onTerminal).toHaveBeenCalledWith(RUNNING_ID);
    expect(onTerminal).toHaveBeenCalledTimes(1);
  });

  it("resolves a submitted request that was already terminal before the first current poll", async () => {
    const submitted = queuedRequest(QUEUED_ID, "queued");
    const onTerminal = vi.fn();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = path(input);
      if (request === "/api/research/requests/current") {
        return json({
          service: { state: "idle", heartbeat_at: NOW, active_request_id: null, queued_count: 0, reason_code: null },
          requests: [],
        });
      }
      if (request === `/api/research/requests/${QUEUED_ID}` && !init?.method) {
        return json({ request: terminalRequest(QUEUED_ID, "blocked", "preflight_failed") });
      }
      throw new Error(`unexpected request ${request}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<CurrentResearchQueue submittedRequests={[submitted]} onTerminal={onTerminal} />);

    expect(await screen.findByText(/研究已阻断，未生成报告/)).toBeInTheDocument();
    expect(screen.getByText(/运行前检查失败/)).toBeInTheDocument();
    expect(onTerminal).toHaveBeenCalledWith(QUEUED_ID);
  });
});
