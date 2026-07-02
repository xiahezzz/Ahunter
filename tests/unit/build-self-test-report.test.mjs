import assert from "node:assert/strict";
import test from "node:test";
import { buildSelfTestReport } from "../../src/self-test/build-report.mjs";

test("persisted self-test report omits raw test streams", () => {
  const sensitiveMarker = "SENSITIVE_TEST_STREAM_MARKER";
  const report = buildSelfTestReport({
    startedAt: "2026-07-03T00:00:00.000Z",
    finishedAt: "2026-07-03T00:00:01.000Z",
    result: {
      status: 0,
      stdout: sensitiveMarker,
      stderr: sensitiveMarker,
    },
  });

  assert.deepEqual(report, {
    schemaVersion: 1,
    startedAt: "2026-07-03T00:00:00.000Z",
    finishedAt: "2026-07-03T00:00:01.000Z",
    ok: true,
    exitCode: 0,
  });
  assert.doesNotMatch(JSON.stringify(report), /SENSITIVE_TEST_STREAM_MARKER/);
});
