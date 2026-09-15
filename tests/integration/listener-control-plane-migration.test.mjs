import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { openEventStore } from "../../src/events/event-store.mjs";
import {
  ListenerSchemaMigrationError,
  auditListenerDatabase,
  migrateListenerDatabase,
} from "../../src/events/migrate-listener-schema.mjs";

const BUSINESS_QUERIES = Object.freeze({
  ingest_runs: "SELECT * FROM ingest_runs ORDER BY run_id",
  events: "SELECT * FROM events ORDER BY event_id",
  media: "SELECT * FROM media ORDER BY event_id, url_hash",
  media_jobs: "SELECT * FROM media_jobs ORDER BY event_id, url_hash",
  decode_failures: "SELECT * FROM decode_failures ORDER BY payload_hash, error_class, bucket_start",
  ingest_counters: "SELECT * FROM ingest_counters ORDER BY bucket_start, kind",
});

async function legacyFixture(t) {
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-listener-migration-"));
  const filename = path.join(directory, "events.sqlite");
  t.after(() => rm(directory, { recursive: true, force: true }));

  const store = openEventStore(filename);
  store.insertEvent({
    eventId: "event-legacy",
    schemaVersion: 2,
    rid: 20025,
    sourceMessageId: "source-id",
    oid: "oid",
    receivedAt: 1_000,
    sourceCreatedAt: 900,
    rawPayloadHash: "a".repeat(64),
    rawPayload: "private-payload",
    decodedText: "规范化正文",
    parsedContent: {
      parsed: [{ type: "text", msg: "规范化正文" }],
      texts: ["规范化正文"],
      imageUrls: ["https://images.example.test/legacy.jpg"],
    },
    contentHash: "b".repeat(64),
  }, "run-legacy");
  const job = store.database.prepare(
    "SELECT event_id, rid, source_url, url_hash FROM media_jobs",
  ).get();
  store.completeMediaJob({
    eventId: job.event_id,
    rid: job.rid,
    sourceUrl: job.source_url,
    urlHash: job.url_hash,
    contentHash: "c".repeat(64),
    contentType: "image/jpeg",
    localPath: "/private/media/legacy.jpg",
    downloadedAt: 1_100,
  });
  store.incrementCounter("accepted", 1_000);
  store.recordDecodeFailure("d".repeat(64), "invalid_payload", 1_000);
  store.database.exec(`
    DROP TRIGGER mx_event_search_after_insert;
    DROP TRIGGER mx_event_search_after_update;
    DROP TRIGGER mx_event_search_after_delete;
    DROP TABLE mx_event_search;
    DROP TABLE listener_service_status;
    DROP TABLE listener_service_lease;
  `);
  store.close();
  return filename;
}

function businessRows(filename) {
  const database = new DatabaseSync(filename, { readOnly: true });
  try {
    return Object.fromEntries(
      Object.entries(BUSINESS_QUERIES).map(
        ([name, query]) => [name, database.prepare(query).all()],
      ),
    );
  } finally {
    database.close();
  }
}

test("listener migration preserves every business row and is idempotent", async (t) => {
  const filename = await legacyFixture(t);
  const before = businessRows(filename);

  const baseline = auditListenerDatabase(filename);
  assert.equal(baseline.integrity, "ok");
  assert.equal(baseline.businessCounts.events, 1);
  assert.equal(baseline.businessCounts.media, 1);
  assert.equal(baseline.businessCounts.media_jobs, 1);
  assert.equal(baseline.controlPlaneReady, false);
  assert.equal(baseline.searchReady, false);
  assert.equal(baseline.migrationRequired, true);
  assert.deepEqual(baseline.associationIssues, {
    eventIngestRun: 0,
    mediaEvent: 0,
    mediaJobEvent: 0,
  });

  const migrated = migrateListenerDatabase(filename, { now: 2_000 });
  assert.equal(migrated.integrity, "ok");
  assert.equal(migrated.controlPlaneReady, true);
  assert.equal(migrated.searchReady, true);
  assert.equal(migrated.migrationRequired, false);
  assert.deepEqual(migrated.invariants, {
    businessRowsUnchanged: true,
    eventMediaAssociationsUnchanged: true,
    contentHashesUnchanged: true,
  });
  assert.deepEqual(businessRows(filename), before);

  const database = new DatabaseSync(filename);
  try {
    assert.deepEqual(
      database.prepare(
        "SELECT event_id FROM mx_event_search WHERE mx_event_search MATCH '规范化正文'",
      ).all().map((row) => row.event_id),
      ["event-legacy"],
    );
    assert.equal(database.prepare("SELECT count(*) AS count FROM listener_service_lease").get().count, 0);
    assert.equal(database.prepare("SELECT count(*) AS count FROM listener_service_status").get().count, 0);
  } finally {
    database.close();
  }

  const repeated = migrateListenerDatabase(filename, { now: 3_000 });
  assert.equal(repeated.migrationRequired, false);
  assert.deepEqual(businessRows(filename), before);
});

test("listener migration refuses to run while a live lease exists", async (t) => {
  const filename = await legacyFixture(t);
  migrateListenerDatabase(filename, { now: 2_000 });
  const database = new DatabaseSync(filename);
  try {
    database.prepare(`
      INSERT INTO listener_service_lease(
        singleton, instance_id, started_at, heartbeat_at, expires_at
      ) VALUES (1, 'listener-live', 2_000, 2_000, 20_000)
    `).run();
  } finally {
    database.close();
  }
  assert.throws(
    () => migrateListenerDatabase(filename, { now: 3_000 }),
    new ListenerSchemaMigrationError("listener_active"),
  );
});
