import { describe, expect, it } from "vitest";
import { parseAgentAccess } from "./contracts";

describe("Research Agent contracts", () => {
  it.each([10, null])("accepts Instructions and optional query budgets: %s", (budget) => {
    const instructions = "核对 https://example.com/reference 后再分析。";
    expect(parseAgentAccess({
      agent_ref: "social@1",
      scope: "security",
      title: "资讯研究",
      summary: instructions,
      instructions,
      data_access: [{ product_ref: "identity@1" }],
      used_by_team_refs: [],
      unassigned_rids: [],
      blocked_reasons: [],
      read_only: { query_budget: budget, max_result_rows: budget, implementation: "declarative" },
    }).instructions).toBe(instructions);
  });
});
