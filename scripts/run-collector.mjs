#!/usr/bin/env node
// Legacy manual command retained as a thin entry point.  It deliberately uses
// the same MxListenerService as the LaunchAgent, not a second collector loop.
import { runMxListenerService } from "../src/services/mx-listener-service.mjs";

function option(name, fallback) {
  const index = process.argv.indexOf(name);
  return index < 0 ? fallback : process.argv[index + 1];
}

const controller = new AbortController();
for (const name of ["SIGINT", "SIGTERM"]) {
  process.once(name, () => controller.abort(new Error("shutdown_requested")));
}

const result = await runMxListenerService({
  root: process.cwd(),
  cdpBase: option("--cdp", "http://127.0.0.1:9222"),
}, { signal: controller.signal });
if (result.code === "self_test_failed" || result.code === "service_failed") process.exitCode = 1;
