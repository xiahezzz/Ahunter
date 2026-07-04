# MX Disclaimer Cleaning Design

## Purpose

Remove the fixed MX disclaimer from normalized decoded event content while preserving message identity, raw audit data, and the existing association between events and downloaded images.

The exact removable text is:

```text
免责声明：信息来源于官方媒体/网络新闻等，仅信息分享，不作为投资建议！
```

## Current data findings

- Events are stored in `events`.
- Downloaded image metadata is stored in `media` with `media.event_id` referencing `events.event_id`.
- Pending and completed image work is stored in `media_jobs` with the same event ID and RID.
- Local image files are addressed through `media.local_path` and content hashes.
- Image-only events currently have the disclaimer as their only decoded text.
- OCR is not required for storage or event-to-image association. A later AI evidence pipeline can load `media.local_path` as multimodal input.

## Cleaning rule

Cleaning applies only to normalized decoded content. A string is removed only when trimming its leading and trailing whitespace produces an exact match with the fixed disclaimer.

The cleaner must:

- remove exact disclaimer string elements from nested arrays;
- remove object properties that represent a text message only when that complete text value matches the disclaimer;
- remove a text content object when its only meaningful content is the exact disclaimer;
- preserve all image objects and image URLs;
- preserve ordinary text that contains the disclaimer as part of a longer string;
- preserve object shape and ordering for all unaffected values;
- produce deduplicated `texts` and `imageUrls` from the cleaned parsed content;
- permit an image-only event to have an empty `decoded_text` value.

The same cleaner is the single source of truth for future ingestion and historical migration.

## Future ingestion

`extractContent` will clean the recursively parsed message before extracting text and image URLs. `classifyFrame` will then:

- store the cleaned parsed content in `parsed_content_json`;
- build `decoded_text` from cleaned text values;
- calculate `content_hash` from the cleaned parsed representation;
- continue deriving event identity from the source message ID or OID when present;
- preserve the pre-cleaning parsed-content hash for the existing fallback identity path so cleaning does not change deduplication identity;
- store new events with schema version 2.

Raw encrypted frames, `raw_payload_hash`, RID filtering, and media download behavior remain unchanged.

## Historical migration

Add an idempotent maintenance script that opens `data/state/events.sqlite` through the existing Node 24 runtime and cleans every stored event transactionally.

For each affected event, the migration updates only:

- `decoded_text`;
- `parsed_content_json`;
- `content_hash`;
- `schema_version` to 2.

It must not change:

- `event_id`;
- `rid`;
- source message ID or OID;
- received or source timestamps;
- raw payload or raw payload hash;
- any `media` or `media_jobs` row;
- any local image file.

The migration uses one SQLite write transaction. On any parsing or update error, it rolls back every event update and exits unsuccessfully. Re-running a successful migration produces no further content changes. The script prints aggregate counts only and never prints decoded messages, image URLs, raw payloads, or debugging identifiers.

Because event primary keys remain unchanged, all `media.event_id` and `media_jobs.event_id` associations remain valid. The migration verifies event count, media count, and orphan counts before committing; it refuses to commit if counts change or an orphan appears.

## Operations

The historical migration is a one-time maintenance action. Stop the collector cleanly with `Ctrl-C`, run the migration, verify its aggregate report, then restart the collector. Future messages are cleaned automatically and do not require another migration.

The operations manual will document:

- the one-time migration command;
- the requirement to stop the collector first;
- a query confirming zero exact disclaimer occurrences in `decoded_text` and `parsed_content_json`;
- a join query showing each event and its associated local image paths.

## Tests

Unit tests cover:

- exact disclaimer removal;
- leading and trailing whitespace around the exact disclaimer;
- preservation of a longer sentence containing the disclaimer;
- plain text, arrays, nested objects, text objects, image objects, and mixed content;
- empty decoded text for an image-only event;
- unchanged image URL extraction.

Integration tests cover:

- future ingestion writes schema version 2 and no normalized disclaimer;
- historical migration updates decoded fields and content hashes;
- migration is idempotent;
- a malformed historical JSON row rolls back the entire migration;
- event and media counts remain unchanged;
- `media.event_id` and `media_jobs.event_id` still join to their original event;
- no local image file is changed.

## Acceptance criteria

- Exact disclaimer matches are absent from normalized decoded fields after migration.
- Future accepted events never store the exact disclaimer in normalized decoded fields.
- Longer text containing the same phrase is preserved.
- Existing event IDs and image associations do not change.
- Existing downloaded files and their content hashes do not change.
- Image-only events remain queryable with empty decoded text and associated local image paths.
- The migration is transactional, idempotent, fails closed, and logs no message content.
- The complete test suite passes under the required Node 24 runtime.
