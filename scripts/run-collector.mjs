#!/usr/bin/env node
import { watchAllowedRids } from "../src/config/watch-allowed-rids.mjs";
import { openEventStore } from "../src/events/event-store.mjs";
import { Collector } from "../src/ingestion/collector.mjs";
import { runCollectorLoop } from "../src/ingestion/collector-runner.mjs";
import { drainMediaJobs } from "../src/media/process-event-media.mjs";
import { startRetentionMaintenance } from "../src/events/retention-maintenance.mjs";

function option(name, fallback) {
  const index = process.argv.indexOf(name);
  return index < 0 ? fallback : process.argv[index + 1];
}

const cdpBase = option("--cdp", "http://127.0.0.1:9222");
const store = openEventStore("data/state/events.sqlite");
const ridConfig = watchAllowedRids("config/allowed-rids.yaml", {
  onError: (reason) => store.incrementCounter(reason, Date.now()),
});
const abortController = new AbortController();
let mediaDrain;
function drainPersistedMedia() {
  if (!mediaDrain) {
    mediaDrain = drainMediaJobs({
      store, mediaRoot: "data/media", signal: abortController.signal,
    }).finally(() => { mediaDrain = undefined; });
  }
  return mediaDrain;
}
const collector = new Collector({
  allowedRids: () => ridConfig.current,
  store,
  onAccepted: () => drainPersistedMedia(),
});
collector.recordOverflow = (reason) => store.incrementCounter(reason, Date.now());
const stopMaintenance = startRetentionMaintenance({
  store,
  onReason: (reason) => store.incrementCounter(reason, Date.now()),
});
await drainMediaJobs({ store, mediaRoot: "data/media", signal: abortController.signal });
const mediaRetryTimer = setInterval(() => void drainPersistedMedia(), 60_000);
mediaRetryTimer.unref?.();

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
  clearInterval(mediaRetryTimer);
  stopMaintenance();
  ridConfig.close();
  store.close();
}
