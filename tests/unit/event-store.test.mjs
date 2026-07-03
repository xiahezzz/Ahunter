import assert from "node:assert/strict";
import { mkdtemp, readFile, stat } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { openEventStore } from "../../src/events/event-store.mjs";

const receivedAt = Date.parse("2026-07-03T00:00:00Z");
const event = {
  eventId: "event-1",
  schemaVersion: 1,
  rid: 20025,
  sourceMessageId: "7",
  oid: "8",
  receivedAt,
  sourceCreatedAt: receivedAt,
  rawPayloadHash: "a".repeat(64),
  rawPayload: "encrypted-target-frame",
  decodedText: "允许内容",
  parsedContent: { parsed: "允许内容", texts: ["允许内容"], imageUrls: [] },
  contentHash: "b".repeat(64),
};

async function temporaryDatabase() {
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-store-"));
  return path.join(directory, "events.sqlite");
}

test("stores events idempotently with an indexed INTEGER RID and payload expiry", async (t) => {
  const filename = await temporaryDatabase();
  const store = openEventStore(filename);
  t.after(() => store.close());

  assert.equal(store.insertEvent(event, "run-1"), true);
  assert.equal(store.insertEvent(event, "run-2"), false);
  assert.deepEqual(store.listEventsByRid(20025).map(({ rid }) => rid), [20025]);
  assert.equal(store.listEventsByRid(23200).length, 0);

  const ridColumn = store.database
    .prepare("PRAGMA table_info(events)")
    .all()
    .find(({ name }) => name === "rid");
  assert.equal(ridColumn.type, "INTEGER");
  const indexes = store.database.prepare("PRAGMA index_list(events)").all().map(({ name }) => name);
  assert.equal(indexes.includes("events_rid_idx"), true);

  assert.equal(store.purgeExpiredPayloads(Date.parse("2026-08-02T00:00:00Z")), 1);
  assert.equal(store.listEventsByRid(20025)[0].rawPayload, null);
  assert.equal(store.purgeExpiredPayloads(Date.parse("2026-08-03T00:00:00Z")), 0);
});

test("inserts the ingest run and event atomically", async (t) => {
  const filename = await temporaryDatabase();
  const store = openEventStore(filename);
  t.after(() => store.close());
  store.database.exec(`
    CREATE TEMP TRIGGER reject_event BEFORE INSERT ON events
    BEGIN
      SELECT RAISE(ABORT, 'forced event failure');
    END
  `);

  assert.throws(
    () => store.insertEvent({ ...event, eventId: "invalid" }, "rolled-back-run"),
    /forced event failure/,
  );
  assert.equal(
    store.database.prepare("SELECT count(*) AS count FROM ingest_runs WHERE run_id = ?").get("rolled-back-run").count,
    0,
  );
});

test("increments counters in hourly buckets", async (t) => {
  const filename = await temporaryDatabase();
  const store = openEventStore(filename);
  t.after(() => store.close());

  store.incrementCounter("accepted", receivedAt + 12_345);
  store.incrementCounter("accepted", receivedAt + 59 * 60_000);
  const counter = store.database
    .prepare("SELECT bucket_start, kind, count FROM ingest_counters")
    .get();
  assert.equal(counter.bucket_start, receivedAt);
  assert.equal(counter.kind, "accepted");
  assert.equal(counter.count, 2);
});

test("uses owner-only file permissions and securely removes purged payload bytes", async (t) => {
  const filename = await temporaryDatabase();
  const marker = `unique-secret-frame-${Date.now()}-${process.pid}`;
  const store = openEventStore(filename);
  t.after(() => store.close());

  assert.equal((await stat(filename)).mode & 0o777, 0o600);
  assert.equal(store.database.prepare("PRAGMA secure_delete").get().secure_delete, 1);
  store.insertEvent({ ...event, eventId: "secret-event", rawPayload: marker }, "run-secret");

  assert.equal(store.purgeExpiredPayloads(Date.parse("2026-08-02T00:00:00Z")), 1);
  for (const suffix of ["", "-wal"]) {
    let bytes;
    try {
      bytes = await readFile(`${filename}${suffix}`);
    } catch (error) {
      if (error.code === "ENOENT") continue;
      throw error;
    }
    assert.equal(bytes.includes(Buffer.from(marker)), false, `marker remained in ${path.basename(filename)}${suffix}`);
  }
});
