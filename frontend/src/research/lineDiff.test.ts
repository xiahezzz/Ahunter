import { describe, expect, it } from "vitest";
import { diffLines } from "./lineDiff";

describe("line diff", () => {
  it("keeps unchanged islands while identifying removed and added lines", () => {
    expect(diffLines("one\ntwo\nthree", "one\nchanged\nthree")).toEqual([
      { kind: "same", text: "one", oldLine: 1, newLine: 1 },
      { kind: "removed", text: "two", oldLine: 2, newLine: null },
      { kind: "added", text: "changed", oldLine: null, newLine: 2 },
      { kind: "same", text: "three", oldLine: 3, newLine: 3 },
    ]);
  });
});
