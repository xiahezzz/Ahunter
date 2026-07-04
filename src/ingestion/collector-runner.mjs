import { setTimeout as timerDelay } from "node:timers/promises";
import { CdpClient } from "./cdp-client.mjs";
import { createMxFrameRouter } from "./cdp-frame-router.mjs";
import { findMxTarget } from "./find-mx-target.mjs";
import { AuthorizationRequiredError } from "./find-mx-target.mjs";

const BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 30_000];

function errorName(error) {
  return error instanceof Error ? error.name : "UnknownError";
}

export async function runCollectorLoop({
  cdpBase,
  collector,
  signal,
  findTarget = findMxTarget,
  createClient = (url) => new CdpClient(url),
  createRouter = createMxFrameRouter,
  delay = (milliseconds, abortSignal) =>
    timerDelay(milliseconds, undefined, { signal: abortSignal }),
  log = (message) => process.stderr.write(`${message}\n`),
  maxConcurrentFrames = 4,
  maxQueuedFrames = 100,
  drainTimeoutMs = 30_000,
}) {
  const pending = new Set();
  const queued = [];
  let activeClient = null;
  let attempt = 0;
  const closeActiveClient = () => {
    if (!activeClient || activeClient.closeRequested) return;
    activeClient.closeRequested = true;
    try {
      activeClient.client.close();
    } catch (error) {
      log(`Collector client close failed (${errorName(error)})`);
    }
  };
  signal.addEventListener("abort", closeActiveClient);

  const startFrame = (frame) => {
    const task = Promise.resolve()
      .then(() => collector.acceptFrame(frame))
      .catch((error) => log(`Frame ingestion failed (${errorName(error)})`))
      .finally(() => {
        pending.delete(task);
        const next = queued.shift();
        if (next) startFrame(next);
      });
    pending.add(task);
  };

  const enqueueFrame = (frame) => {
    if (pending.size < maxConcurrentFrames) startFrame(frame);
    else if (queued.length < maxQueuedFrames) queued.push(frame);
    else collector.recordOverflow?.("frame_queue_overflow");
  };

  try {
    while (!signal.aborted) {
      let unregister;
      try {
        const targetUrl = await findTarget(cdpBase);
        if (signal.aborted) break;

        const client = createClient(targetUrl);
        activeClient = { client, closeRequested: false };
        const route = createRouter({ onFrame: enqueueFrame });
        unregister = client.onEvent(route);
        await client.send("Network.enable");
        attempt = 0;
        await client.closed;
      } catch (error) {
        if (error instanceof AuthorizationRequiredError || error?.code === "authorization_required") {
          log("authorization_required");
          return;
        }
        if (!signal.aborted) {
          log(`Collector connection failed (${errorName(error)})`);
        }
      } finally {
        unregister?.();
        closeActiveClient();
        activeClient = null;
      }

      if (!signal.aborted) {
        try {
          await delay(BACKOFF_MS[Math.min(attempt++, BACKOFF_MS.length - 1)], signal);
        } catch (error) {
          if (!signal.aborted) throw error;
        }
      }
    }
  } finally {
    signal.removeEventListener("abort", closeActiveClient);
    closeActiveClient();
    queued.length = 0;
    if (pending.size) {
      const drainTimer = new AbortController();
      try {
        await Promise.race([
          Promise.allSettled([...pending]),
          timerDelay(drainTimeoutMs, undefined, { signal: drainTimer.signal }),
        ]);
      } finally {
        drainTimer.abort();
      }
    }
  }
}
