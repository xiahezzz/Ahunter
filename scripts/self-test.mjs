import { mkdir, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { buildSelfTestReport } from "../src/self-test/build-report.mjs";

function option(name, fallback) {
  const index = process.argv.indexOf(name);
  return index < 0 ? fallback : process.argv[index + 1];
}

const tests = option("--tests", "tests/**/*.test.mjs").split(",");
const output = option("--output", "reports/self-test");
const startedAt = new Date().toISOString();
const { NODE_TEST_CONTEXT: _nodeTestContext, ...env } = process.env;
const result = spawnSync(process.execPath, ["--test", ...tests], {
  encoding: "utf8",
  env,
});
const report = buildSelfTestReport({
  startedAt,
  finishedAt: new Date().toISOString(),
  result,
});
await mkdir(output, { recursive: true });
await writeFile(path.join(output, "latest.json"), `${JSON.stringify(report, null, 2)}\n`);
await writeFile(
  path.join(output, "latest.md"),
  `# Self Test\n\n- Status: ${report.ok ? "PASS" : "FAIL"}\n- Started: ${startedAt}\n- Finished: ${report.finishedAt}\n`,
);
process.stdout.write(result.stdout);
process.stderr.write(result.stderr);
process.exitCode = result.status ?? 1;
