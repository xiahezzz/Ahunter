import { mkdir, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";

const startedAt = new Date().toISOString();
const result = spawnSync(process.execPath, ["--test"], {
  encoding: "utf8",
});
const report = {
  schemaVersion: 1,
  startedAt,
  finishedAt: new Date().toISOString(),
  ok: result.status === 0,
  exitCode: result.status,
  stdout: result.stdout,
  stderr: result.stderr,
};
await mkdir("reports/self-test", { recursive: true });
await writeFile("reports/self-test/latest.json", `${JSON.stringify(report, null, 2)}\n`);
await writeFile(
  "reports/self-test/latest.md",
  `# Self Test\n\n- Status: ${report.ok ? "PASS" : "FAIL"}\n- Started: ${startedAt}\n- Finished: ${report.finishedAt}\n`,
);
process.stdout.write(result.stdout);
process.stderr.write(result.stderr);
process.exitCode = result.status ?? 1;
