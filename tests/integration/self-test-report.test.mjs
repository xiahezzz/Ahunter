import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import test from "node:test";

test("self-test report mirrors nested test status without persisting test streams", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-self-test-"));
  const sensitiveMarker = "SENSITIVE_NESTED_TEST_STREAM_MARKER";

  for (const [name, assertion, expected] of [
    ["pass", "true", 0],
    ["fail", "false", 1],
  ]) {
    const fixture = path.join(directory, `${name}.test.mjs`);
    const output = path.join(directory, `${name}-report`);
    await writeFile(
      fixture,
      `import test from "node:test";
import assert from "node:assert/strict";
test("fixture", () => {
  console.log("${sensitiveMarker}");
  console.error("${sensitiveMarker}");
  assert.equal(${assertion}, true);
});
`,
    );

    const result = spawnSync(process.execPath, [
      "scripts/self-test.mjs",
      "--tests",
      fixture,
      "--output",
      output,
    ]);
    assert.equal(result.status, expected);

    const json = await readFile(path.join(output, "latest.json"), "utf8");
    const markdown = await readFile(path.join(output, "latest.md"), "utf8");
    const report = JSON.parse(json);
    assert.equal(report.schemaVersion, 1);
    assert.equal(report.ok, expected === 0);
    assert.match(markdown, expected === 0 ? /PASS/ : /FAIL/);
    assert.doesNotMatch(json, new RegExp(sensitiveMarker));
    assert.doesNotMatch(markdown, new RegExp(sensitiveMarker));
  }
});
