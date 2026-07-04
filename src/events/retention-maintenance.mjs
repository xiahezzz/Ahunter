import { setTimeout as delayTimer } from "node:timers/promises";

const RETRY_DELAYS = [60_000, 120_000, 240_000, 480_000, 1_800_000];
const DAY = 86_400_000;

export function startRetentionMaintenance({
  store,
  now = () => Date.now(),
  delay = (ms, signal) => delayTimer(ms, undefined, { signal }),
  onReason = () => {},
  scheduleInterval = setInterval,
  clearInterval: cancelInterval = globalThis.clearInterval,
}) {
  const controller = new AbortController();
  let timer;

  async function maintain() {
    let attempt = 0;
    while (!controller.signal.aborted) {
      try {
        store.purgeExpiredPayloads(now());
        return;
      } catch (error) {
        const reason = error?.code === "checkpoint_busy" ? "checkpoint_busy" : "maintenance_failed";
        onReason(reason);
        if (reason !== "checkpoint_busy") return;
        try {
          await delay(RETRY_DELAYS[Math.min(attempt++, RETRY_DELAYS.length - 1)], controller.signal);
        } catch {
          return;
        }
      }
    }
  }

  const ready = maintain().then(() => {
    if (!controller.signal.aborted) {
      timer = scheduleInterval(() => maintain(), DAY);
      timer.unref?.();
    }
  });
  function stop() {
    controller.abort();
    cancelInterval(timer);
  }
  stop.ready = ready;
  return stop;
}
