import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import MxOperationsPage from "./MxOperationsPage";

const VERSION = "a".repeat(64);

function json(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" }, ...init });
}

function path(input: RequestInfo | URL): string {
  return typeof input === "string" ? input : input.toString();
}

function status(loaded = false) {
  return {
    service_id: "mx-listener",
    status: loaded ? "运行中" : "未加载",
    details: loaded ? "正在等待用户提供已开启调试的 Chrome" : "MX Listener 未加载",
    launchagent_loaded: loaded,
    liveness: loaded ? "live" : "offline",
    readiness: loaded ? "waiting_for_chrome" : "stopping",
    health: loaded ? "healthy" : "failed",
    reason_code: loaded ? "waiting_for_chrome" : "not_started",
    connected_at: null,
    last_frame_at: null,
    last_accepted_event_at: null,
    lease_expires_at: null,
    rid_count: 1,
    collection_enabled: true,
    rid_config_valid: true,
  };
}

describe("MX operations page", () => {
  afterEach(() => vi.useRealTimers());

  beforeEach(() => {
    window.history.replaceState({}, "", "/mx");
    vi.stubGlobal("fetch", vi.fn());
  });

  it("uses explicit local lifecycle, dedicated Chrome, and full-set RID APIs", async () => {
    let loaded = false;
    let rids = [111];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = path(input);
      if (request === "/api/mx/listener/status") return json(status(loaded));
      if (request === "/api/mx/rids" && (!init?.method || init.method === "GET")) return json({ rids, version: VERSION, collection_enabled: rids.length > 0 });
      if (request === "/api/mx/listener/start" && init?.method === "POST") {
        loaded = true;
        return json({ changed: true, service: status(true), message: "已请求启动" });
      }
      if (request === "/api/mx/chrome/start" && init?.method === "POST") {
        return json({ changed: true, ready: true, message: "专用 Chrome 已启动；请在其中自行登录并打开 MX 页面" });
      }
      if (request === "/api/mx/rids" && init?.method === "PUT") {
        const body = JSON.parse(String(init.body)) as { rids: number[]; version: string };
        rids = body.rids;
        return json({ rids, version: VERSION, collection_enabled: rids.length > 0, message: "已更新" });
      }
      if (request.startsWith("/api/mx/events?")) return json({ events: [], next_cursor: null, limit: 50 });
      throw new Error(`unexpected ${request}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<MxOperationsPage />);

    expect(await screen.findByText("MX Listener 状态")).toBeInTheDocument();
    expect(screen.getAllByText("未加载")).toHaveLength(2);
    await user.click(screen.getByRole("button", { name: "启动 Listener" }));
    expect(await screen.findByText("已请求启动 MX Listener；状态已刷新")).toBeInTheDocument();
    expect(screen.getByText("点击“启动专用 Chrome”后，请在隔离窗口中自行登录并打开 MX 页面；系统不会代替你导航、登录或操作页面。")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "启动专用 Chrome" }));
    expect(await screen.findByText("专用 Chrome 已启动；请在其中自行登录并打开 MX 页面")).toBeInTheDocument();

    await user.type(screen.getByLabelText("新增 RID"), "222");
    await user.click(screen.getByRole("button", { name: "加入" }));
    await user.click(screen.getByRole("button", { name: "保存完整授权集合" }));
    expect(await screen.findByText("RID 授权已保存，Listener 会在下一次配置轮询时热加载")).toBeInTheDocument();

    const start = fetchMock.mock.calls.find(([input, init]) => path(input) === "/api/mx/listener/start" && init?.method === "POST");
    expect(start?.[1]).toMatchObject({ headers: { "Content-Type": "application/json" }, body: "{}" });
    const chrome = fetchMock.mock.calls.find(([input, init]) => path(input) === "/api/mx/chrome/start" && init?.method === "POST");
    expect(chrome?.[1]).toMatchObject({ headers: { "Content-Type": "application/json" }, body: "{}" });
    const saved = fetchMock.mock.calls.find(([input, init]) => path(input) === "/api/mx/rids" && init?.method === "PUT");
    expect(JSON.parse(String(saved?.[1]?.body))).toEqual({ rids: [111, 222], version: VERSION });
    expect(JSON.stringify(fetchMock.mock.calls)).not.toContain("127.0.0.1");
  });

  it("keeps a RID draft after a version conflict", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = path(input);
      if (request === "/api/mx/listener/status") return json(status());
      if (request === "/api/mx/rids" && (!init?.method || init.method === "GET")) return json({ rids: [111], version: VERSION, collection_enabled: true });
      if (request === "/api/mx/rids" && init?.method === "PUT") return json({ detail: "RID 配置已更新，请刷新后再提交" }, { status: 409 });
      if (request.startsWith("/api/mx/events?")) return json({ events: [], next_cursor: null, limit: 50 });
      throw new Error(`unexpected ${request}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<MxOperationsPage />);
    await screen.findByText("RID Authorization Set");
    await user.type(screen.getByLabelText("新增 RID"), "222");
    await user.click(screen.getByRole("button", { name: "加入" }));
    await user.click(screen.getByRole("button", { name: "保存完整授权集合" }));

    expect(await screen.findByText("RID 配置已更新，请刷新后比较并重新提交")).toBeInTheDocument();
    expect(screen.getByText("222")).toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  });
});
