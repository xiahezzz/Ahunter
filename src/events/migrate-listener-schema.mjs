import { createHash } from "node:crypto";
import fs from "node:fs";
import { DatabaseSync } from "node:sqlite";

const schemaSql = fs
  .readFileSync(new URL("./schema.sql", import.meta.url), "utf8")
  .replace(/^PRAGMA\s+[^;]+;\s*$/gim, "");

const BUSINESS_TABLES = Object.freeze([
  {
    name: "ingest_runs",
    columns: ["run_id", "started_at"],
    orderBy: ["run_id"],
  },
  {
    name: "events",
    columns: [
      "event_id", "schema_version", "rid", "source_message_id", "oid",
      "received_at", "source_created_at", "raw_payload_hash", "raw_payload",
      "raw_payload_expires_at", "decoded_text", "parsed_content_json",
      "content_hash", "ingest_run_id",
    ],
    orderBy: ["event_id"],
  },
  {
    name: "media",
    columns: [
      "event_id", "rid", "source_url", "url_hash", "content_hash",
      "content_type", "local_path", "downloaded_at",
    ],
    orderBy: ["event_id", "url_hash"],
  },
  {
    name: "media_jobs",
    columns: [
      "event_id", "rid", "source_url", "url_hash", "status", "attempts",
      "next_attempt_at", "error_code",
    ],
    orderBy: ["event_id", "url_hash"],
  },
  {
    name: "decode_failures",
    columns: ["payload_hash", "error_class", "bucket_start", "count"],
    orderBy: ["payload_hash", "error_class", "bucket_start"],
  },
  {
    name: "ingest_counters",
    columns: ["bucket_start", "kind", "count"],
    orderBy: ["bucket_start", "kind"],
  },
]);

const CONTROL_COLUMNS = Object.freeze({
  listener_service_lease: [
    "singleton", "instance_id", "started_at", "heartbeat_at", "expires_at",
  ],
  listener_service_status: [
    "singleton", "readiness", "health", "reason_code", "updated_at",
    "connected_at", "last_frame_at", "last_accepted_event_at",
  ],
});

const SEARCH_TRIGGERS = Object.freeze([
  "mx_event_search_after_insert",
  "mx_event_search_after_update",
  "mx_event_search_after_delete",
]);

export class ListenerSchemaMigrationError extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

export function auditListenerSchema(database, { now = Date.now() } = {}) {
  const business = captureBusinessState(database);
  const schema = inspectListenerSchema(database, now);
  return publicAudit(business, schema);
}

export function migrateListenerSchema(database, { now = Date.now() } = {}) {
  database.exec("PRAGMA foreign_keys = ON; PRAGMA secure_delete = ON;");
  let transactionOpen = false;
  try {
    database.exec("BEGIN IMMEDIATE");
    transactionOpen = true;

    const schemaBefore = inspectListenerSchema(database, now);
    if (schemaBefore.liveLease) {
      throw new ListenerSchemaMigrationError("listener_active");
    }
    const before = captureBusinessState(database);

    database.exec(schemaSql);
    repairSearchIndex(database);

    const after = captureBusinessState(database);
    requireBusinessStateUnchanged(before, after);
    const schemaAfter = inspectListenerSchema(database, now);
    if (!schemaAfter.controlPlaneReady || !schemaAfter.searchReady) {
      throw new ListenerSchemaMigrationError("listener_schema_incomplete");
    }

    database.exec("COMMIT");
    transactionOpen = false;
    return {
      status: "migrated",
      ...publicAudit(after, schemaAfter),
      invariants: {
        businessRowsUnchanged: true,
        eventMediaAssociationsUnchanged: true,
        contentHashesUnchanged: true,
      },
    };
  } catch (error) {
    if (transactionOpen) {
      try {
        database.exec("ROLLBACK");
      } catch {
        // Preserve the original, safely classified migration failure.
      }
    }
    if (error instanceof ListenerSchemaMigrationError) throw error;
    throw new ListenerSchemaMigrationError("listener_migration_failed");
  }
}

export function auditListenerDatabase(filename, options) {
  requireExistingDatabase(filename);
  const database = new DatabaseSync(filename, { readOnly: true });
  try {
    return auditListenerSchema(database, options);
  } finally {
    database.close();
  }
}

export function migrateListenerDatabase(filename, options) {
  requireExistingDatabase(filename);
  const database = new DatabaseSync(filename, { timeout: 5_000 });
  try {
    return migrateListenerSchema(database, options);
  } finally {
    database.close();
  }
}

function requireExistingDatabase(filename) {
  if (typeof filename !== "string" || filename.length === 0 || filename === ":memory:") {
    throw new ListenerSchemaMigrationError("database_path_required");
  }
  const stat = fs.lstatSync(filename, { throwIfNoEntry: false });
  if (!stat || stat.isSymbolicLink() || !stat.isFile()) {
    throw new ListenerSchemaMigrationError("database_unreadable");
  }
}

function captureBusinessState(database) {
  requireQuickCheck(database);
  requireBusinessSchema(database);
  const associations = associationIssues(database);
  if (Object.values(associations).some((value) => value !== 0)) {
    throw new ListenerSchemaMigrationError("business_association_invalid");
  }

  const counts = {};
  const fingerprints = {};
  for (const table of BUSINESS_TABLES) {
    const projection = table.columns.map(quoteIdentifier).join(", ");
    const order = table.orderBy.map(quoteIdentifier).join(", ");
    const rows = database.prepare(
      `SELECT ${projection} FROM ${quoteIdentifier(table.name)} ORDER BY ${order}`,
    ).all();
    counts[table.name] = rows.length;
    fingerprints[table.name] = fingerprintRows(rows, table.columns);
  }
  return { counts, fingerprints, associations };
}

function requireQuickCheck(database) {
  const results = database.prepare("PRAGMA quick_check").all();
  if (
    results.length !== 1
    || String(Object.values(results[0])[0]).toLowerCase() !== "ok"
  ) {
    throw new ListenerSchemaMigrationError("database_integrity_failed");
  }
}

function requireBusinessSchema(database) {
  for (const table of BUSINESS_TABLES) {
    const present = new Set(
      database.prepare(`PRAGMA table_info(${quoteIdentifier(table.name)})`)
        .all()
        .map((row) => row.name),
    );
    if (table.columns.some((column) => !present.has(column))) {
      throw new ListenerSchemaMigrationError("business_schema_invalid");
    }
  }
}

function associationIssues(database) {
  const row = database.prepare(`
    SELECT
      (SELECT count(*)
       FROM events AS e
       LEFT JOIN ingest_runs AS r ON r.run_id = e.ingest_run_id
       WHERE r.run_id IS NULL) AS event_ingest_run,
      (SELECT count(*)
       FROM media AS m
       LEFT JOIN events AS e ON e.event_id = m.event_id AND e.rid = m.rid
       WHERE e.event_id IS NULL) AS media_event,
      (SELECT count(*)
       FROM media_jobs AS j
       LEFT JOIN events AS e ON e.event_id = j.event_id AND e.rid = j.rid
       WHERE e.event_id IS NULL) AS media_job_event
  `).get();
  return {
    eventIngestRun: row.event_ingest_run,
    mediaEvent: row.media_event,
    mediaJobEvent: row.media_job_event,
  };
}

function inspectListenerSchema(database, now) {
  const controlPlaneReady = Object.entries(CONTROL_COLUMNS).every(
    ([table, columns]) => hasColumns(database, table, columns),
  );
  const searchSql = database.prepare(
    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'mx_event_search'",
  ).get()?.sql;
  const searchIsFts = typeof searchSql === "string" && /\bUSING\s+fts5\s*\(/i.test(searchSql);
  const triggerRows = database.prepare(
    "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'mx_event_search_after_%'",
  ).all();
  const triggers = new Set(triggerRows.map((row) => row.name));
  const triggersReady = SEARCH_TRIGGERS.every((name) => triggers.has(name));
  const searchReady = searchIsFts && triggersReady && searchMismatchCount(database) === 0;

  let liveLease = false;
  if (hasColumns(database, "listener_service_lease", CONTROL_COLUMNS.listener_service_lease)) {
    liveLease = database.prepare(
      "SELECT count(*) AS count FROM listener_service_lease WHERE singleton = 1 AND expires_at > ?",
    ).get(now).count !== 0;
  }
  return {
    controlPlaneReady,
    searchReady,
    liveLease,
    migrationRequired: !controlPlaneReady || !searchReady,
  };
}

function hasColumns(database, table, columns) {
  const present = new Set(
    database.prepare(`PRAGMA table_info(${quoteIdentifier(table)})`)
      .all()
      .map((row) => row.name),
  );
  return columns.every((column) => present.has(column));
}

function searchMismatchCount(database) {
  const row = database.prepare(`
    SELECT
      (SELECT count(*)
       FROM events AS e
       LEFT JOIN mx_event_search AS s ON s.rowid = e.rowid
       WHERE s.rowid IS NULL
          OR s.event_id IS NOT e.event_id
          OR s.decoded_text IS NOT e.decoded_text)
      +
      (SELECT count(*)
       FROM mx_event_search AS s
       LEFT JOIN events AS e ON e.rowid = s.rowid
       WHERE e.rowid IS NULL) AS count
  `).get();
  return row.count;
}

function repairSearchIndex(database) {
  if (searchMismatchCount(database) === 0) return;
  database.exec(`
    DELETE FROM mx_event_search;
    INSERT INTO mx_event_search(rowid, event_id, decoded_text)
    SELECT rowid, event_id, decoded_text FROM events;
  `);
}

function requireBusinessStateUnchanged(before, after) {
  for (const table of BUSINESS_TABLES) {
    const name = table.name;
    if (
      before.counts[name] !== after.counts[name]
      || before.fingerprints[name] !== after.fingerprints[name]
    ) {
      throw new ListenerSchemaMigrationError("business_rows_changed");
    }
  }
  if (JSON.stringify(before.associations) !== JSON.stringify(after.associations)) {
    throw new ListenerSchemaMigrationError("business_associations_changed");
  }
}

function fingerprintRows(rows, columns) {
  const digest = createHash("sha256");
  for (const row of rows) {
    const encoded = JSON.stringify(columns.map((column) => row[column]));
    digest.update(String(Buffer.byteLength(encoded)));
    digest.update(":");
    digest.update(encoded);
    digest.update("\n");
  }
  return digest.digest("hex");
}

function quoteIdentifier(value) {
  return `"${value.replaceAll('"', '""')}"`;
}

function publicAudit(business, schema) {
  return {
    integrity: "ok",
    businessCounts: { ...business.counts },
    associationIssues: { ...business.associations },
    controlPlaneReady: schema.controlPlaneReady,
    searchReady: schema.searchReady,
    liveLease: schema.liveLease,
    migrationRequired: schema.migrationRequired,
  };
}
