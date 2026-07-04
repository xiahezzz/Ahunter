# Date-Grouped Media Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store newly downloaded images under the parent event's Beijing calendar date without changing existing files or database rows.

**Architecture:** `drainMediaJobs` joins due jobs to `events.received_at` and passes the authoritative timestamp into `downloadImage`. The downloader validates that timestamp, formats it with the `Asia/Shanghai` time zone, and retains content-addressed filenames and atomic writes inside `data/media/YYYY-MM-DD/`.

**Tech Stack:** Node.js 24 ESM, `node:test`, `node:assert/strict`, `node:sqlite`, built-in `Intl.DateTimeFormat`, built-in filesystem APIs.

## Global Constraints

- New media files use `data/media/YYYY-MM-DD/<content-hash>.<extension>`.
- The directory date comes from the parent event's `received_at` timestamp using `Asia/Shanghai` calendar boundaries.
- Existing files and existing `media.local_path` values are not moved or rewritten.
- Identical bytes deduplicate within one date but may be stored independently on different dates.
- The database schema and existing media security controls remain unchanged.
- Production changes follow red-green-refactor: every behavior test must be observed failing for the expected reason before implementation.
- Do not modify or commit `config/allowed-rids.yaml`.

## File Structure

- Modify `src/media/download-image.mjs`: validate `eventReceivedAt`, derive a Beijing date directory, and retain content-addressed atomic storage.
- Modify `src/media/process-event-media.mjs`: join media jobs to parent events and pass `received_at` to the downloader.
- Modify `tests/unit/download-image.test.mjs`: cover date boundaries, validation, same-date deduplication, and cross-date copies; update existing downloader calls to the required interface.
- Modify `tests/integration/event-media.test.mjs`: prove the parent event timestamp controls the stored path.
- Modify `tests/unit/final-fixes.test.mjs`: update direct downloader calls to the required interface and retain adjacent regression coverage.
- Modify `docs/mx-listener-operations-manual.md`: document the incremental date-grouped layout while retaining support for legacy paths.

---

### Task 1: Make the Downloader Date-Aware

**Files:**
- Modify: `tests/unit/download-image.test.mjs`
- Modify: `src/media/download-image.mjs`

**Interfaces:**
- Consumes: `downloadImage({ url, mediaRoot, eventReceivedAt, fetchImpl?, connectTimeoutMs?, bodyTimeoutMs?, signal? })`, where `eventReceivedAt` is a finite Unix timestamp in milliseconds.
- Produces: the existing media result object with `localPath` set to `<mediaRoot>/<Beijing YYYY-MM-DD>/<contentHash>.<extension>`.

- [ ] **Step 1: Update existing test calls and add failing date-layout tests**

Add a shared timestamp near the image fixtures and pass it to every existing `downloadImage` call:

```js
const eventReceivedAt = Date.parse("2026-07-03T01:00:00Z");

await downloadImage({
  url: "https://example.com/a.jpg",
  mediaRoot: root,
  eventReceivedAt,
  fetchImpl,
});
```

Add these focused behaviors:

```js
test("stores images under the event's Beijing calendar date", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "a-hunter-media-"));
  const beforeMidnight = await downloadImage({
    url: "https://example.com/before.jpg",
    mediaRoot: root,
    eventReceivedAt: Date.parse("2026-07-03T15:59:59.999Z"),
    fetchImpl: async () => response(jpeg),
  });
  const afterMidnight = await downloadImage({
    url: "https://example.com/after.jpg",
    mediaRoot: root,
    eventReceivedAt: Date.parse("2026-07-03T16:00:00.000Z"),
    fetchImpl: async () => response(jpeg),
  });

  assert.equal(path.basename(path.dirname(beforeMidnight.localPath)), "2026-07-03");
  assert.equal(path.basename(path.dirname(afterMidnight.localPath)), "2026-07-04");
  assert.notEqual(beforeMidnight.localPath, afterMidnight.localPath);
});

test("rejects missing or invalid event timestamps before fetching or writing", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "a-hunter-media-"));
  for (const eventReceivedAt of [undefined, Number.NaN]) {
    await t.test(String(eventReceivedAt), async () => {
      let fetched = false;
      await assert.rejects(downloadImage({
        url: "https://example.com/a.jpg",
        mediaRoot: root,
        eventReceivedAt,
        fetchImpl: async () => { fetched = true; return response(jpeg); },
      }), /eventReceivedAt/);
      assert.equal(fetched, false);
    });
  }
  assert.deepEqual(await readdir(root), []);
});
```

Import `readdir` from `node:fs/promises`. Extend the existing deduplication test to assert that two identical images with the same `eventReceivedAt` share a path. The midnight test above proves identical bytes on different Beijing dates create distinct paths.

- [ ] **Step 2: Run the focused test and verify red**

Run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test tests/unit/download-image.test.mjs
```

Expected: FAIL because paths still use a two-character hash prefix and invalid `eventReceivedAt` is ignored.

- [ ] **Step 3: Implement Beijing-date path generation**

Create one formatter at module scope and one focused validation helper:

```js
const BEIJING_DATE_FORMATTER = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

function beijingEventDate(eventReceivedAt) {
  if (!Number.isFinite(eventReceivedAt)) {
    throw new TypeError("eventReceivedAt must be a finite Unix timestamp in milliseconds");
  }
  return BEIJING_DATE_FORMATTER.format(new Date(eventReceivedAt));
}
```

Require the new argument and validate it before URL parsing or fetching:

```js
export async function downloadImage({
  url, mediaRoot, eventReceivedAt, fetchImpl = fetch, connectTimeoutMs = 10_000,
  bodyTimeoutMs = 30_000, signal,
}) {
  const eventDate = beijingEventDate(eventReceivedAt);
  const source = validatedUrl(url, "Image URL");
  // Existing download and byte validation remain unchanged.
```

Replace the hash-prefix directory only:

```js
const directory = path.join(mediaRoot, eventDate);
const localPath = path.join(directory, `${contentHash}.${extension}`);
```

Keep directory permissions, temporary-file creation, hard-link publication, `EEXIST` handling, final-file permissions, and the return object unchanged.

- [ ] **Step 4: Run the focused test and verify green**

Run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test tests/unit/download-image.test.mjs
```

Expected: all downloader tests PASS with no warnings.

- [ ] **Step 5: Commit the downloader change**

```bash
git add src/media/download-image.mjs tests/unit/download-image.test.mjs
git commit -m "refactor: group downloaded media by event date"
```

---

### Task 2: Propagate the Parent Event Timestamp

**Files:**
- Modify: `tests/integration/event-media.test.mjs`
- Modify: `tests/unit/final-fixes.test.mjs`
- Modify: `src/media/process-event-media.mjs`

**Interfaces:**
- Consumes: existing `media_jobs.event_id` relation to `events.event_id` and `events.received_at` in Unix milliseconds.
- Produces: each `downloadImage` call receives `eventReceivedAt: job.event_received_at`; public `processEventMedia` and `drainMediaJobs` signatures remain unchanged.

- [ ] **Step 1: Add failing integration assertions and update direct downloader consumers**

In `tests/integration/event-media.test.mjs`, assert the accepted event's known timestamp produces the Beijing date directory:

```js
assert.equal(path.basename(path.dirname(row.local_path)), "2026-07-03");
assert.equal(path.basename(row.local_path).endsWith(".jpg"), true);
```

In `tests/unit/final-fixes.test.mjs`, add `eventReceivedAt: at` to every direct `downloadImage` call. Do not add it to `drainMediaJobs`; that function must obtain the timestamp from SQLite.

- [ ] **Step 2: Run integration and adjacent tests and verify red**

Run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test \
  tests/integration/event-media.test.mjs tests/unit/final-fixes.test.mjs
```

Expected: FAIL because `drainMediaJobs` does not yet select or pass the event timestamp, so jobs become failed instead of completing.

- [ ] **Step 3: Join jobs to events and pass the timestamp**

Replace the due-job query construction with qualified columns and the authoritative join:

```js
let sql = `SELECT jobs.event_id, jobs.rid, jobs.source_url, jobs.url_hash, jobs.attempts,
                  events.received_at AS event_received_at
  FROM media_jobs AS jobs
  INNER JOIN events AS events ON events.event_id = jobs.event_id
  WHERE jobs.status IN ('pending', 'failed')
    AND jobs.next_attempt_at <= ? AND jobs.attempts < ?`;
if (eventId) { sql += " AND jobs.event_id = ?"; params.push(eventId); }
sql += " ORDER BY jobs.next_attempt_at, jobs.event_id, jobs.url_hash LIMIT ?";
```

Pass the selected value into the downloader:

```js
const media = await downloadImage({
  url: job.source_url,
  mediaRoot,
  eventReceivedAt: job.event_received_at,
  fetchImpl,
  signal,
});
```

Do not alter retry calculations, error sanitization, concurrency, completion transactions, or batching.

- [ ] **Step 4: Run integration and adjacent tests and verify green**

Run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test \
  tests/integration/event-media.test.mjs tests/unit/final-fixes.test.mjs
```

Expected: all selected tests PASS; the media row points into `2026-07-03` and restart/retry/concurrency tests remain green.

- [ ] **Step 5: Commit timestamp propagation**

```bash
git add src/media/process-event-media.mjs tests/integration/event-media.test.mjs tests/unit/final-fixes.test.mjs
git commit -m "refactor: derive media folders from parent events"
```

---

### Task 3: Document and Verify the Incremental Layout

**Files:**
- Modify: `docs/mx-listener-operations-manual.md`

**Interfaces:**
- Consumes: new `data/media/YYYY-MM-DD/<content-hash>.<extension>` paths and legacy paths recorded in `media.local_path`.
- Produces: operator guidance that treats the SQLite `local_path` value as authoritative during the incremental period.

- [ ] **Step 1: Update the operations manual**

After the storage-path table near the start, add:

```markdown
New downloads are grouped by the parent event's Beijing calendar date as
`data/media/YYYY-MM-DD/<content-hash>.<extension>`. Older rows may still point
to legacy hash-prefix directories. During this incremental transition, use
`media.local_path` as the authoritative location; do not move legacy files by
hand.
```

Do not add migration instructions, because moving existing files is deferred.

- [ ] **Step 2: Run focused and full verification**

Run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test \
  tests/unit/download-image.test.mjs tests/integration/event-media.test.mjs \
  tests/integration/collector.test.mjs tests/unit/final-fixes.test.mjs
npm test
git diff --check
```

Expected: all focused tests PASS, the full suite PASSes, and `git diff --check` emits no output.

- [ ] **Step 3: Confirm scope and commit documentation**

Run:

```bash
git status --short
git diff -- config/allowed-rids.yaml
```

Expected: the pre-existing `config/allowed-rids.yaml` modification remains unstaged and unchanged by this work; only the operations manual is staged for this commit.

```bash
git add docs/mx-listener-operations-manual.md
git commit -m "docs: explain date-grouped media paths"
```

---

## Completion Criteria

- New downloads use the event's Beijing `YYYY-MM-DD` directory.
- Beijing midnight boundaries are covered by automated tests.
- Same-date identical bytes reuse a path; cross-date identical bytes use distinct paths.
- Due jobs obtain timestamps through one joined query without a schema change.
- Existing media files and stored paths remain untouched.
- All tests pass and `config/allowed-rids.yaml` remains outside all commits.
