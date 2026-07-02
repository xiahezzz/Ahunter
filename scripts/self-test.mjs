import { mkdir, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { buildSelfTestReport } from "../src/self-test/build-report.mjs";

const startedAt = new Date().toISOString();
const result = spawnSync(process.execPath, ["--test", "tests/**/*.test.mjs"], {
  encoding: "utf8",
});
const report = buildSelfTestReport({
  startedAt,
  finishedAt: new Date().toISOString(),
  result,
});
await mkdir("reports/self-test", { recursive: true });
await writeFile("reports/self-test/latest.json", `${JSON.stringify(report, null, 2)}\n`);
await writeFile(
  "reports/self-test/latest.md",
  `# Self Test\n\n- Status: ${report.ok ? "PASS" : "FAIL"}\n- Started: ${startedAt}\n- Finished: ${report.finishedAt}\n`,
);
process.stdout.write(result.stdout);
process.stderr.write(result.stderr);
process.exitCode = result.status ?? 1;
