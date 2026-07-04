# Date-Grouped Media Storage Design

## Goal

Store newly downloaded event images in Beijing-date directories instead of two-character hash-prefix directories, while leaving existing files and database paths unchanged until a separate migration.

## Scope

- New media files use `data/media/YYYY-MM-DD/<content-hash>.<extension>`.
- The directory date comes from the parent event's `received_at` timestamp.
- Date conversion uses `Asia/Shanghai` calendar boundaries.
- Existing media files and existing `media.local_path` values are not moved or rewritten.
- Historical migration is explicitly deferred to a separate change.

## Storage Semantics

The content SHA-256 remains the filename, and the verified image type determines the extension. Within one date directory, identical bytes resolve to the same path and reuse the existing atomic create-or-link behavior. Identical bytes received on different Beijing dates are stored once in each date directory. The existing `media.content_hash` column continues to identify matching content across dates.

No source filename becomes part of the local path. This preserves collision resistance, avoids unsafe filename input, and keeps file identity based on verified content.

## Data Flow

`drainMediaJobs` will join each due media job to its parent event and select the event's `received_at`. It will pass that timestamp to `downloadImage`. The downloader will convert the timestamp to a Beijing calendar date, create that date directory with owner-only permissions, and write the content-addressed file there.

This join keeps `received_at` authoritative in the `events` table and avoids adding duplicated timestamp data to `media_jobs`. It also avoids a separate event lookup for every job.

## Validation and Failure Behavior

`downloadImage` requires a finite event-received timestamp. Missing or invalid timestamps fail before any media file is written. The existing URL, redirect, size, MIME, byte-signature, timeout, atomic-write, and permission protections remain unchanged.

The media job follows the existing retry/failure path if date validation or download fails. A failed attempt must not create a final file or completed media record.

## Compatibility

The database schema does not change. `media.local_path` remains the source of truth for both legacy hash-prefix paths and new date-grouped paths. Operational queries and backup procedures continue to address the common `data/media/` root and therefore support both layouts during the incremental period.

## Testing

Tests will be written before production changes and will cover:

- Beijing-date path generation from an event timestamp;
- UTC timestamps on both sides of a Beijing midnight boundary;
- rejection of missing or invalid event timestamps before writing;
- same-date content deduplication;
- identical content stored independently on different Beijing dates;
- media-job processing using the parent event's `received_at`;
- preservation of existing download security and integration behavior.

## Deferred Migration

A later migration may move legacy `data/media/<hash-prefix>/<hash>.<extension>` files into Beijing-date directories and update `media.local_path` transactionally. That migration is not part of this implementation and must define collision handling, rollback, idempotency, and file/database consistency separately.
