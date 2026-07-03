import fs from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

const THIRTY_DAYS_MS = 30 * 24 * 60 * 60 * 1000;

export function openEventStore(filename) {
  const fileBacked = filename !== ":memory:";
  if (fileBacked) fs.mkdirSync(path.dirname(filename), { recursive: true });

  const database = new DatabaseSync(filename);
  if (fileBacked) fs.chmodSync(filename, 0o600);
  database.exec(fs.readFileSync(new URL("./schema.sql", import.meta.url), "utf8"));

  const insertRun = database.prepare(
    "INSERT OR IGNORE INTO ingest_runs(run_id, started_at) VALUES (?, ?)",
  );
  const insertEventStatement = database.prepare(`
    INSERT OR IGNORE INTO events(
      event_id, schema_version, rid, source_message_id, oid, received_at,
      source_created_at, raw_payload_hash, raw_payload, raw_payload_expires_at,
      decoded_text, parsed_content_json, content_hash, ingest_run_id
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  `);
  const increment = database.prepare(`
    INSERT INTO ingest_counters(bucket_start, kind, count) VALUES (?, ?, 1)
    ON CONFLICT(bucket_start, kind) DO UPDATE SET count = count + 1
  `);
  const listByRid = database.prepare(`
    SELECT event_id AS eventId, rid, raw_payload AS rawPayload,
           decoded_text AS decodedText, parsed_content_json AS parsedContentJson
    FROM events WHERE rid = ? ORDER BY received_at, event_id
  `);
  const purge = database.prepare(`
    UPDATE events SET raw_payload = NULL
    WHERE raw_payload IS NOT NULL AND raw_payload_expires_at <= ?
  `);
  const truncateWal = database.prepare("PRAGMA wal_checkpoint(TRUNCATE)");

  function checkpointAndRequireTruncate() {
    const result = truncateWal.get();
    if (result.busy !== 0) {
      throw new Error(
        `WAL checkpoint is busy (busy=${result.busy}, log=${result.log}, checkpointed=${result.checkpointed})`,
      );
    }
  }

  return {
    database,
    insertEvent(event, ingestRunId) {
      database.exec("BEGIN IMMEDIATE");
      try {
        insertRun.run(ingestRunId, event.receivedAt);
        const result = insertEventStatement.run(
          event.eventId,
          event.schemaVersion,
          event.rid,
          event.sourceMessageId,
          event.oid,
          event.receivedAt,
          event.sourceCreatedAt,
          event.rawPayloadHash,
          event.rawPayload,
          event.receivedAt + THIRTY_DAYS_MS,
          event.decodedText,
          JSON.stringify(event.parsedContent),
          event.contentHash,
          ingestRunId,
        );
        database.exec("COMMIT");
        return result.changes === 1;
      } catch (error) {
        database.exec("ROLLBACK");
        throw error;
      }
    },
    incrementCounter(kind, at) {
      const bucketStart = Math.floor(at / 3_600_000) * 3_600_000;
      increment.run(bucketStart, kind);
    },
    purgeExpiredPayloads(now) {
      const changes = purge.run(now).changes;
      checkpointAndRequireTruncate();
      database.exec("VACUUM");
      checkpointAndRequireTruncate();
      return changes;
    },
    listEventsByRid(rid) {
      return listByRid.all(rid);
    },
    close() {
      database.close();
    },
  };
}
