import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import MxInformationFeed from "./MxInformationFeed";

const EVENT_A = "abcdefgh.abcdefghijklmnop";
const EVENT_B = "ijklmnop.abcdefghijklmnop";
const MEDIA = "qrstuvwx.abcdefghijklmnop";

function json(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" }, ...init });
}

function path(input: RequestInfo | URL): string {
  return typeof input === "string" ? input : input.toString();
}

function item(eventId: string, rid = 111) {
  return {
    event_id: eventId,
    rid,
    authorization: rid === 111 ? "current" : "revoked",
    received_at: "2026-08-08T11:00:00+08:00",
    source_created_at: "2026-08-08T10:59:00+08:00",
    content_hash: "a".repeat(64),
    summary: "规范化资讯正文",
    media: { available_count: 1, pending_count: 0, has_media: true },
  };
}

describe("MX information feed", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/mx");
    vi.stubGlobal("fetch", vi.fn());
  });

  it("keeps composed filters in the URL, loads a keyset page, and opens only opaque media", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const request = path(input);
      if (request.startsWith("/api/mx/events?") && request.includes("cursor=older")) {
        return json({ events: [item(EVENT_B, 222)], next_cursor: null, limit: 50 });
      }
      if (request.startsWith("/api/mx/events?")) return json({ events: [item(EVENT_A)], next_cursor: "olderxxx.abcdefghijklmnop", limit: 50 });
      if (request === `/api/mx/events/${encodeURIComponent(EVENT_A)}`) {
        return json({
          event_id: EVENT_A,
          rid: 111,
          authorization: "current",
          received_at: "2026-08-08T11:00:00+08:00",
          source_created_at: "2026-08-08T10:59:00+08:00",
          content_hash: "a".repeat(64),
          blocks: [
            { type: "text", text: "详情中的规范化正文" },
            { type: "media", media_id: MEDIA, content_hash: "b".repeat(64), content_type: "image/jpeg", href: `/api/mx/events/${EVENT_A}/media/${MEDIA}` },
          ],
        });
      }
      throw new Error(`unexpected ${request}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<MxInformationFeed />);

    expect(await screen.findByText("规范化资讯正文")).toBeInTheDocument();
    await user.clear(screen.getByLabelText("RID 筛选"));
    await user.type(screen.getByLabelText("RID 筛选"), "111,222");
    await user.selectOptions(screen.getByLabelText("授权状态"), "current");
    await user.selectOptions(screen.getByLabelText("图片筛选"), "true");
    await user.type(screen.getByLabelText("正文关键词"), "市场");
    await user.click(screen.getByRole("button", { name: "应用筛选" }));

    await waitFor(() => expect(window.location.search).toContain("rid=111"));
    expect(window.location.search).toContain("rid=222");
    expect(window.location.search).toContain("authorization=current");
    expect(window.location.search).toContain("has_media=true");
    expect(window.location.search).toContain("q=%E5%B8%82%E5%9C%BA");

    await user.click(screen.getByRole("button", { name: "加载更早资讯" }));
    expect(await screen.findByText("RID 222")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /RID 111/ }));
    expect(await screen.findByText("详情中的规范化正文")).toBeInTheDocument();
    const image = screen.getByRole("img", { name: "MX 资讯图片" });
    expect(image).toHaveAttribute("src", `/api/mx/events/${EVENT_A}/media/${MEDIA}`);
    expect(document.body.textContent).not.toContain("source_url");
    expect(document.body.textContent).not.toContain("raw_payload");
  });

  it("resets malformed URL filters without sending unsafe query parameters", async () => {
    window.history.replaceState({}, "", "/mx?rid=0&q=https%3A%2F%2Funsafe.invalid");
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const request = path(input);
      if (request.startsWith("/api/mx/events?")) return json({ events: [], next_cursor: null, limit: 50 });
      throw new Error(`unexpected ${request}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<MxInformationFeed />);

    expect(await screen.findByText("URL 筛选条件无效，已安全重置")).toBeInTheDocument();
    expect(window.location.search).toBe("");
    expect(fetchMock.mock.calls.map(([input]) => path(input)).join("\n")).not.toContain("unsafe.invalid");
  });
});
