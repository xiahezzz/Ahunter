# MX Disclaimer Cleaning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the exact fixed MX disclaimer from historical and future normalized event content without changing event identity, raw audit data, or event-to-image associations.

**Architecture:** A focused recursive normalizer supplies one cleaning rule to future ingestion and historical migration. Future ingestion stores cleaned schema-version-2 content while preserving the pre-cleaning fallback identity hash; an idempotent SQLite transaction migrates historical normalized fields and verifies relationship invariants before commit.

**Tech Stack:** Node.js 24.18.0, ECMAScript modules, `node:test`, `node:sqlite`, SQLite WAL and foreign keys, Markdown.

## Global Constraints

- The exact removable text is `免责声明：信息来源于官方媒体/网络新闻等，仅信息分享，不作为投资建议！` after trimming leading and trailing whitespace.
- Remove only complete exact matches; preserve longer text containing the phrase.
- Preserve event IDs, RIDs, source IDs, timestamps, raw payloads, raw hashes, media rows, media-job rows, and local image files.
- Preserve the pre-cleaning parsed-content hash for fallback event identity; store the cleaned hash in `content_hash`.
- Future and successfully migrated events use schema version 2.
- Image-only events may have empty `decoded_text` while retaining image associations.
- Migration is atomic, idempotent, fails closed, and logs aggregate counts only.
- Run Node with `/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node`.
- Do not modify or commit `config/allowed-rids.yaml`.
- Stop the collector cleanly before migrating `data/state/events.sqlite`; never kill it from an implementation task.

## File structure

- Create `src/ingestion/content-normalizer.mjs` and `tests/unit/content-normalizer.test.mjs` for the exact recursive rule.
- Modify `src/ingestion/room-codec.mjs` and its tests to extract cleaned text while retaining images.
- Modify `src/ingestion/classify-frame.mjs` and its tests for schema 2 and stable fallback identity.
- Create `src/events/migrate-disclaimer-content.mjs`, its CLI, and integration tests for migration.
- Modify `docs/mx-listener-operations-manual.md` for migration and event-image queries.

---

### Task 1: Normalize extracted MX content

**Files:**
- Create: `src/ingestion/content-normalizer.mjs`
- Create: `tests/unit/content-normalizer.test.mjs`
- Modify: `src/ingestion/room-codec.mjs`
- Modify: `tests/unit/room-codec.test.mjs`

**Interfaces:**
- Produces: `MX_DISCLAIMER`, `isMxDisclaimer(value)`, and `normalizeMxContent(value)`.
- Produces: exported `parseNestedMessage(message)`.
- Preserves: `extractContent(message) -> { parsed, texts, imageUrls }`.

- [ ] **Step 1: Write failing normalizer tests**

Create tests asserting exact and whitespace-wrapped matches are recognized, longer sentences are preserved, exact strings in arrays are removed, `{type: "text", msg: exact}` objects are removed, image objects remain, unrelated object properties remain, and a root-only disclaimer becomes `null`.

Use these concrete values:

```js
assert.equal(isMxDisclaimer(MX_DISCLAIMER), true);
assert.equal(isMxDisclaimer(`  ${MX_DISCLAIMER}\n`), true);
assert.equal(isMxDisclaimer(`正文 ${MX_DISCLAIMER}`), false);
assert.deepEqual(normalizeMxContent([
  { type: "text", msg: "正文" },
  { type: "text", msg: MX_DISCLAIMER },
  MX_DISCLAIMER,
  `正文引用：${MX_DISCLAIMER}`,
]), [
  { type: "text", msg: "正文" },
  `正文引用：${MX_DISCLAIMER}`,
]);
assert.deepEqual(normalizeMxContent({
  items: [
    { type: "pic", url: "https://example.com/a.png" },
    { type: "text", msg: MX_DISCLAIMER },
  ],
  attribution: MX_DISCLAIMER,
}), {
  items: [{ type: "pic", url: "https://example.com/a.png" }],
  attribution: MX_DISCLAIMER,
});
assert.equal(normalizeMxContent(MX_DISCLAIMER), null);
```

- [ ] **Step 2: Confirm RED**

Run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test \
  tests/unit/content-normalizer.test.mjs
```

Expected: module-not-found failure.

- [ ] **Step 3: Implement the normalizer**

Create `content-normalizer.mjs` with this public contract and a private drop sentinel:

```js
export const MX_DISCLAIMER =
  "免责声明：信息来源于官方媒体/网络新闻等，仅信息分享，不作为投资建议！";

export function isMxDisclaimer(value) {
  return typeof value === "string" && value.trim() === MX_DISCLAIMER;
}

const DROP = Symbol("drop-disclaimer");

function clean(value, location) {
  if (typeof value === "string") {
    if (isMxDisclaimer(value) && ["root", "array", "msg"].includes(location)) {
      return DROP;
    }
    return value;
  }
  if (Array.isArray(value)) {
    return value.flatMap((child) => {
      const cleaned = clean(child, "array");
      return cleaned === DROP ? [] : [cleaned];
    });
  }
  if (!value || typeof value !== "object") return value;

  const entries = Object.entries(value);
  const type = String(value.type || "").toLowerCase();
  const onlyTextFields = entries.every(([key]) => ["type", "msg"].includes(key));
  if (type === "text" && onlyTextFields && isMxDisclaimer(value.msg)) return DROP;

  const result = {};
  for (const [key, child] of entries) {
    const cleaned = clean(child, key === "msg" ? "msg" : "property");
    if (cleaned !== DROP) result[key] = cleaned;
  }
  return result;
}

export function normalizeMxContent(value) {
  const cleaned = clean(value, "root");
  return cleaned === DROP ? null : cleaned;
}
```

The private recursion must drop exact string values only at the root, inside arrays, or under `msg`. It must drop a text object entirely when its only keys are `type` and `msg` and `msg` exactly matches. If a text object has other keys, remove only its matching `msg` and preserve the remaining fields. Do not mutate the input.

- [ ] **Step 4: Confirm GREEN**

Run the Step 2 command. Expected: every normalizer test passes.

- [ ] **Step 5: Write failing extraction tests**

Add to `room-codec.test.mjs`:

```js
const message = JSON.stringify([
  { type: "pic", url: "https://example.com/evidence.png" },
  { type: "text", msg: MX_DISCLAIMER },
]);
assert.deepEqual(extractContent(message), {
  parsed: [{ type: "pic", url: "https://example.com/evidence.png" }],
  texts: [],
  imageUrls: ["https://example.com/evidence.png"],
});
const quoted = `正文引用：${MX_DISCLAIMER}`;
assert.deepEqual(extractContent(quoted).texts, [quoted]);
```

Run `node --test tests/unit/room-codec.test.mjs` with the required absolute Node path. Expected: the disclaimer remains before integration.

- [ ] **Step 6: Integrate extraction and run focused tests**

Export `parseNestedMessage`, import `normalizeMxContent`, and assign:

```js
const parsed = normalizeMxContent(parseNestedMessage(message));
```

Leave URL extraction and deduplication unchanged. Run both Task 1 test files and expect all pass.

- [ ] **Step 7: Commit Task 1**

```bash
git add src/ingestion/content-normalizer.mjs src/ingestion/room-codec.mjs \
  tests/unit/content-normalizer.test.mjs tests/unit/room-codec.test.mjs
git commit -m "feat: normalize MX disclaimer content"
```

---

### Task 2: Clean future events while preserving identity

**Files:**
- Modify: `src/ingestion/classify-frame.mjs`
- Modify: `tests/unit/classify-frame.test.mjs`

**Interfaces:**
- Consumes: `parseNestedMessage` and `extractContent`.
- Produces: schema-version-2 events with cleaned normalized content and legacy-compatible fallback identity.

- [ ] **Step 1: Write failing classifier tests**

Add a test for a message containing one image and the exact disclaimer. Assert:

```js
assert.equal(result.event.schemaVersion, 2);
assert.equal(result.event.decodedText, "");
assert.deepEqual(result.event.parsedContent.imageUrls, [
  "https://example.com/evidence.png",
]);
assert.equal(JSON.stringify(result.event.parsedContent).includes(MX_DISCLAIMER), false);
```

Add a fallback identity test using a value without `id`, `oid`, or `createtime`:

```js
const rawParsed = [{ type: "text", msg: MX_DISCLAIMER }];
const legacyContentHash = createHash("sha256")
  .update(JSON.stringify(rawParsed)).digest("hex");
const expectedEventId = createHash("sha256")
  .update(`20025:${receivedAt}:${legacyContentHash}`).digest("hex");
assert.equal(result.event.eventId, expectedEventId);
```

- [ ] **Step 2: Confirm RED**

Run the classifier unit test with the absolute Node path. Expected: schema version remains 1 or fallback identity differs.

- [ ] **Step 3: Implement dual hashes and schema 2**

Use:

```js
const identityParsed = parseNestedMessage(value.msg);
const identityContentHash = hash(JSON.stringify(identityParsed));
const content = extractContent(identityParsed);
const contentHash = hash(JSON.stringify(content.parsed));
const sourceIdentity =
  value.id ?? value.oid ?? `${value.createtime ?? receivedAt}:${identityContentHash}`;
```

Set `schemaVersion: 2`. Preserve raw payload hashing, allowlist behavior, and sanitized failures.

- [ ] **Step 4: Run adjacent tests and commit**

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test \
  tests/unit/classify-frame.test.mjs tests/unit/room-codec.test.mjs \
  tests/integration/collector.test.mjs tests/integration/event-media.test.mjs
git add src/ingestion/classify-frame.mjs tests/unit/classify-frame.test.mjs
git commit -m "feat: store cleaned MX event schema"
```

Expected: all focused and adjacent tests pass; image jobs still derive from cleaned `imageUrls`.

---

### Task 3: Build the transactional historical migration

**Files:**
- Create: `src/events/migrate-disclaimer-content.mjs`
- Create: `scripts/migrate-disclaimer-content.mjs`
- Create: `tests/integration/disclaimer-migration.test.mjs`

**Interfaces:**
- Produces: `migrateDisclaimerContent(database)` returning numeric aggregate counts only.
- Produces CLI: `scripts/migrate-disclaimer-content.mjs [--database path]`.

- [ ] **Step 1: Write failing migration tests**

Create a temporary file-backed store with one body-plus-disclaimer event and one image-plus-disclaimer event. Complete a media job for the image event and create a real temporary image file. Assert after migration:

```js
assert.equal(report.scanned, 2);
assert.equal(report.updated, 2);
assert.equal(report.eventsBefore, report.eventsAfter);
assert.equal(report.mediaBefore, report.mediaAfter);
assert.equal(report.mediaJobsBefore, report.mediaJobsAfter);
assert.equal(report.orphansAfter, 0);
```

Also assert event IDs remain unchanged, schema versions become 2, normalized fields contain no disclaimer, the image event text is empty, joined media path is unchanged, image SHA-256 is unchanged, and a second migration reports `updated: 0`.

Add a rollback test that corrupts the second row with:

```sql
UPDATE events SET parsed_content_json = '{' WHERE event_id = 'broken'
```

Assert migration throws and the first row remains schema version 1 with its original text.

- [ ] **Step 2: Confirm RED**

Run the new integration test with the absolute Node path. Expected: module-not-found failure.

- [ ] **Step 3: Implement migration transaction**

`migrateDisclaimerContent(database)` must:

1. Execute `BEGIN IMMEDIATE`.
2. Count events, media, media jobs, and left-join orphans; reject pre-existing orphans.
3. Select all events ordered by `event_id`.
4. Parse `parsed_content_json` and require an object with its own `parsed` property.
5. Call `extractContent(stored.parsed)` and hash `JSON.stringify(cleaned.parsed)` with SHA-256.
6. Update only changed rows with parameterized SQL:

```sql
UPDATE events
SET schema_version = 2,
    decoded_text = ?,
    parsed_content_json = ?,
    content_hash = ?
WHERE event_id = ?
```

7. Recount invariants, reject any count change or orphan, commit, and return numeric fields `scanned`, `updated`, `eventsBefore`, `eventsAfter`, `mediaBefore`, `mediaAfter`, `mediaJobsBefore`, `mediaJobsAfter`, `orphansBefore`, and `orphansAfter`.
8. Roll back and rethrow every error.

- [ ] **Step 4: Implement aggregate-only CLI**

Use `openEventStore`; accept optional `--database` with fallback `data/state/events.sqlite`, reject a missing option value, print only formatted report JSON, and always close in `finally`. Do not print decoded text, raw data, URLs, event IDs, or debugger identifiers.

- [ ] **Step 5: Run tests and commit**

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test \
  tests/integration/disclaimer-migration.test.mjs \
  tests/unit/event-store.test.mjs tests/integration/event-media.test.mjs
git add src/events/migrate-disclaimer-content.mjs \
  scripts/migrate-disclaimer-content.mjs \
  tests/integration/disclaimer-migration.test.mjs
git commit -m "feat: migrate stored MX disclaimer content"
```

Expected: all tests pass and no message content is printed.

---

### Task 4: Document migration and event-image association

**Files:**
- Modify: `docs/mx-listener-operations-manual.md`

**Interfaces:**
- Consumes: the migration CLI and current `events`/`media` schema.
- Produces: one-time maintenance and read-only evidence queries.

- [ ] **Step 1: Add the stopped-collector migration procedure**

Require `Ctrl-C`, a returned shell prompt, and no output from:

```bash
pgrep -fl 'scripts/run-collector.mjs'
```

Then document:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node \
  scripts/migrate-disclaimer-content.mjs
```

Explain that a successful report has equal before/after counts and `orphansAfter: 0`; a repeated run has `updated: 0`.

- [ ] **Step 2: Add verification queries**

Document exact-disclaimer verification:

```bash
sqlite3 -header -column data/state/events.sqlite \
"SELECT
   sum(decoded_text LIKE '%免责声明：信息来源于官方媒体/网络新闻等，仅信息分享，不作为投资建议！%') AS decoded_matches,
   sum(parsed_content_json LIKE '%免责声明：信息来源于官方媒体/网络新闻等，仅信息分享，不作为投资建议！%') AS parsed_matches
 FROM events;"
```

Expected: both zero.

Document association:

```bash
sqlite3 -header -column data/state/events.sqlite \
"SELECT e.event_id, e.rid,
        datetime(e.received_at / 1000, 'unixepoch', 'localtime') AS received_time,
        e.decoded_text, m.content_type, m.local_path
 FROM events AS e
 LEFT JOIN media AS m ON m.event_id = e.event_id AND m.rid = e.rid
 ORDER BY e.received_at DESC, m.local_path;"
```

State that empty text plus a nonempty local path is valid for image-only events.

- [ ] **Step 3: Update index, validate, and commit**

```bash
rg -n 'migrate-disclaimer-content|decoded_matches|parsed_matches|LEFT JOIN media|orphansAfter' \
  docs/mx-listener-operations-manual.md
git diff --check
git add docs/mx-listener-operations-manual.md
git commit -m "docs: add MX disclaimer migration procedure"
```

Expected: all required concepts appear and no whitespace errors exist.

---

### Task 5: Verify and migrate the authorized local database

**Files:**
- Runtime only: `data/state/events.sqlite`
- Do not commit: runtime data, local media, or `config/allowed-rids.yaml`
- Commit: `docs/superpowers/plans/2026-07-04-disclaimer-cleaning.md`

**Interfaces:**
- Consumes: completed Tasks 1–4 and a stopped collector.
- Produces: cleaned local normalized fields and final invariant evidence.

- [ ] **Step 1: Run full verification**

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test 'tests/**/*.test.mjs'
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
```

Expected: every test passes in both runs. Record the new total rather than assuming 114.

- [ ] **Step 2: Require a stopped collector**

```bash
pgrep -fl 'scripts/run-collector.mjs'
```

Expected: no output. If active, stop and ask the user to press `Ctrl-C`; do not kill it or migrate production data.

- [ ] **Step 3: Record aggregate pre-migration invariants**

```bash
sqlite3 -header -column data/state/events.sqlite \
"SELECT
 (SELECT count(*) FROM events) AS events,
 (SELECT count(*) FROM media) AS media,
 (SELECT count(*) FROM media_jobs) AS media_jobs,
 (SELECT count(*) FROM media AS m LEFT JOIN events AS e ON e.event_id=m.event_id WHERE e.event_id IS NULL)
 +
 (SELECT count(*) FROM media_jobs AS j LEFT JOIN events AS e ON e.event_id=j.event_id WHERE e.event_id IS NULL)
 AS orphans;"
```

Expected: zero orphans. Record all counts without printing message content.

- [ ] **Step 4: Run and verify production migration**

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node \
  scripts/migrate-disclaimer-content.mjs
```

Expected: exit 0, equal before/after counts, and zero orphans. Run the Task 4 queries; both disclaimer counts must be zero and every media row must retain its parent event and path. Run migration again; expected `updated: 0`.

- [ ] **Step 5: Check state and commit only the plan**

```bash
git diff --check
git status --short
git add docs/superpowers/plans/2026-07-04-disclaimer-cleaning.md
git commit -m "docs: add MX disclaimer cleaning plan"
```

Confirm runtime data and the user's RID configuration remain uncommitted.
