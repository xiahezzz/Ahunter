import { fireEvent, render, screen, waitFor, cleanup } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import LAgentPanel from "./LAgentPanel";

const config = { main: { instructions: "主研究", model: null, reasoning_effort: null, timeout_seconds: 900, max_retries: 1 },
  subagent: { instructions: "子研究", model: null, reasoning_effort: null, timeout_seconds: 900, max_retries: 1 },
  allowed_products: null, mx_rids: null, prefetch_intraday: true, max_steps_per_agent: null, max_subagents: null, max_queries: null, max_result_rows: null, max_result_bytes: null, max_run_seconds: null };
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("publishes configuration and submits a free research task without a Team", async () => {
  const request = { mode: "lagent", request_id: `request-${"a".repeat(32)}`, team_ref: "lagent@2", scope: "market", subject: { scope: "market", code: null, name: null }, origin: "web", requested_at: "2026-09-08T12:00:00Z", accepted_at: "2026-09-08T12:00:00Z", boundary_at: null, status: "queued", phase: "queued", agents_completed: 0, agents_total: 0, decision_stage: null, reason_code: null, published_at: null, rerun_of: null, last_updated_at: "2026-09-08T12:00:00Z", can_cancel: true, can_rerun: false };
  const submitted = vi.fn();
  const calls: { path: string; body: Record<string, unknown> }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (path: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    if (init?.method) calls.push({ path, body });
    if (path.includes("/records")) return { ok: true, json: async () => ({ records: [] }) };
    if (path.endsWith("/settings")) return { ok: true, json: async () => ({ version: init?.method ? 1 : 0, config: body.config ?? config }) };
    if (path.endsWith("/requests")) return { ok: true, json: async () => ({ request }) };
    if (path.includes("/trace")) return { ok: true, json: async () => ({ request: { ...request, status: "cancelled" }, task: "研究市场结构", config, config_version: 1, products: [], events: [], next_after: 0 }) };
    return { ok: false, json: async () => ({ detail: "fixture" }) };
  }));
  render(<LAgentPanel onSubmitted={submitted} />);
  await screen.findByText("发布 LAgent 配置");
  fireEvent.change(screen.getByLabelText("main 研究指令"), { target: { value: "新指令" } });
  fireEvent.click(screen.getByText("发布 LAgent 配置"));
  await waitFor(() => expect(calls[0]?.body.expected_version).toBe(0));
  expect((calls[0].body.config as typeof config).main.instructions).toBe("新指令");
  await screen.findByText("LAgent 配置已发布：版本 1");
  fireEvent.change(screen.getByLabelText("LAgent 研究任务"), { target: { value: "研究市场结构" } });
  fireEvent.click(screen.getByText("提交 LAgent 研究"));
  await waitFor(() => expect(calls[1]?.body.task).toBe("研究市场结构"));
  expect(calls[1].body).toMatchObject({ expected_version: 1, scope: "market", code: null });
  expect(calls[1].body).not.toHaveProperty("team_ref");
  await waitFor(() => expect(submitted).toHaveBeenCalledWith(expect.objectContaining({ mode: "lagent" })));
});

it("preserves unpublished changes and blocks submission until publication", async () => {
  const fetch = vi.fn(async (path: string) => ({ ok: true, json: async () => path.includes("/records") ? { records: [] } : { version: 0, config } }));
  vi.stubGlobal("fetch", fetch);
  const view = render(<LAgentPanel refreshToken={0} onSubmitted={() => {}} />);
  await screen.findByText("发布 LAgent 配置");
  fireEvent.change(screen.getByLabelText("main 研究指令"), { target: { value: "未发布" } });
  view.rerender(<LAgentPanel refreshToken={1} onSubmitted={() => {}} />);
  fireEvent.change(screen.getByLabelText("LAgent 研究任务"), { target: { value: "研究" } });
  fireEvent.click(screen.getByText("提交 LAgent 研究"));
  expect(await screen.findByText("请先发布配置，再提交研究。")).toBeTruthy();
  expect((screen.getByLabelText("main 研究指令") as HTMLTextAreaElement).value).toBe("未发布");
  expect(fetch.mock.calls.every(([path]) => !path.endsWith("/requests"))).toBe(true);
});
