PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA secure_delete = ON;

CREATE TABLE IF NOT EXISTS ingest_runs (
  run_id TEXT PRIMARY KEY,
  started_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL,
  rid INTEGER NOT NULL,
  source_message_id TEXT,
  oid TEXT,
  received_at INTEGER NOT NULL,
  source_created_at INTEGER,
  raw_payload_hash TEXT NOT NULL,
  raw_payload TEXT,
  raw_payload_expires_at INTEGER NOT NULL,
  decoded_text TEXT NOT NULL,
  parsed_content_json TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  ingest_run_id TEXT NOT NULL REFERENCES ingest_runs(run_id)
);

CREATE INDEX IF NOT EXISTS events_rid_idx ON events(rid);
CREATE INDEX IF NOT EXISTS events_received_at_idx ON events(received_at);

CREATE TABLE IF NOT EXISTS media (
  event_id TEXT NOT NULL REFERENCES events(event_id),
  rid INTEGER NOT NULL,
  source_url TEXT NOT NULL,
  url_hash TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  content_type TEXT NOT NULL,
  local_path TEXT NOT NULL,
  downloaded_at INTEGER NOT NULL,
  PRIMARY KEY(event_id, url_hash)
);

CREATE INDEX IF NOT EXISTS media_rid_idx ON media(rid);
CREATE INDEX IF NOT EXISTS media_content_hash_idx ON media(content_hash);

CREATE TABLE IF NOT EXISTS media_jobs (
  event_id TEXT NOT NULL REFERENCES events(event_id),
  rid INTEGER NOT NULL,
  source_url TEXT NOT NULL,
  url_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending', 'failed', 'completed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at INTEGER NOT NULL,
  error_code TEXT,
  PRIMARY KEY(event_id, url_hash)
);

CREATE INDEX IF NOT EXISTS media_jobs_rid_idx ON media_jobs(rid);
CREATE INDEX IF NOT EXISTS media_jobs_due_idx ON media_jobs(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS decode_failures (
  payload_hash TEXT NOT NULL,
  error_class TEXT NOT NULL,
  bucket_start INTEGER NOT NULL,
  count INTEGER NOT NULL,
  PRIMARY KEY(payload_hash, error_class, bucket_start)
);

CREATE TABLE IF NOT EXISTS ingest_counters (
  bucket_start INTEGER NOT NULL,
  kind TEXT NOT NULL,
  count INTEGER NOT NULL,
  PRIMARY KEY(bucket_start, kind)
);

-- The listener control plane is deliberately separate from accepted event
-- rows.  These additions are idempotent and never rewrite historical events,
-- media, media jobs, counters, or decode failures.
CREATE TABLE IF NOT EXISTS listener_service_lease (
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  instance_id TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  heartbeat_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  CHECK(length(instance_id) BETWEEN 1 AND 128),
  CHECK(started_at >= 0),
  CHECK(heartbeat_at >= started_at),
  CHECK(expires_at > heartbeat_at)
);

CREATE TABLE IF NOT EXISTS listener_service_status (
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  readiness TEXT NOT NULL CHECK(readiness IN (
    'starting', 'waiting_for_chrome', 'waiting_for_authorization',
    'connecting', 'listening', 'stopping'
  )),
  health TEXT NOT NULL CHECK(health IN ('healthy', 'degraded', 'failed')),
  reason_code TEXT,
  updated_at INTEGER NOT NULL,
  connected_at INTEGER,
  last_frame_at INTEGER,
  last_accepted_event_at INTEGER,
  CHECK(updated_at >= 0),
  CHECK(connected_at IS NULL OR connected_at >= 0),
  CHECK(last_frame_at IS NULL OR last_frame_at >= 0),
  CHECK(last_accepted_event_at IS NULL OR last_accepted_event_at >= 0)
);

-- Search intentionally indexes only the normalized user-facing text.  It is
-- contentless so raw payloads, source URLs and parsed JSON never become FTS
-- columns or query results.
CREATE VIRTUAL TABLE IF NOT EXISTS mx_event_search USING fts5(
  event_id UNINDEXED,
  decoded_text
);

CREATE TRIGGER IF NOT EXISTS mx_event_search_after_insert
AFTER INSERT ON events BEGIN
  INSERT INTO mx_event_search(rowid, event_id, decoded_text)
  VALUES (NEW.rowid, NEW.event_id, NEW.decoded_text);
END;

CREATE TRIGGER IF NOT EXISTS mx_event_search_after_update
AFTER UPDATE OF decoded_text, event_id ON events BEGIN
  DELETE FROM mx_event_search WHERE rowid = OLD.rowid;
  INSERT INTO mx_event_search(rowid, event_id, decoded_text)
  VALUES (NEW.rowid, NEW.event_id, NEW.decoded_text);
END;

CREATE TRIGGER IF NOT EXISTS mx_event_search_after_delete
AFTER DELETE ON events BEGIN
  DELETE FROM mx_event_search WHERE rowid = OLD.rowid;
END;

INSERT INTO mx_event_search(rowid, event_id, decoded_text)
SELECT events.rowid, events.event_id, events.decoded_text
FROM events
WHERE NOT EXISTS (
  SELECT 1 FROM mx_event_search WHERE mx_event_search.rowid = events.rowid
);
