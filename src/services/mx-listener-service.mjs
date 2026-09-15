import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { watchAllowedRids } from "../config/watch-allowed-rids.mjs";
import { startRetentionMaintenance } from "../events/retention-maintenance.mjs";
import { openEventStore } from "../events/event-store.mjs";
import { Collector } from "../ingestion/collector.mjs";
import { runCollectorLoop } from "../ingestion/collector-runner.mjs";
import { drainMediaJobs } from "../media/process-event-media.mjs";
import { openListenerControlPlane } from "./listener-control-plane.mjs";
import { MacIdleSleepInhibitor } from "./sleep-inhibition.mjs";

export const NODE24 = "/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node";
const REPOSITORY_ROOT = fileURLToPath(new URL("../..", import.meta.url));

const STATE = Object.freeze({
  starting: ["starting", "healthy", "starting"],
  waiting_for_chrome: ["waiting_for_chrome", "healthy", "chrome_unavailable"],
  waiting_for_authorization: ["waiting_for_authorization", "healthy", "authorization_required"],
  connecting: ["connecting", "healthy", "connecting"],
  listening: ["listening", "healthy", "network_enabled"],
  stopping: ["stopping", "healthy", "shutdown_requested"],
});

/**
 * One long-running, passive MX listener owner.  It does not start, navigate,
 * reload, click, type into, or inject code into Chrome or the MX page.
 */
export class MxListenerService {
  constructor({
    root = REPOSITORY_ROOT,
    eventsDbPath,
    allowedRidsPath,
    mediaRoot,
    cdpBase = "http://127.0.0.1:9333",
    instanceId = `mx-listener-${randomUUID()}`,
    ttlMs = 30_000,
    clock = () => Date.now(),
    nodeBinary = NODE24,
    runSelfTest = defaultSelfTest,
    openControlPlane = openListenerControlPlane,
    openStore = openEventStore,
    watchRids = watchAllowedRids,
    createCollector = (options) => new Collector(options),
    runLoop = runCollectorLoop,
    drainMedia = drainMediaJobs,
    startMaintenance = startRetentionMaintenance,
    sleepInhibitor = new MacIdleSleepInhibitor(),
    isPersistentStoreFailure = defaultPersistentStoreFailure,
    log = (message) => process.stderr.write(`${message}\n`),
  } = {}) {
    this.root = path.resolve(String(root));
    this.eventsDbPath = eventsDbPath ?? path.join(this.root, "data/state/events.sqlite");
    this.allowedRidsPath = allowedRidsPath ?? path.join(this.root, "config/allowed-rids.yaml");
    // The collector's durable media metadata is repository-relative.  The
    // LaunchAgent runs with `root` as its working directory, so preserving
    // this form keeps old accepted-event readers and safe media routing valid.
    this.mediaRoot = mediaRoot ?? "data/media";
    this.cdpBase = cdpBase;
    this.instanceId = instanceId;
    this.ttlMs = ttlMs;
    this.clock = clock;
    this.nodeBinary = nodeBinary;
    this.runSelfTest = runSelfTest;
    this.openControlPlane = openControlPlane;
    this.openStore = openStore;
    this.watchRids = watchRids;
    this.createCollector = createCollector;
    this.runLoop = runLoop;
    this.drainMedia = drainMedia;
    this.startMaintenance = startMaintenance;
    this.sleepInhibitor = sleepInhibitor;
    this.isPersistentStoreFailure = isPersistentStoreFailure;
    this.log = log;
    this.running = false;
  }

  async run({ signal } = {}) {
    if (this.running) throw new Error("listener service is already running");
    this.running = true;
    let plane;
    let store;
    let watcher;
    let stopMaintenance = () => {};
    let heartbeatTimer;
    let mediaDrain;
    let leaseHeld = false;
    let outcome = { started: false, code: "stopped" };
    const controller = new AbortController();
    const workController = new AbortController();
    const state = { readiness: "starting", health: "healthy", reasonCode: "starting" };

    const stopFromOutside = () => controller.abort(signal?.reason ?? new Error("shutdown_requested"));
    if (signal?.aborted) stopFromOutside();
    else signal?.addEventListener("abort", stopFromOutside, { once: true });

    const transition = (nextReadiness, health, reasonCode, activity = {}) => {
      if (!plane || !leaseHeld || controller.signal.aborted && nextReadiness !== "stopping") return;
      const enteringListening = state.readiness !== "listening" && nextReadiness === "listening";
      if (state.readiness === "listening" && nextReadiness !== "listening") {
        this.sleepInhibitor.release();
      }
      const now = this.clock();
      const nextActivity = nextReadiness === "listening" && activity.connectedAt === undefined
        ? { ...activity, connectedAt: now }
        : activity;
      plane.transition(this.instanceId, {
        readiness: nextReadiness,
        health,
        reasonCode,
      }, { at: now, activity: nextActivity });
      state.readiness = nextReadiness;
      state.health = health;
      state.reasonCode = reasonCode;
      if (enteringListening && !this.sleepInhibitor.acquire()) {
        plane.transition(this.instanceId, {
          readiness: nextReadiness,
          health: "degraded",
          reasonCode: "sleep_inhibition_unavailable",
        }, { at: now, activity: nextActivity });
        state.health = "degraded";
        state.reasonCode = "sleep_inhibition_unavailable";
      }
    };

    const fatal = (reasonCode) => {
      if (controller.signal.aborted) return;
      try {
        transition(state.readiness, "failed", reasonCode);
      } catch {
        // A broken state database cannot safely be represented as healthy.
      }
      controller.abort(new Error(reasonCode));
    };

    const noteActivity = (activity) => {
      try {
        transition(state.readiness, state.health, state.reasonCode, activity);
      } catch {
        fatal("heartbeat_failed");
      }
    };

    const startMediaDrain = (activeSignal = workController.signal) => {
      if (mediaDrain) return mediaDrain;
      mediaDrain = Promise.resolve(this.drainMedia({
        store,
        mediaRoot: this.mediaRoot,
        signal: activeSignal,
        now: this.clock,
      })).catch((error) => {
        if (this.isPersistentStoreFailure(error)) fatal("event_store_unwritable");
        else {
          try { store.incrementCounter("media_failed", this.clock()); } catch { fatal("event_store_unwritable"); }
        }
      }).finally(() => { mediaDrain = undefined; });
      return mediaDrain;
    };

    try {
      const selfTestPassed = await Promise.resolve(this.runSelfTest({
        root: this.root,
        nodeBinary: this.nodeBinary,
      }));
      if (!selfTestPassed) {
        outcome = { started: false, code: "self_test_failed" };
        return outcome;
      }

      plane = this.openControlPlane(this.eventsDbPath, { ttlMs: this.ttlMs, now: this.clock });
      const claim = plane.claim(this.instanceId, { at: this.clock() });
      if (!claim.claimed) {
        outcome = { started: false, code: "already_running" };
        return outcome;
      }
      leaseHeld = true;
      store = this.openStore(this.eventsDbPath);
      transition(...STATE.starting);
      watcher = this.watchRids(this.allowedRidsPath, {
        onError: (reason) => {
          try {
            transition(state.readiness, "degraded", reason);
            store.incrementCounter(reason, this.clock());
          } catch { fatal("event_store_unwritable"); }
        },
        onReload: () => {
          if (state.health === "degraded" && ["config_invalid", "config_unreadable"].includes(state.reasonCode)) {
            try { transition(state.readiness, "healthy", STATE[state.readiness]?.[2] ?? "config_reloaded"); } catch { fatal("heartbeat_failed"); }
          }
        },
      });

      const collector = this.createCollector({
        allowedRids: () => watcher.current,
        store,
        now: this.clock,
        onAccepted: async (_event, options) => {
          noteActivity({ lastAcceptedEventAt: this.clock() });
          await startMediaDrain(options?.signal);
        },
      });
      collector.recordOverflow = (reason) => {
        try { store.incrementCounter(reason, this.clock()); } catch { fatal("event_store_unwritable"); }
      };
      stopMaintenance = this.startMaintenance({
        store,
        now: this.clock,
        onReason: (reason) => {
          try {
            store.incrementCounter(reason, this.clock());
            transition(state.readiness, "degraded", reason);
          } catch { fatal("event_store_unwritable"); }
        },
      });
      void startMediaDrain();
      heartbeatTimer = setInterval(() => {
        try {
          plane.heartbeat(this.instanceId, { at: this.clock() });
        } catch {
          fatal("heartbeat_failed");
        }
      }, Math.max(1_000, Math.floor(this.ttlMs / 3)));
      heartbeatTimer.unref?.();

      await this.runLoop({
        cdpBase: this.cdpBase,
        collector,
        signal: controller.signal,
        workController,
        keepWaitingForAuthorization: true,
        onState: (name) => {
          const next = STATE[name];
          if (next) transition(...next);
        },
        onFrame: () => noteActivity({ lastFrameAt: this.clock() }),
        onFrameError: (error) => {
          if (this.isPersistentStoreFailure(error)) fatal("event_store_unwritable");
        },
        log: this.log,
      });
      outcome = { started: true, code: controller.signal.aborted ? "stopped" : "loop_finished" };
    } catch {
      fatal("service_failed");
      outcome = { started: leaseHeld, code: "service_failed" };
    } finally {
      clearInterval(heartbeatTimer);
      this.sleepInhibitor.release();
      try { stopMaintenance(); } catch { /* no state change during shutdown */ }
      try { watcher?.close(); } catch { /* already fail-closed */ }
      if (mediaDrain) await Promise.allSettled([mediaDrain]);
      if (plane && leaseHeld) {
        try { transition("stopping", state.health, "shutdown_requested"); } catch { /* DB may be the failure */ }
        try { plane.release(this.instanceId, { at: this.clock() }); } catch { /* TTL fails closed */ }
      }
      try { store?.close(); } catch { /* cannot make a failed store healthy */ }
      try { plane?.close(); } catch { /* no further recovery in this process */ }
      signal?.removeEventListener("abort", stopFromOutside);
      this.running = false;
    }
    return outcome;
  }
}

export async function runMxListenerService(options, runOptions) {
  return new MxListenerService(options).run(runOptions);
}

export function defaultSelfTest({ root, nodeBinary = NODE24 }) {
  const environment = { ...process.env, NO_PROXY: "*", no_proxy: "*" };
  for (const key of ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]) {
    delete environment[key];
  }
  try {
    return spawnSync(nodeBinary, ["scripts/self-test.mjs"], {
      cwd: root,
      env: environment,
      encoding: "utf8",
      timeout: 600_000,
    }).status === 0;
  } catch {
    return false;
  }
}

function defaultPersistentStoreFailure(error) {
  const code = String(error?.code ?? error?.name ?? "");
  return /sqlite|database|read.?only|disk/i.test(code);
}
