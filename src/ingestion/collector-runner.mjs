import { setTimeout as timerDelay } from "node:timers/promises";
import { CdpClient } from "./cdp-client.mjs";
import { createMxFrameRouter } from "./cdp-frame-router.mjs";
import { findMxTarget } from "./find-mx-target.mjs";

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
}) {
  const pending = new Set();
  let client = null;
  let attempt = 0;
  const closeClient = () => client?.close();
  signal.addEventListener("abort", closeClient);

  try {
    while (!signal.aborted) {
      try {
        const targetUrl = await findTarget(cdpBase);
        if (signal.aborted) break;

        client = createClient(targetUrl);
        const route = createRouter({
          onFrame: (frame) => {
            const task = Promise.resolve()
              .then(() => collector.acceptFrame(frame))
              .catch((error) => {
                log(`Frame ingestion failed (${errorName(error)})`);
              })
              .finally(() => pending.delete(task));
            pending.add(task);
          },
        });
        client.onEvent(route);
        await client.send("Network.enable");
        attempt = 0;
        await client.closed;
      } catch (error) {
        if (!signal.aborted) {
          log(`Collector connection failed (${errorName(error)})`);
        }
      } finally {
        client?.close();
        client = null;
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
    signal.removeEventListener("abort", closeClient);
    client?.close();
    await Promise.allSettled(pending);
  }
}
