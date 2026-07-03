#!/usr/bin/env node
import { loadAllowedRids } from "../src/config/load-allowed-rids.mjs";
import { openEventStore } from "../src/events/event-store.mjs";
import { Collector } from "../src/ingestion/collector.mjs";
import { runCollectorLoop } from "../src/ingestion/collector-runner.mjs";
import { processEventMedia } from "../src/media/process-event-media.mjs";

function option(name, fallback) {
  const index = process.argv.indexOf(name);
  return index < 0 ? fallback : process.argv[index + 1];
}

const cdpBase = option("--cdp", "http://127.0.0.1:9222");
const allowedRids = loadAllowedRids("config/allowed-rids.yaml");
const store = openEventStore("data/state/events.sqlite");
const collector = new Collector({
  allowedRids,
  store,
  onAccepted: (event) =>
    processEventMedia({ event, store, mediaRoot: "data/media" }),
});
const abortController = new AbortController();

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.once(signal, () => abortController.abort());
}

try {
  await runCollectorLoop({
    cdpBase,
    collector,
    signal: abortController.signal,
  });
} finally {
  store.close();
}
