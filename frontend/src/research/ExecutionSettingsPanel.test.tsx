import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ExecutionSettingsPanel from "./ExecutionSettingsPanel";

const current = { policy_ref: "codex@3", model: "gpt-5.6-sol", reasoning_effort: "medium", model_options: [
  { model: "gpt-5.6-sol", reasoning_efforts: ["low", "medium", "high", "ultra"], default_reasoning_effort: "low" },
  { model: "another-model", reasoning_efforts: ["medium", "high"], default_reasoning_effort: "medium" },
] };
function json(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }); }
afterEach(() => vi.unstubAllGlobals());

describe("research model settings", () => {
  it("ignores an older load that completes after a newer load and an edit", async () => {
    let finishFirst!: (response: Response) => void;
    const fetcher = vi.fn().mockImplementationOnce(() => new Promise<Response>((resolve) => { finishFirst = resolve; }))
      .mockResolvedValue(json(current));
    vi.stubGlobal("fetch", fetcher);
    const { rerender } = render(<ExecutionSettingsPanel refreshToken={0} />);
    rerender(<ExecutionSettingsPanel refreshToken={1} />);
    await waitFor(() => expect(screen.getByLabelText("推理强度")).toHaveValue("medium"));
    fireEvent.change(screen.getByLabelText("推理强度"), { target: { value: "high" } });
    finishFirst(json(current));
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
    expect(screen.getByLabelText("推理强度")).toHaveValue("high");
    expect(screen.getByRole("button", { name: "保存模型配置" })).toBeEnabled();
  });

  it("loads the active model and saves only an explicit edit", async () => {
    const fetcher = vi.fn(async (_path: unknown, init?: RequestInit) => init?.method === "PUT"
      ? json({ ...current, policy_ref: "codex@4", model: "another-model" }) : json(current));
    vi.stubGlobal("fetch", fetcher);
    render(<ExecutionSettingsPanel />);
    await waitFor(() => expect(screen.getByLabelText("模型名称")).toHaveValue("gpt-5.6-sol"));
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(screen.getAllByRole("combobox")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "保存模型配置" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("模型名称"), { target: { value: "another-model" } });
    fireEvent.click(screen.getByRole("button", { name: "保存模型配置" }));
    await screen.findByText("已保存，新开始的研究将使用 another-model，推理强度为 中（medium）。");
    expect(JSON.parse(String(fetcher.mock.calls[1][1]?.body))).toEqual({ expected_policy_ref: "codex@3", model: "another-model", reasoning_effort: "medium" });
  });

  it("shows a conflict without claiming success or discarding the draft", async () => {
    vi.stubGlobal("fetch", vi.fn(async (_path: unknown, init?: RequestInit) => init?.method === "PUT"
      ? json({ detail: "模型配置已更新，请刷新后重试" }, 409) : json(current)));
    render(<ExecutionSettingsPanel />);
    await waitFor(() => expect(screen.getByLabelText("模型名称")).toHaveValue("gpt-5.6-sol"));
    fireEvent.change(screen.getByLabelText("模型名称"), { target: { value: "another-model" } });
    fireEvent.click(screen.getByRole("button", { name: "保存模型配置" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("模型配置已更新");
    expect(screen.getByLabelText("模型名称")).toHaveValue("another-model");
  });

  it("saves an effort-only edit and retains it across queue refreshes", async () => {
    const fetcher = vi.fn(async (_path: unknown, init?: RequestInit) => init?.method === "PUT"
      ? json({ ...current, policy_ref: "codex@4", reasoning_effort: "high" }) : json(current));
    vi.stubGlobal("fetch", fetcher);
    const { rerender } = render(<ExecutionSettingsPanel refreshToken={0} />);
    await waitFor(() => expect(screen.getByRole("combobox", { name: "推理强度" })).toHaveValue("medium"));
    fireEvent.change(screen.getByRole("combobox", { name: "推理强度" }), { target: { value: "high" } });
    rerender(<ExecutionSettingsPanel refreshToken={1} />);
    expect(screen.getByRole("combobox", { name: "推理强度" })).toHaveValue("high");
    fireEvent.click(screen.getByRole("button", { name: "保存模型配置" }));
    expect(await screen.findByRole("status")).toHaveTextContent("高（high）");
    expect(JSON.parse(String(fetcher.mock.calls[1][1]?.body))).toEqual({
      expected_policy_ref: "codex@3", model: "gpt-5.6-sol", reasoning_effort: "high",
    });
  });

  it("adjusts unsupported effort on model change and preserves compatible effort", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(current)));
    render(<ExecutionSettingsPanel />);
    await waitFor(() => expect(screen.getByLabelText("模型名称")).toHaveValue("gpt-5.6-sol"));
    fireEvent.change(screen.getByLabelText("推理强度"), { target: { value: "ultra" } });
    fireEvent.change(screen.getByLabelText("模型名称"), { target: { value: "another-model" } });
    expect(screen.getByLabelText("推理强度")).toHaveValue("medium");
    expect(screen.queryByRole("option", { name: /ultra/ })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("推理强度"), { target: { value: "high" } });
    fireEvent.change(screen.getByLabelText("模型名称"), { target: { value: "gpt-5.6-sol" } });
    expect(screen.getByLabelText("推理强度")).toHaveValue("high");
  });

  it("preserves unsaved edits when the research queue refreshes", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(current)));
    const { rerender } = render(<ExecutionSettingsPanel refreshToken={0} />);
    await waitFor(() => expect(screen.getByLabelText("模型名称")).toHaveValue("gpt-5.6-sol"));
    fireEvent.change(screen.getByLabelText("模型名称"), { target: { value: "another-model" } });
    rerender(<ExecutionSettingsPanel refreshToken={1} />);
    expect(screen.getByLabelText("模型名称")).toHaveValue("another-model");
  });
});
