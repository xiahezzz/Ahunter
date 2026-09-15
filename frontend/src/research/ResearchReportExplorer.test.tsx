import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ResearchReportExplorer from "./ResearchReportExplorer";

const NOW = "2026-08-06T08:30:00+00:00";
const REQUEST_ID = `request-${"d".repeat(32)}`;
const RERUN_ID = `request-${"e".repeat(32)}`;
const RECORD_ID = `record-${"d".repeat(32)}`;

function json(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" }, ...init });
}

const record = {
  record_id: RECORD_ID,
  request_id: REQUEST_ID,
  team_ref: "market_overview@1",
  scope: "market",
  subject: { scope: "market", code: null, name: null },
  origin: "web",
  requested_at: NOW,
  accepted_at: NOW,
  boundary_at: NOW,
  status: "partial",
  phase: "complete",
  reason_code: null,
  published_at: NOW,
  rerun_of: null,
  has_report: true,
  quality_summary: { status: "warning", limitations_count: 1, blocked_insights: 1 },
};

const teams = {
  teams: [{
    team_id: "market_overview",
    latest: { team_ref: "market_overview@1", scope: "market", title: "市场概览", agents: ["market_breadth@1"] },
    history: [{ team_ref: "market_overview@1", scope: "market", title: "市场概览", agents: ["market_breadth@1"] }],
    daily_enabled_ref: null,
  }],
};

function request(id: string) {
  return {
    request_id: id,
    team_ref: "market_overview@1",
    scope: "market",
    subject: { scope: "market", code: null, name: null },
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
    rerun_of: REQUEST_ID,
    last_updated_at: NOW,
    can_cancel: true,
    can_rerun: false,
  };
}

function path(input: RequestInfo | URL): string { return typeof input === "string" ? input : input.toString(); }

describe("Research report explorer", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    window.history.replaceState(null, "", "/research");
  });

  it("uses server-side default filtering, safely opens a partial report, and creates a new rerun request", async () => {
    const onRerun = vi.fn();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const requestPath = path(input);
      if (requestPath === "/api/research/teams") return json(teams);
      if (requestPath.startsWith("/api/research/records?") && !init?.method) return json({ records: [record], limit: 20, offset: 0 });
      if (requestPath === `/api/research/records/${RECORD_ID}`) {
        return json({
          ...record,
          report: {
            json: {
              status: "partial",
              evidence_quality: { status: "warning", checks: ["boundary"], limitations: ["信息窗口有限"] },
              risks: ["样例覆盖范围有限。"],
              invalidation_conditions: ["后续同边界数据相反。"],
              insights: [
                { insight_id: "breadth_sentiment", status: "passed" },
                { insight_id: "macro_policy", status: "blocked", reason_code: "information_unavailable" },
              ],
              provenance: {
                pipeline: "market-overview-fixed@1",
                snapshot_id: "snapshot-market-fixture",
                snapshot_hash: "a".repeat(64),
                insight_hash: "b".repeat(64),
                agents: ["market_breadth@1", "market_macro_policy@1"],
                source_url: "https://unsafe.example/raw",
              },
            },
            markdown: "# 市场报告\n<script>window.__unsafe = true</script>\n- 可核验 Insight",
          },
        });
      }
      if (requestPath === `/api/research/requests/${REQUEST_ID}/rerun` && init?.method === "POST") return json({ request: request(RERUN_ID) }, { status: 202 });
      throw new Error(`unexpected request ${requestPath}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<ResearchReportExplorer onRerun={onRerun} />);

    expect(await screen.findByText("market_overview@1")).toBeInTheDocument();
    const recordsCall = fetchMock.mock.calls.find(([input]) => path(input).startsWith("/api/research/records?"));
    expect(path(recordsCall?.[0] as RequestInfo)).toContain("limit=20");
    expect(path(recordsCall?.[0] as RequestInfo)).not.toContain("status=");

    await user.click(screen.getByRole("button", { name: /market_overview@1 全市场 · 沪深 A 股整体 · 部分可用 · 质量：存在限制/ }));
    expect(await screen.findByRole("article", { name: "研究报告详情" })).toBeInTheDocument();
    expect(screen.queryByRole("script")).not.toBeInTheDocument();
    expect(screen.getByText("受理时间")).toBeInTheDocument();
    expect(screen.getByText("发布时间")).toBeInTheDocument();
    expect(screen.getByText("证据质量：")).toBeInTheDocument();
    expect(screen.getByText("风险：")).toBeInTheDocument();
    expect(screen.getByText("失效条件：")).toBeInTheDocument();
    expect(screen.getByText("未发布 Insight：")).toBeInTheDocument();
    expect(screen.getByText("溯源摘要：")).toBeInTheDocument();
    expect(screen.queryByText("https://unsafe.example/raw")).not.toBeInTheDocument();
    expect(window.location.search).toContain(`research_record_id=${RECORD_ID}`);

    await user.click(screen.getAllByRole("button", { name: "再次研究" })[0]);
    await waitFor(() => expect(onRerun).toHaveBeenCalledWith(expect.objectContaining({
      request_id: RERUN_ID,
      status: "queued",
      rerun_of: REQUEST_ID,
    })));
    expect(await screen.findByRole("status")).toHaveTextContent(`已创建新的研究请求 ${RERUN_ID}`);
    expect(screen.queryByRole("article", { name: "研究报告详情" })).not.toBeInTheDocument();
    const rerunCall = fetchMock.mock.calls.find(([input, init]) => path(input) === `/api/research/requests/${REQUEST_ID}/rerun` && init?.method === "POST");
    expect(JSON.parse(String(rerunCall?.[1]?.body)).submission_identity).toMatch(/^rerun-/);
  });
});
