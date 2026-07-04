import fs from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { createHash } from "node:crypto";

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
  const insertMediaStatement = database.prepare(`
    INSERT OR IGNORE INTO media(
      event_id, rid, source_url, url_hash, content_hash, content_type, local_path, downloaded_at
    )
    SELECT ?, ?, ?, ?, ?, ?, ?, ?
    FROM events
    WHERE event_id = ? AND rid = ?
  `);
  const insertMediaJob = database.prepare(`
    INSERT OR IGNORE INTO media_jobs(
      event_id, rid, source_url, url_hash, status, attempts, next_attempt_at, error_code
    ) VALUES (?, ?, ?, ?, 'pending', 0, ?, NULL)
  `);
  const mediaParentMatches = database.prepare(
    "SELECT 1 FROM events WHERE event_id = ? AND rid = ?",
  );
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
  const recordDecodeFailure = database.prepare(`
    INSERT INTO decode_failures(payload_hash, error_class, bucket_start, count)
    VALUES (?, ?, ?, 1)
    ON CONFLICT(payload_hash, error_class, bucket_start)
    DO UPDATE SET count = count + 1
  `);

  function checkpointAndRequireTruncate() {
    const result = truncateWal.get();
    if (result.busy !== 0) {
      const error = new Error("WAL checkpoint is busy");
      error.code = "checkpoint_busy";
      throw error;
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
        if (result.changes === 1) {
          for (const sourceUrl of event.parsedContent.imageUrls) {
            const urlHash = createHash("sha256").update(sourceUrl).digest("hex");
            insertMediaJob.run(event.eventId, event.rid, sourceUrl, urlHash, event.receivedAt);
          }
        }
        database.exec("COMMIT");
        return result.changes === 1;
      } catch (error) {
        database.exec("ROLLBACK");
        throw error;
      }
    },
    insertMedia(media) {
      const result = insertMediaStatement.run(
        media.eventId,
        media.rid,
        media.sourceUrl,
        media.urlHash,
        media.contentHash,
        media.contentType,
        media.localPath,
        media.downloadedAt,
        media.eventId,
        media.rid,
      );
      if (result.changes === 1) return true;
      if (!mediaParentMatches.get(media.eventId, media.rid)) {
        throw new Error("Media RID does not match parent event");
      }
      return false;
    },
    incrementCounter(kind, at) {
      const bucketStart = Math.floor(at / 3_600_000) * 3_600_000;
      increment.run(bucketStart, kind);
    },
    recordDecodeFailure(payloadHash, errorClass, at) {
      const bucketStart = Math.floor(at / 3_600_000) * 3_600_000;
      recordDecodeFailure.run(payloadHash, errorClass, bucketStart);
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
