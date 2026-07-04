import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { Collector } from "../../src/ingestion/collector.mjs";
import { findMxTarget } from "../../src/ingestion/find-mx-target.mjs";
import { openEventStore } from "../../src/events/event-store.mjs";
import { encodeRoomPayload } from "../helpers/encode-room-payload.mjs";

function frame(rid, text) {
  const payload = encodeRoomPayload({ id: rid, rid, msg: text }, "2026-07-03");
  return `42/msg,["room_msg",${JSON.stringify(payload)}]`;
}

test("collector persists only one unique allowlisted event", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-collector-"));
  const filename = path.join(directory, "events.sqlite");
  const store = openEventStore(filename);
  const collector = new Collector({ allowedRids: new Set([20025]), store });
  const accepted = frame(20025, "allowed-marker");

  assert.equal(
    await collector.acceptFrame({
      payloadData: accepted,
      receivedAt: Date.parse("2026-07-03T01:00:00Z"),
    }),
    "accepted",
  );
  assert.equal(
    await collector.acceptFrame({
      payloadData: frame(23200, "rejected-marker"),
      receivedAt: Date.parse("2026-07-03T01:01:00Z"),
    }),
    "rejected",
  );
  assert.equal(
    await collector.acceptFrame({
      payloadData: accepted,
      receivedAt: Date.parse("2026-07-03T01:02:00Z"),
    }),
    "duplicate",
  );
  assert.equal(
    await collector.acceptFrame({
      payloadData: '42/msg,["room_msg","broken"]',
      receivedAt: Date.parse("2026-07-03T01:03:00Z"),
    }),
    "failed",
  );
  assert.equal(
    await collector.acceptFrame({
      payloadData: "2",
      receivedAt: Date.parse("2026-07-03T01:04:00Z"),
    }),
    "ignored",
  );
  assert.equal(
    await collector.acceptFrame({
      payloadData: '42/msg,["other",["room_msg","broken"]]',
      receivedAt: Date.parse("2026-07-03T01:05:00Z"),
    }),
    "ignored",
  );

  assert.equal(store.listEventsByRid(20025).length, 1);
  assert.equal(store.database.prepare("SELECT count(*) AS n FROM events").get().n, 1);
  store.close();
  const bytes = await readFile(filename);
  assert.equal(bytes.includes(Buffer.from("rejected-marker")), false);
});

test("collector awaits accepted hooks and records hook failures", async () => {
  const store = openEventStore(":memory:");
  let release;
  const hookFinished = new Promise((resolve) => {
    release = resolve;
  });
  const collector = new Collector({
    allowedRids: new Set([20025]),
    store,
    now: () => Date.parse("2026-07-03T02:00:00Z"),
    onAccepted: async () => hookFinished,
  });

  let settled = false;
  const accepting = collector
    .acceptFrame({ payloadData: frame(20025, "hook-marker") })
    .finally(() => {
      settled = true;
    });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(settled, false);
  release();
  assert.equal(await accepting, "accepted");

  const failingCollector = new Collector({
    allowedRids: new Set([20026]),
    store,
    now: () => Date.parse("2026-07-03T02:00:00Z"),
    onAccepted: () => {
      throw new Error("hook payload must not be logged");
    },
  });
  assert.equal(
    await failingCollector.acceptFrame({ payloadData: frame(20026, "private-marker") }),
    "accepted",
  );
  const mediaFailures = store.database
    .prepare("SELECT count FROM ingest_counters WHERE kind = 'media_failed'")
    .get();
  assert.equal(mediaFailures.count, 1);
  store.close();
});

test("collector aborts before classification persistence and after classification", async () => {
  const store = openEventStore(":memory:");
  const alreadyAborted = new AbortController();
  alreadyAborted.abort(new Error("shutdown"));
  const collector = new Collector({ allowedRids: new Set([20025]), store });
  assert.equal(await collector.acceptFrame(
    { payloadData: frame(20025, "before-marker") },
    { signal: alreadyAborted.signal },
  ), "aborted");

  const duringClassification = new AbortController();
  const abortingCollector = new Collector({
    allowedRids: () => {
      duringClassification.abort(new Error("shutdown"));
      return new Set([20025]);
    },
    store,
  });
  assert.equal(await abortingCollector.acceptFrame(
    { payloadData: frame(20025, "after-marker") },
    { signal: duringClassification.signal },
  ), "aborted");
  assert.equal(store.database.prepare("SELECT count(*) AS count FROM events").get().count, 0);
  assert.equal(store.database.prepare("SELECT count(*) AS count FROM ingest_counters").get().count, 0);
  assert.equal(store.database.prepare("SELECT count(*) AS count FROM decode_failures").get().count, 0);
  store.close();
});

test("findMxTarget returns the debugger URL for the exact MX origin", async () => {
  const requested = [];
  const result = await findMxTarget("http://127.0.0.1:9222", async (url) => {
    requested.push(url);
    return {
      ok: true,
      async json() {
        return [
          {
            url: "https://mx.2026.naaifu.cn.evil.example/",
            webSocketDebuggerUrl: "ws://wrong",
          },
          {
            url: "https://mx.2026.naaifu.cn/room",
            webSocketDebuggerUrl: "ws://right",
          },
        ];
      },
    };
  });

  assert.equal(result, "ws://right");
  assert.deepEqual(requested, ["http://127.0.0.1:9222/json/list"]);
});

test("findMxTarget rejects a non-OK target-list response", async () => {
  await assert.rejects(
    findMxTarget("http://127.0.0.1:9222", async () => ({ ok: false, status: 503 })),
    /CDP target list failed: 503/,
  );
});

test("findMxTarget normalizes a trailing slash in the CDP base URL", async () => {
  const requested = [];
  await findMxTarget("http://127.0.0.1:9222/", async (url) => {
    requested.push(url);
    return {
      ok: true,
      async json() {
        return [
          {
            url: "https://mx.2026.naaifu.cn/",
            webSocketDebuggerUrl: "ws://right",
          },
        ];
      },
    };
  });

  assert.deepEqual(requested, ["http://127.0.0.1:9222/json/list"]);
});

test("findMxTarget rejects a target list without the MX page", async () => {
  await assert.rejects(
    findMxTarget("http://127.0.0.1:9222", async () => ({
      ok: true,
      async json() {
        return [{ url: "https://example.com/", webSocketDebuggerUrl: "ws://other" }];
      },
    })),
    /authorization_required/,
  );
});

test("findMxTarget forwards abort to a half-open target-list request", async () => {
  const controller = new AbortController();
  const pending = findMxTarget("http://127.0.0.1:9222", async (_url, { signal }) =>
    new Promise((_, reject) => signal.addEventListener("abort", () => reject(signal.reason), { once: true })),
  { signal: controller.signal });
  controller.abort(new Error("target discovery timeout"));
  await assert.rejects(pending, /target discovery timeout/);
});
