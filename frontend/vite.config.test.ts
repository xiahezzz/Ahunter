import { describe, expect, it } from "vitest";
import viteConfig from "./vite.config";


describe("Vite API proxy", () => {
  it("preserves the WebUI host for backend same-origin validation", () => {
    const config = viteConfig as {
      server?: { proxy?: Record<string, unknown> };
    };

    expect(config.server?.proxy?.["/api"]).toEqual({
      target: "http://127.0.0.1:8000",
      changeOrigin: false,
    });
  });
});
