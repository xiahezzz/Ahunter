import { mkdir, writeFile } from "node:fs/promises";
import { setTimeout as delay } from "node:timers/promises";
import { loadAllowedRids } from "../src/config/load-allowed-rids.mjs";
import { CdpClient } from "../src/ingestion/cdp-client.mjs";
import { findMxTarget } from "../src/ingestion/find-mx-target.mjs";

const checks = [];
async function check(name, action) {
  try {
    const details = await action();
    checks.push({ name, ok: true, ...details });
  } catch (error) {
    checks.push({ name, ok: false, errorClass: error instanceof Error ? error.name : "UnknownError" });
  }
}

await check("node", () => {
  const [major, minor] = process.versions.node.split(".").map(Number);
  if (major < 24 || (major === 24 && minor < 18)) throw new Error("UnsupportedNodeVersion");
  return {};
});
await check("rid-config", () => ({ active: loadAllowedRids("config/allowed-rids.yaml").size > 0 }));
await check("cdp-network-listener", async () => {
  const target = await findMxTarget("http://127.0.0.1:9222");
  const client = new CdpClient(target);
  let activity = false;
  let closeRequested = false;
  let unregister;
  try {
    unregister = client.onEvent((event) => {
      if (event.method === "Network.webSocketFrameReceived") activity = true;
    });
    await client.send("Network.enable");
    checks.push({ name: "listener-ready", ok: true });
    await delay(2_000);
    checks.push({ name: "websocket-activity", ok: true, observed: activity });
  } finally {
    unregister?.();
    if (!closeRequested) { closeRequested = true; client.close(); }
  }
  return { listenerReady: true, activityObserved: activity };
});

const report = { schemaVersion: 2, checkedAt: new Date().toISOString(), ok: checks.every(({ ok }) => ok), checks };
await mkdir("reports/self-test", { recursive: true });
await writeFile("reports/self-test/smoke-latest.json", `${JSON.stringify(report, null, 2)}\n`);
console.log(JSON.stringify(report, null, 2));
if (!report.ok) process.exitCode = 1;
