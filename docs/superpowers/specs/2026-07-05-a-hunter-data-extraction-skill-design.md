# A Hunter Data Extraction Skill Design

## Goal

Create a personal Codex skill at `~/.codex/skills/analyze-a-hunter-data/` that extracts A Hunter SQLite records and original media without analyzing, interpreting, cleaning, or transforming their content.

## Skill Boundary

The skill may:

- filter, sort, paginate, and join stored records;
- run basic database aggregates such as `COUNT` and `SUM`;
- display query results as terminal tables;
- export query results as CSV or JSON;
- return stored media paths and metadata;
- copy selected original media files to a user-specified directory without modifying file contents.

The skill must not:

- summarize, classify, rewrite, or clean content;
- perform OCR, entity extraction, sentiment analysis, topic analysis, or investment analysis;
- infer facts, trends, causality, or conclusions from extracted records;
- resize, convert, annotate, or otherwise process images;
- update SQLite, source media, configuration, or runtime data.

## Data Sources

Use the project database at `data/state/events.sqlite`, resolved relative to the A Hunter project root. Use `media.local_path` as the authoritative path for original media because legacy hash-prefix and current date-grouped layouts coexist.

Open SQLite in read-only mode. The database uses WAL, so read-only queries may run while the collector is active. Never copy the live database merely to query it and never issue schema or data mutation statements.

## Supported Extraction Dimensions

- Time: `events.received_at`, `events.source_created_at`, `media.downloaded_at`, `ingest_runs.started_at`, media-job scheduling times, and counter/failure buckets.
- Source and identity: RID, OID, source message ID, event ID, ingest run ID, schema version, and hashes.
- Stored content: raw payload, decoded text, parsed-content JSON, raw-payload expiry, and content hash.
- Media: event/RID association, source URL, URL hash, content hash, content type, local path, and download time.
- Media jobs: status, attempts, next attempt time, and error code.
- Operational quality records: ingestion counters and decode-failure class/count buckets.
- Cross-table extraction: events with media, events with ingest runs, and media jobs with parent events.

Raw payload extraction is allowed by default. Queries must still select only the fields and rows required by the user's request and must not echo unrelated records.

## Output Modes

- Terminal table for interactive inspection.
- CSV written only when the user requests an export path.
- JSON written only when the user requests an export path.
- Original media copied only when the user requests a destination directory.

Exports must preserve stored values. Timestamp formatting may be added as an extra display column, but raw millisecond values must remain available and must not be overwritten. Media copying must preserve source bytes, filenames, and extensions; name collisions must be detected rather than silently overwritten.

## Skill Structure

Use one concise `SKILL.md` containing:

- clear trigger metadata for A Hunter extraction requests;
- project/database discovery rules;
- a read-only workflow;
- the supported dimension catalog;
- exact SQLite templates for row extraction, joins, and basic aggregates;
- exact terminal-table, CSV, and JSON invocation patterns;
- safe original-media copy instructions;
- explicit prohibited analysis and mutation behavior;
- result-reporting requirements that state query scope, row count, output path, and omitted/unavailable fields without interpreting content.

Generate the recommended `agents/openai.yaml` metadata using the skill-creator tooling. No scripts, references, assets, README, or auxiliary guides are needed.

## Validation

Run the skill creator's structural validator. Then inspect the generated files for placeholders and confirm that every SQL example opens SQLite read-only, every export requires an explicit destination, and media-copy guidance prevents overwrite and content modification.
