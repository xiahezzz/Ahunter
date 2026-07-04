import assert from "node:assert/strict";
import { mkdtemp, readFile, rename, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { watchAllowedRids } from "../../src/config/watch-allowed-rids.mjs";
import { openEventStore } from "../../src/events/event-store.mjs";
import { startRetentionMaintenance } from "../../src/events/retention-maintenance.mjs";
import { Collector } from "../../src/ingestion/collector.mjs";
import { AuthorizationRequiredError, findMxTarget } from "../../src/ingestion/find-mx-target.mjs";
import { runCollectorLoop } from "../../src/ingestion/collector-runner.mjs";
import { downloadImage } from "../../src/media/download-image.mjs";
import { drainMediaJobs } from "../../src/media/process-event-media.mjs";
import { encodeRoomPayload } from "../helpers/encode-room-payload.mjs";

const at = Date.parse("2026-07-03T00:00:00Z");
const event = {
  eventId: "event-final", schemaVersion: 1, rid: 20025, sourceMessageId: "7", oid: null,
  receivedAt: at, sourceCreatedAt: at, rawPayloadHash: "a".repeat(64), rawPayload: "cipher",
  decodedText: "ok", parsedContent: { parsed: {}, texts: ["ok"], imageUrls: ["https://example.com/a.jpg", "https://example.com/b.jpg"] },
  contentHash: "b".repeat(64),
};

function frame(rid, text = "ok") {
  const payload = encodeRoomPayload({ id: `${rid}-${text}`, rid, msg: text }, "2026-07-03");
  return `42/msg,["room_msg",${JSON.stringify(payload)}]`;
}

test("event insertion atomically enqueues every media URL with indexed RID", () => {
  const store = openEventStore(":memory:");
  try {
    assert.equal(store.insertEvent(event, "run"), true);
    const jobs = store.database.prepare("SELECT rid, status, attempts, source_url FROM media_jobs ORDER BY source_url").all().map((row) => ({ ...row }));
    assert.deepEqual(jobs, [
      { rid: 20025, status: "pending", attempts: 0, source_url: "https://example.com/a.jpg" },
      { rid: 20025, status: "pending", attempts: 0, source_url: "https://example.com/b.jpg" },
    ]);
    assert.equal(store.database.prepare("PRAGMA index_list(media_jobs)").all().some(({ name }) => name === "media_jobs_rid_idx"), true);
  } finally { store.close(); }
});

test("media jobs continue after one failure and completed jobs survive restart", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-jobs-"));
  const filename = path.join(directory, "events.sqlite");
  let store = openEventStore(filename);
  store.insertEvent(event, "run");
  let calls = 0;
  await drainMediaJobs({ store, mediaRoot: directory, now: () => at, maxAttempts: 3, fetchImpl: async (url) => {
    calls += 1;
    if (String(url).endsWith("a.jpg")) throw new Error("private detail");
    return new Response(new Uint8Array([0xff, 0xd8, 0xff, 0xd9]), { headers: { "content-type": "image/jpeg" } });
  } });
  assert.equal(calls, 2);
  assert.deepEqual(store.database.prepare("SELECT status, error_code FROM media_jobs ORDER BY source_url").all().map((row) => ({ ...row })), [
    { status: "failed", error_code: "download_failed" }, { status: "completed", error_code: null },
  ]);
  store.close();
  store = openEventStore(filename);
  await drainMediaJobs({ store, mediaRoot: directory, now: () => at + 60_000, fetchImpl: async () => { calls += 1; throw new Error("retry"); } });
  assert.equal(calls, 3);
  assert.equal(store.database.prepare("SELECT attempts FROM media_jobs WHERE source_url LIKE '%b.jpg'").get().attempts, 1);
  store.close();
});

test("retention runs immediately and retries busy with capped sanitized delays", async () => {
  const delays = [];
  const reasons = [];
  let calls = 0;
  const stop = startRetentionMaintenance({
    store: { purgeExpiredPayloads() { calls += 1; if (calls < 4) { const error = new Error("secret"); error.code = "checkpoint_busy"; throw error; } } },
    now: () => at,
    delay: async (ms) => { delays.push(ms); },
    onReason: (reason) => reasons.push(reason),
  });
  await stop.ready;
  stop();
  assert.deepEqual(delays.slice(0, 3), [60_000, 120_000, 240_000]);
  assert.deepEqual(reasons, ["checkpoint_busy", "checkpoint_busy", "checkpoint_busy"]);
});

test("retention schedules a purge every 24 hours outside ingestion", async () => {
  let scheduled;
  let calls = 0;
  const stop = startRetentionMaintenance({
    store: { purgeExpiredPayloads() { calls += 1; } },
    scheduleInterval(fn, ms) { scheduled = { fn, ms }; return 7; },
    clearInterval: () => {},
  });
  await stop.ready;
  assert.equal(calls, 1);
  assert.equal(scheduled.ms, 86_400_000);
  await scheduled.fn();
  assert.equal(calls, 2);
  stop();
});

test("invalid RID reload fails closed and removal affects later frames", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-rids-"));
  const filename = path.join(directory, "allowed.yaml");
  await writeFile(filename, "allowed_rids: [20025]\n");
  const reasons = [];
  const config = watchAllowedRids(filename, { onError: (reason) => reasons.push(reason), watch: false });
  const store = openEventStore(":memory:");
  const collector = new Collector({ allowedRids: () => config.current, store });
  assert.equal(await collector.acceptFrame({ payloadData: frame(20025), receivedAt: at }), "accepted");
  await writeFile(filename, "allowed_rids: [\"bad\"]\n");
  config.reload();
  assert.equal(config.current.size, 0);
  assert.equal(await collector.acceptFrame({ payloadData: frame(20025, "later"), receivedAt: at }), "rejected");
  assert.deepEqual(reasons, ["config_invalid"]);
  config.close(); store.close();
});

test("RID watcher follows atomic file replacement", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-rids-atomic-"));
  const filename = path.join(directory, "allowed.yaml");
  await writeFile(filename, "allowed_rids: [20025]\n");
  const config = watchAllowedRids(filename, { pollIntervalMs: 10 });
  await writeFile(`${filename}.new`, "allowed_rids: [20026]\n");
  await rename(`${filename}.new`, filename);
  for (let attempt = 0; attempt < 20 && !config.current.has(20026); attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.deepEqual([...config.current], [20026]);
  await writeFile(`${filename}.new`, "allowed_rids: [20027]\n");
  await rename(`${filename}.new`, filename);
  for (let attempt = 0; attempt < 20 && !config.current.has(20027); attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.deepEqual([...config.current], [20027]);
  config.close();
});

test("decode failures persist only bucketed hash, class, and count", async () => {
  const store = openEventStore(":memory:");
  const collector = new Collector({ allowedRids: new Set([20025]), store });
  await collector.acceptFrame({ payloadData: '42/msg,["room_msg","private-marker"]', receivedAt: at });
  const row = store.database.prepare("SELECT payload_hash, error_class, bucket_start, count FROM decode_failures").get();
  assert.match(row.payload_hash, /^[a-f0-9]{64}$/);
  assert.equal(row.error_class, "Error"); assert.equal(row.bucket_start, at); assert.equal(row.count, 1);
  assert.equal(JSON.stringify(row).includes("private-marker"), false);
  store.close();
});

test("all non-global literal addresses are rejected but global literals proceed", async (t) => {
  const blocked = [
    "0.1.2.3", "100.64.0.1", "192.0.0.8", "192.0.2.1", "192.88.99.1",
    "198.18.0.1", "224.0.0.1", "240.0.0.1", "[::]", "[ff02::1]",
    "[64:ff9b:1::1]", "[100::1]", "[2001:2::1]", "[2001:db8::1]",
    "[3fff::1]", "[5f00::1]", "[::ffff:100.64.0.1]", "[::ffff:8.8.8.8]",
  ];
  for (const host of blocked) await t.test(host, async () => {
    let fetched = false;
    await assert.rejects(downloadImage({ url: `https://${host}/a.jpg`, mediaRoot: os.tmpdir(), fetchImpl: async () => { fetched = true; } }), /not allowed/);
    assert.equal(fetched, false);
  });
  let fetched = false;
  await assert.rejects(downloadImage({ url: "https://8.8.8.8/a.jpg", mediaRoot: os.tmpdir(), fetchImpl: async () => { fetched = true; throw new Error("stop"); } }), /stop/);
  assert.equal(fetched, true);
  for (const host of [
    "192.0.0.9", "192.0.0.10", "192.0.1.1", "198.51.1.1",
    "[64:ff9b::808:808]", "[2001:3::1]", "[2001:4:112::1]", "[2606:4700:4700::1111]",
  ]) {
    fetched = false;
    await assert.rejects(downloadImage({ url: `https://${host}/a.jpg`, mediaRoot: os.tmpdir(), fetchImpl: async () => { fetched = true; throw new Error("stop"); } }), /stop/);
    assert.equal(fetched, true);
  }
});

test("download aborts a never-resolving fetch on connect timeout", async () => {
  await assert.rejects(downloadImage({
    url: "https://example.com/a.jpg", mediaRoot: os.tmpdir(), connectTimeoutMs: 10,
    fetchImpl: async (_url, { signal }) => new Promise((_, reject) => signal.addEventListener("abort", () => reject(signal.reason), { once: true })),
  }), /timeout/i);
});

test("download aborts a never-resolving body read", async () => {
  const body = new ReadableStream({ start(controller) { controller.enqueue(new Uint8Array([0xff, 0xd8, 0xff])); }, pull() { return new Promise(() => {}); } });
  await assert.rejects(downloadImage({
    url: "https://example.com/a.jpg", mediaRoot: os.tmpdir(), bodyTimeoutMs: 10,
    fetchImpl: async () => new Response(body, { headers: { "content-type": "image/jpeg" } }),
  }), /body-read timeout/i);
});

test("media drain bounds download concurrency", async () => {
  const store = openEventStore(":memory:");
  const many = { ...event, parsedContent: { ...event.parsedContent, imageUrls: Array.from({ length: 6 }, (_, i) => `https://example.com/${i}.jpg`) } };
  store.insertEvent(many, "run");
  let active = 0; let peak = 0;
  await drainMediaJobs({ store, mediaRoot: os.tmpdir(), maxConcurrent: 2, fetchImpl: async () => {
    active += 1; peak = Math.max(peak, active);
    await new Promise((resolve) => setImmediate(resolve));
    active -= 1;
    return new Response(new Uint8Array([0xff, 0xd8, 0xff, 0xd9]), { headers: { "content-type": "image/jpeg" } });
  } });
  assert.equal(peak, 2);
  store.close();
});

test("media drain repeats bounded batches until no due jobs remain", async () => {
  const store = openEventStore(":memory:");
  const first = { ...event, parsedContent: { ...event.parsedContent, imageUrls: ["https://example.com/first.jpg"] } };
  const later = { ...event, eventId: "event-later", sourceMessageId: "8", parsedContent: { ...event.parsedContent, imageUrls: ["https://example.com/later.jpg"] } };
  store.insertEvent(first, "run");
  const urls = [];
  await drainMediaJobs({ store, mediaRoot: os.tmpdir(), batchSize: 1, fetchImpl: async (url) => {
    urls.push(String(url));
    if (urls.length === 1) store.insertEvent(later, "run");
    return new Response(new Uint8Array([0xff, 0xd8, 0xff, 0xd9]), { headers: { "content-type": "image/jpeg" } });
  } });
  assert.deepEqual(urls, ["https://example.com/first.jpg", "https://example.com/later.jpg"]);
  store.close();
});

test("media insertion rolls back when job completion fails", async () => {
  const store = openEventStore(":memory:");
  const single = { ...event, parsedContent: { ...event.parsedContent, imageUrls: ["https://example.com/atomic.jpg"] } };
  store.insertEvent(single, "run");
  store.database.exec(`CREATE TRIGGER reject_media_completion BEFORE UPDATE ON media_jobs
    WHEN NEW.status = 'completed' BEGIN SELECT RAISE(ABORT, 'completion rejected'); END`);
  await drainMediaJobs({ store, mediaRoot: os.tmpdir(), fetchImpl: async () =>
    new Response(new Uint8Array([0xff, 0xd8, 0xff, 0xd9]), { headers: { "content-type": "image/jpeg" } }) });
  assert.equal(store.database.prepare("SELECT count(*) AS count FROM media").get().count, 0);
  assert.equal(store.database.prepare("SELECT status FROM media_jobs").get().status, "failed");
  store.close();
});

test("available target list without MX target is terminal authorization_required", async () => {
  await assert.rejects(findMxTarget("http://cdp", async () => ({ ok: true, async json() { return []; } })), AuthorizationRequiredError);
  const controller = new AbortController();
  const logs = [];
  await runCollectorLoop({ cdpBase: "http://cdp", collector: {}, signal: controller.signal,
    findTarget: async () => { throw new AuthorizationRequiredError(); }, delay: async () => assert.fail("must not retry"), log: (line) => logs.push(line) });
  assert.deepEqual(logs, ["authorization_required"]);
});

test("shutdown starts every queued frame before abort and awaits settlement", async () => {
  const controller = new AbortController();
  let listener;
  const attempted = [];
  let unsettled = 0;
  let closeClient;
  let closeCalls = 0;
  let unregisterCalls = 0;
  const client = {
    closed: new Promise((resolve) => { closeClient = resolve; }),
    onEvent(fn) { listener = fn; return () => { unregisterCalls += 1; }; },
    async send() {},
    close() { closeCalls += 1; },
  };
  const collector = {
    acceptFrame(frame, { signal }) {
      attempted.push({ payloadData: frame.payloadData, aborted: signal.aborted });
      unsettled += 1;
      if (signal.aborted) { unsettled -= 1; return Promise.resolve(); }
      return new Promise((resolve) => signal.addEventListener("abort", () => {
        unsettled -= 1;
        resolve();
      }, { once: true }));
    },
  };
  const running = runCollectorLoop({ cdpBase: "x", collector, signal: controller.signal,
    findTarget: async () => "ws://x", createClient: () => client, createRouter: ({ onFrame }) => onFrame,
    maxConcurrentFrames: 1, maxQueuedFrames: 2, drainTimeoutMs: 5, delay: async () => {} });
  await new Promise((resolve) => setImmediate(resolve));
  listener({ payloadData: "0" });
  await new Promise((resolve) => setImmediate(resolve));
  listener({ payloadData: "1" });
  listener({ payloadData: "2" });
  assert.deepEqual(attempted, [{ payloadData: "0", aborted: false }]);
  assert.equal(unsettled, 1);
  controller.abort();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(attempted, [
    { payloadData: "0", aborted: false },
    { payloadData: "1", aborted: false },
    { payloadData: "2", aborted: false },
  ]);
  closeClient();
  await running;
  assert.equal(unsettled, 0);
  assert.equal(closeCalls, 1);
  assert.equal(unregisterCalls, 1);
});

test("live smoke is Network-only and docs use repository-root commands", async () => {
  const smoke = await readFile(new URL("../../scripts/smoke-test.mjs", import.meta.url), "utf8");
  assert.match(smoke, /new CdpClient/);
  assert.match(smoke, /send\("Network\.enable"\)/);
  assert.match(smoke, /findMxTarget[^;]+signal/s);
  assert.match(smoke, /openTimeoutMs/);
  assert.match(smoke, /commandTimeoutMs/);
  assert.match(smoke, /listener-ready/);
  assert.match(smoke, /websocket-activity/);
  assert.doesNotMatch(smoke, /Runtime\.|Page\.|navigate|reload/i);
  const docs = await readFile(new URL("../../scripts/mx-websocket/README.md", import.meta.url), "utf8");
  assert.match(docs, /npm --prefix scripts\/mx-websocket install/);
  assert.match(docs, /npm --prefix scripts\/mx-websocket run decode/);
  for (const phrase of ["24 hours", "media jobs", "30-second", "authorization_required", "live reload"]) assert.match(docs, new RegExp(phrase, "i"));
  const runner = await readFile(new URL("../../scripts/run-collector.mjs", import.meta.url), "utf8");
  assert.match(runner, /await drainMediaJobs/);
  assert.match(runner, /setInterval\([^\n]*drainPersistedMedia/);
  assert.match(runner, /try\s*{\s*stopMaintenance = startRetentionMaintenance/s);
});
