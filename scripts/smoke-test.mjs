import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { loadAllowedRids } from "../src/config/load-allowed-rids.mjs";
import { openEventStore } from "../src/events/event-store.mjs";
import { findMxTarget } from "../src/ingestion/find-mx-target.mjs";

const checks = [];

async function check(name, action) {
  try {
    await action();
    checks.push({ name, ok: true });
  } catch (error) {
    checks.push({ name, ok: false, error: error.message });
  }
}

await check("node", () => {
  const [major, minor] = process.versions.node.split(".").map(Number);
  if (major < 24 || (major === 24 && minor < 18)) {
    throw new Error(`Node 24.18+ required, got ${process.versions.node}`);
  }
});

await check("rid-config", () => {
  const values = loadAllowedRids("config/allowed-rids.yaml");
  checks.push({ name: "rid-active", ok: true, active: values.size > 0 });
});

await check("chrome-target", () => findMxTarget("http://127.0.0.1:9222"));

await check("sqlite", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-smoke-"));
  const store = openEventStore(path.join(directory, "events.sqlite"));
  try {
    store.incrementCounter("smoke", Date.now());
  } finally {
    store.close();
  }
});

const report = {
  schemaVersion: 1,
  checkedAt: new Date().toISOString(),
  ok: checks.every(({ ok }) => ok),
  checks,
};
await mkdir("reports/self-test", { recursive: true });
await writeFile(
  "reports/self-test/smoke-latest.json",
  `${JSON.stringify(report, null, 2)}\n`,
);
console.log(JSON.stringify(report, null, 2));
if (!report.ok) process.exitCode = 1;
