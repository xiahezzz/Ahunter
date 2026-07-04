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
const abortController = new AbortController();
const workController = new AbortController();
let shutdownDeadline;
for (const signal of ["SIGINT", "SIGTERM"]) {
  process.once(signal, () => {
    if (abortController.signal.aborted) return;
    abortController.abort();
    shutdownDeadline = setTimeout(() => {
      workController.abort(new Error("Collector shutdown deadline exceeded"));
    }, 30_000);
    shutdownDeadline.unref?.();
  });
}
const store = openEventStore("data/state/events.sqlite");
const ridConfig = watchAllowedRids("config/allowed-rids.yaml", {
  onError: (reason) => store.incrementCounter(reason, Date.now()),
});
let mediaDrain;
function drainPersistedMedia(signal = workController.signal) {
  if (!mediaDrain) {
    mediaDrain = drainMediaJobs({
      store, mediaRoot: "data/media", signal,
    }).finally(() => { mediaDrain = undefined; });
  }
  return mediaDrain;
}
const collector = new Collector({
  allowedRids: () => ridConfig.current,
  store,
  onAccepted: (_event, { signal }) => drainPersistedMedia(signal),
});
collector.recordOverflow = (reason) => store.incrementCounter(reason, Date.now());
let stopMaintenance = () => {};
let mediaRetryTimer;
try {
  stopMaintenance = startRetentionMaintenance({
    store,
    onReason: (reason) => store.incrementCounter(reason, Date.now()),
  });
  await drainMediaJobs({ store, mediaRoot: "data/media", signal: workController.signal });
  if (!abortController.signal.aborted) {
    mediaRetryTimer = setInterval(() => void drainPersistedMedia(), 60_000);
    mediaRetryTimer.unref?.();
  }
  await runCollectorLoop({
    cdpBase,
    collector,
    signal: abortController.signal,
    workController,
  });
} finally {
  clearTimeout(shutdownDeadline);
  clearInterval(mediaRetryTimer);
  stopMaintenance();
  if (mediaDrain) await Promise.allSettled([mediaDrain]);
  try {
    ridConfig.close();
  } finally {
    store.close();
  }
}
