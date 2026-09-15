import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { openEventStore } from "../../src/events/event-store.mjs";
import { openListenerControlPlane } from "../../src/services/listener-control-plane.mjs";
import { MxListenerService } from "../../src/services/mx-listener-service.mjs";

function makeClock(initial = 2_000_000) {
  let now = initial;
  return { now: () => now, advance: (milliseconds) => { now += milliseconds; } };
}

async function fixture() {
  const root = await mkdtemp(path.join(os.tmpdir(), "a-hunter-listener-service-"));
  const file = path.join(root, "events.sqlite");
  const config = path.join(root, "allowed-rids.yaml");
  await (await import("node:fs/promises")).writeFile(config, "allowed_rids: []\n");
  return { root, file, config };
}

test("default service root is module-pinned and does not consult process.cwd", () => {
  const originalCwd = process.cwd;
  process.cwd = () => { throw new Error("process.cwd is unavailable to this LaunchAgent"); };
  try {
    const service = new MxListenerService();
    assert.equal(
      service.root,
      path.resolve(fileURLToPath(new URL("../..", import.meta.url))),
    );
  } finally {
    process.cwd = originalCwd;
  }
});

test("failed offline self-test never opens the event database or listener path", async () => {
  const { root, file, config } = await fixture();
  let opened = false;
  const service = new MxListenerService({
    root,
    eventsDbPath: file,
    allowedRidsPath: config,
    runSelfTest: () => false,
    openStore: () => { opened = true; throw new Error("must not open"); },
  });
  const result = await service.run();
  assert.deepEqual(result, { started: false, code: "self_test_failed" });
  assert.equal(opened, false);
});

test("service waits through unavailable Chrome and authorization, holds sleep only while listening", async () => {
  const { root, file, config } = await fixture();
  const fake = makeClock();
  const acquired = [];
  const released = [];
  const service = new MxListenerService({
    root,
    eventsDbPath: file,
    allowedRidsPath: config,
    clock: fake.now,
    runSelfTest: () => true,
    sleepInhibitor: {
      acquire() { acquired.push("listening"); return true; },
      release() { released.push("not-listening"); return true; },
    },
    startMaintenance: () => () => {},
    drainMedia: async () => 0,
    runLoop: async ({ onState, onFrame }) => {
      onState("waiting_for_chrome");
      onState("waiting_for_authorization");
      onState("listening");
      fake.advance(100);
      onFrame({});
      onState("waiting_for_chrome");
    },
  });
  const result = await service.run();
  assert.deepEqual(result, { started: true, code: "loop_finished" });
  assert.deepEqual(acquired, ["listening"]);
  assert.ok(released.length >= 1);
  const check = openListenerControlPlane(file, { now: fake.now });
  try {
    const snapshot = check.readSnapshot();
    assert.equal(snapshot.liveness, "offline");
    assert.equal(snapshot.readiness, "stopping");
    assert.equal(snapshot.activity.lastFrameAt, 2_000_100);
  } finally {
    check.close();
  }
});

test("only the lease holder starts collector work", async () => {
  const { root, file, config } = await fixture();
  const fake = makeClock();
  const blocker = new AbortController();
  let entered = 0;
  const common = {
    root,
    eventsDbPath: file,
    allowedRidsPath: config,
    clock: fake.now,
    runSelfTest: () => true,
    startMaintenance: () => () => {},
    drainMedia: async () => 0,
    sleepInhibitor: { acquire: () => true, release: () => true },
  };
  const first = new MxListenerService({
    ...common,
    instanceId: "listener-a",
    runLoop: async ({ signal }) => {
      entered += 1;
      await new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }));
    },
  });
  const running = first.run({ signal: blocker.signal });
  while (entered === 0) await new Promise((resolve) => setImmediate(resolve));
  const second = new MxListenerService({ ...common, instanceId: "listener-b", runLoop: async () => assert.fail("must not run") });
  assert.deepEqual(await second.run(), { started: false, code: "already_running" });
  blocker.abort();
  assert.deepEqual(await running, { started: true, code: "stopped" });
});

test("the service treats a persistent store failure as a failed lease owner", async () => {
  const { root, file, config } = await fixture();
  const fake = makeClock();
  const service = new MxListenerService({
    root,
    eventsDbPath: file,
    allowedRidsPath: config,
    clock: fake.now,
    runSelfTest: () => true,
    startMaintenance: () => () => {},
    drainMedia: async () => 0,
    sleepInhibitor: { acquire: () => true, release: () => true },
    runLoop: async ({ onFrameError, signal }) => {
      onFrameError({ code: "ERR_SQLITE_ERROR" });
      assert.equal(signal.aborted, true);
    },
  });
  const result = await service.run();
  assert.deepEqual(result, { started: true, code: "stopped" });
  const reader = openListenerControlPlane(file, { now: fake.now });
  try {
    assert.equal(reader.readSnapshot().liveness, "offline");
  } finally {
    reader.close();
  }
  const store = openEventStore(file);
  store.close();
});
