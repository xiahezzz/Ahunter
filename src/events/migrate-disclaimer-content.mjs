import { createHash } from "node:crypto";

import { extractContent } from "../ingestion/room-codec.mjs";

function count(database, table) {
  return database.prepare(`SELECT count(*) AS count FROM ${table}`).get().count;
}

function countOrphans(database) {
  const row = database.prepare(`
    SELECT
      (SELECT count(*)
       FROM media AS m
       LEFT JOIN events AS e ON e.event_id = m.event_id
       WHERE e.event_id IS NULL)
      +
      (SELECT count(*)
       FROM media_jobs AS j
       LEFT JOIN events AS e ON e.event_id = j.event_id
       WHERE e.event_id IS NULL) AS count
  `).get();
  return row.count;
}

function requireUnchanged(label, before, after) {
  if (before !== after) throw new Error(`${label} count changed during migration`);
}

export function migrateDisclaimerContent(database) {
  database.exec("BEGIN IMMEDIATE");
  try {
    const eventsBefore = count(database, "events");
    const mediaBefore = count(database, "media");
    const mediaJobsBefore = count(database, "media_jobs");
    const orphansBefore = countOrphans(database);
    if (orphansBefore !== 0) throw new Error("Pre-existing media orphans detected");

    const rows = database.prepare(`
      SELECT event_id, schema_version, decoded_text, parsed_content_json, content_hash
      FROM events
      ORDER BY event_id
    `).all();
    const update = database.prepare(`
      UPDATE events
      SET schema_version = 2,
          decoded_text = ?,
          parsed_content_json = ?,
          content_hash = ?
      WHERE event_id = ?
    `);
    let updated = 0;

    for (const row of rows) {
      const stored = JSON.parse(row.parsed_content_json);
      if (!stored || typeof stored !== "object" || !Object.hasOwn(stored, "parsed")) {
        throw new Error("Stored parsed content is invalid");
      }
      const cleaned = extractContent(stored.parsed);
      const decodedText = cleaned.texts.join("\n");
      const parsedContentJson = JSON.stringify(cleaned);
      const contentHash = createHash("sha256")
        .update(JSON.stringify(cleaned.parsed))
        .digest("hex");
      const changed =
        row.schema_version !== 2 ||
        row.decoded_text !== decodedText ||
        row.parsed_content_json !== parsedContentJson ||
        row.content_hash !== contentHash;
      if (changed) {
        update.run(decodedText, parsedContentJson, contentHash, row.event_id);
        updated += 1;
      }
    }

    const eventsAfter = count(database, "events");
    const mediaAfter = count(database, "media");
    const mediaJobsAfter = count(database, "media_jobs");
    const orphansAfter = countOrphans(database);
    requireUnchanged("Event", eventsBefore, eventsAfter);
    requireUnchanged("Media", mediaBefore, mediaAfter);
    requireUnchanged("Media job", mediaJobsBefore, mediaJobsAfter);
    if (orphansAfter !== 0) throw new Error("Media orphans detected after migration");

    database.exec("COMMIT");
    return {
      scanned: rows.length,
      updated,
      eventsBefore,
      eventsAfter,
      mediaBefore,
      mediaAfter,
      mediaJobsBefore,
      mediaJobsAfter,
      orphansBefore,
      orphansAfter,
    };
  } catch (error) {
    database.exec("ROLLBACK");
    throw error;
  }
}
