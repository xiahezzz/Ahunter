import { mkdir, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { buildSelfTestReport } from "../src/self-test/build-report.mjs";

function option(name, fallback) {
  const index = process.argv.indexOf(name);
  return index < 0 ? fallback : process.argv[index + 1];
}

const tests = option("--tests", "tests/**/*.test.mjs").split(",");
const python = option(
  "--python",
  existsSync(".venv311/bin/python") ? ".venv311/bin/python" : "python3",
);
const pythonTests = option("--python-tests", "tests/advisor").split(",");
const output = option("--output", "reports/self-test");
const startedAt = new Date().toISOString();
const { NODE_TEST_CONTEXT: _nodeTestContext, ...env } = process.env;
const result = spawnSync(process.execPath, ["--test", ...tests], {
  encoding: "utf8",
  env,
});
const pythonResult = spawnSync(python, ["-m", "pytest", ...pythonTests], {
  encoding: "utf8",
  env,
});
const nodeStatus = result.status ?? 1;
const pythonStatus = pythonResult.status ?? 1;
const combinedResult = {
  status: nodeStatus === 0 && pythonStatus === 0 ? 0 : 1,
};
const report = buildSelfTestReport({
  startedAt,
  finishedAt: new Date().toISOString(),
  result: combinedResult,
});
await mkdir(output, { recursive: true });
await writeFile(path.join(output, "latest.json"), `${JSON.stringify(report, null, 2)}\n`);
await writeFile(
  path.join(output, "latest.md"),
  `# Self Test\n\n- Status: ${report.ok ? "PASS" : "FAIL"}\n- Started: ${startedAt}\n- Finished: ${report.finishedAt}\n`,
);
process.stdout.write(result.stdout);
process.stderr.write(result.stderr);
process.stdout.write(pythonResult.stdout);
process.stderr.write(pythonResult.stderr);
if (pythonResult.error) {
  process.stderr.write(`${pythonResult.error.message}\n`);
}
process.exitCode = combinedResult.status;
