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
