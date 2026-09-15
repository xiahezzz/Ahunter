import assert from "node:assert/strict";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { AuthorizationRequiredError } from "../../src/ingestion/find-mx-target.mjs";
import { runCollectorLoop } from "../../src/ingestion/collector-runner.mjs";
import { openListenerControlPlane } from "../../src/services/listener-control-plane.mjs";
import { MxListenerService } from "../../src/services/mx-listener-service.mjs";

function clock(initial = 2_500_000) {
  let value = initial;
  return {
    now: () => value,
    advance: (milliseconds = 1) => { value += milliseconds; },
  };
}

async function fixture() {
  const root = await mkdtemp(path.join(os.tmpdir(), "a-hunter-mx-e2e-"));
  const eventsDbPath = path.join(root, "state", "events.sqlite");
  const allowedRidsPath = path.join(root, "config", "allowed-rids.yaml");
  await mkdir(path.dirname(allowedRidsPath), { recursive: true });
  await writeFile(allowedRidsPath, "allowed_rids: [101]\n", { encoding: "utf8" });
  return { root, eventsDbPath, allowedRidsPath };
}

test("offline listener lifecycle recovers through Chrome and authorization waits without browser control", async () => {
  const { root, eventsDbPath, allowedRidsPath } = await fixture();
  const fakeClock = clock();
  const shutdown = new AbortController();
  const states = [];
  const delays = [];
  const commands = [];
  const sleep = [];
  let targetAttempt = 0;
  let accepted = 0;
  let clientCount = 0;

  const service = new MxListenerService({
    root,
    eventsDbPath,
    allowedRidsPath,
    instanceId: "offline-e2e",
    clock: fakeClock.now,
    runSelfTest: () => true,
    sleepInhibitor: {
      acquire() { sleep.push("acquire"); return true; },
      release() { sleep.push("release"); return true; },
    },
    openControlPlane: (filename, options) => {
      const plane = openListenerControlPlane(filename, options);
      return {
        claim: (...args) => plane.claim(...args),
        heartbeat: (...args) => plane.heartbeat(...args),
        transition: (...args) => {
          states.push(args[1].readiness);
          return plane.transition(...args);
        },
        release: (...args) => plane.release(...args),
        close: () => plane.close(),
      };
    },
    createCollector: ({ onAccepted }) => ({
      acceptFrame: async () => {
        accepted += 1;
        fakeClock.advance();
        await onAccepted({ eventId: `fixture-${accepted}` });
      },
      recordOverflow: () => {},
    }),
    startMaintenance: () => () => {},
    drainMedia: async () => 0,
    runLoop: (options) => runCollectorLoop({
      ...options,
      findTarget: async () => {
        targetAttempt += 1;
        if (targetAttempt === 1) throw new Error("fixture_chrome_unavailable");
        if (targetAttempt === 2) throw new AuthorizationRequiredError();
        return `ws://fixture/${targetAttempt}`;
      },
      createRouter: ({ onFrame }) => () => onFrame({ fixture: true }),
      createClient: () => {
        clientCount += 1;
        let resolveClosed;
        const closed = new Promise((resolve) => { resolveClosed = resolve; });
        return {
          closed,
          close() { resolveClosed(); },
          onEvent(handler) {
            this.handler = handler;
            return () => { this.handler = undefined; };
          },
          async send(command) {
            commands.push(command);
            setImmediate(() => {
              this.handler?.({ method: "Network.webSocketFrameReceived" });
              if (clientCount === 1) resolveClosed();
              else shutdown.abort(new Error("SIGTERM"));
            });
            return {};
          },
        };
      },
      delay: async (milliseconds) => { delays.push(milliseconds); },
      log: () => {},
    }),
  });

  const result = await service.run({ signal: shutdown.signal });

  assert.deepEqual(result, { started: true, code: "stopped" });
  assert.deepEqual(delays, [1_000, 2_000, 1_000]);
  assert.equal(accepted, 2);
  assert.deepEqual(commands, ["Network.enable", "Network.enable"]);
  assert.equal(commands.some((command) => /^(Page|Runtime|Input)\./.test(command)), false);
  const readinessChanges = states.filter((state, index) => index === 0 || state !== states[index - 1]);
  assert.deepEqual(readinessChanges, [
    "starting",
    "connecting",
    "waiting_for_chrome",
    "connecting",
    "waiting_for_authorization",
    "connecting",
    "listening",
    "connecting",
    "listening",
    "stopping",
  ]);
  assert.equal(sleep.filter((item) => item === "acquire").length, 2);
  assert.ok(sleep.filter((item) => item === "release").length >= 3);

  const plane = openListenerControlPlane(eventsDbPath, { now: fakeClock.now });
  try {
    const snapshot = plane.readSnapshot();
    assert.equal(snapshot.liveness, "offline");
    assert.equal(snapshot.readiness, "stopping");
    assert.ok(snapshot.activity.lastFrameAt !== null);
    assert.ok(snapshot.activity.lastAcceptedEventAt !== null);
    assert.ok(snapshot.activity.lastAcceptedEventAt >= snapshot.activity.lastFrameAt);
  } finally {
    plane.close();
  }
});
