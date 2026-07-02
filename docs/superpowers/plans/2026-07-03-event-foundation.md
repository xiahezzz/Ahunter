# Event Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a continuously running local event foundation that captures MX WebSocket frames through Chrome DevTools, decodes them in memory, persists only allowlisted RID messages and their media, and provides deterministic offline self-tests.

**Architecture:** A Node.js 24 process attaches to the existing logged-in Chrome DevTools endpoint, routes inbound Socket.IO `room_msg` frames through a pure decode-and-filter pipeline, and writes accepted canonical events to SQLite WAL. Configuration is fail-closed, rejected content never reaches durable storage, and the existing decoder remains available through a compatibility CLI.

**Tech Stack:** Node.js 24.18.0 ESM, `node:test`, `node:sqlite`, built-in `fetch`/`WebSocket`, CryptoJS 4.2.0, lz-string 1.5.0, YAML 2.x, SQLite WAL.

## Global Constraints

- Work only inside `/Users/mac/Documents/Ahunter/a_hunter`.
- Use `/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node` because the default `/usr/local/bin/node` is Node 14.19.0.
- Invoke npm as `/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node /Users/mac/.local/share/chrome-devtools-mcp/node/lib/node_modules/npm/bin/npm-cli.js`.
- Never persist a WebSocket payload, decoded text, image URL, or image unless its decoded integer `rid` is present in `config/allowed-rids.yaml`.
- An empty `allowed_rids` list must persist zero WebSocket content.
- On decode failure, persist only an irreversible payload hash, error class, timestamp, and count.
- `rid` is a separate indexed SQLite column.
- Do not save cookies, tokens, Socket.IO session IDs, or Chrome debugger identifiers.
- Subscribe only to inbound Chrome DevTools WebSocket frame events; do not emit Socket.IO business events or operate page business UI.
- Preserve existing scripts and data. Move legacy decoded output to quarantine rather than deleting or silently importing it.
- All behavior changes use TDD. Run targeted tests before the full suite. Commit after every task.

---

### Task 1: Root Runtime, Fail-Closed RID Configuration, and Test Harness

**Files:**
- Modify: `.gitignore`
- Create: `package.json`
- Create: `config/allowed-rids.yaml`
- Create: `src/config/load-allowed-rids.mjs`
- Create: `tests/unit/load-allowed-rids.test.mjs`
- Create: `scripts/self-test.mjs`

**Interfaces:**
- Produces: `loadAllowedRids(path: string) -> ReadonlySet<number>`.
- Produces: root `npm test` and `npm run self-test` commands used by every later task.

- [ ] **Step 1: Write the failing configuration tests**

```js
// tests/unit/load-allowed-rids.test.mjs
import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { loadAllowedRids } from "../../src/config/load-allowed-rids.mjs";

async function configFile(contents) {
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-config-"));
  const filename = path.join(directory, "allowed-rids.yaml");
  await writeFile(filename, contents);
  return filename;
}

test("empty allowlist is valid and fail-closed", async () => {
  assert.deepEqual([...loadAllowedRids(await configFile("allowed_rids: []\n"))], []);
});

test("deduplicates positive integer RIDs", async () => {
  const result = loadAllowedRids(
    await configFile("allowed_rids:\n  - 20025\n  - 23200\n  - 20025\n"),
  );
  assert.deepEqual([...result], [20025, 23200]);
});

for (const invalid of ["allowed_rids: 20025\n", "allowed_rids: [0]\n", "allowed_rids: [\"20025\"]\n"]) {
  test(`rejects invalid config: ${JSON.stringify(invalid)}`, async () => {
    const filename = await configFile(invalid);
    assert.throws(() => loadAllowedRids(filename), /allowed_rids/);
  });
}
```

- [ ] **Step 2: Run the targeted test and verify failure**

Run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test tests/unit/load-allowed-rids.test.mjs
```

Expected: FAIL with `ERR_MODULE_NOT_FOUND` for `src/config/load-allowed-rids.mjs`.

- [ ] **Step 3: Add the runtime, configuration, and loader**

```json
// package.json
{
  "name": "a-hunter",
  "private": true,
  "type": "module",
  "engines": { "node": ">=24.18.0" },
  "scripts": {
    "test": "node --test tests/unit tests/integration",
    "self-test": "node scripts/self-test.mjs"
  },
  "dependencies": {
    "crypto-js": "4.2.0",
    "lz-string": "1.5.0",
    "yaml": "2.8.1"
  }
}
```

```yaml
# config/allowed-rids.yaml
allowed_rids: []
```

```js
// src/config/load-allowed-rids.mjs
import fs from "node:fs";
import YAML from "yaml";

export function loadAllowedRids(filename) {
  const document = YAML.parse(fs.readFileSync(filename, "utf8"));
  const values = document?.allowed_rids;
  if (!Array.isArray(values)) throw new TypeError("allowed_rids must be an array");
  for (const value of values) {
    if (!Number.isSafeInteger(value) || value <= 0) {
      throw new TypeError("allowed_rids must contain positive integers");
    }
  }
  return new Set(values);
}
```

```gitignore
# .gitignore
node_modules/
.superpowers/
data/state/*.sqlite*
data/media/
data/quarantine/
reports/self-test/
*.log
```

```js
// scripts/self-test.mjs
import { mkdir, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";

const startedAt = new Date().toISOString();
const result = spawnSync(process.execPath, ["--test", "tests/unit", "tests/integration"], {
  encoding: "utf8",
});
const report = {
  schemaVersion: 1,
  startedAt,
  finishedAt: new Date().toISOString(),
  ok: result.status === 0,
  exitCode: result.status,
  stdout: result.stdout,
  stderr: result.stderr,
};
await mkdir("reports/self-test", { recursive: true });
await writeFile("reports/self-test/latest.json", `${JSON.stringify(report, null, 2)}\n`);
await writeFile(
  "reports/self-test/latest.md",
  `# Self Test\n\n- Status: ${report.ok ? "PASS" : "FAIL"}\n- Started: ${startedAt}\n- Finished: ${report.finishedAt}\n`,
);
process.stdout.write(result.stdout);
process.stderr.write(result.stderr);
process.exitCode = result.status ?? 1;
```

- [ ] **Step 4: Install pinned dependencies and run the test**

Run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node /Users/mac/.local/share/chrome-devtools-mcp/node/lib/node_modules/npm/bin/npm-cli.js install
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test tests/unit/load-allowed-rids.test.mjs
```

Expected: npm creates `package-lock.json`; all configuration tests PASS.

- [ ] **Step 5: Commit**

```bash
git add .gitignore package.json package-lock.json config src/config tests/unit/load-allowed-rids.test.mjs scripts/self-test.mjs
git commit -m "feat: add fail-closed RID configuration"
```

---

### Task 2: Extract a Pure Room Message Codec

**Files:**
- Create: `src/ingestion/room-codec.mjs`
- Create: `tests/helpers/encode-room-payload.mjs`
- Create: `tests/unit/room-codec.test.mjs`
- Modify: `scripts/mx-websocket/decode-room-msg.mjs`

**Interfaces:**
- Produces: `decodeRoomFrame(frame: string, dateString: string) -> object`.
- Produces: `extractContent(message: unknown) -> { parsed, texts, imageUrls }`.
- Preserves: existing manual decode CLI behavior before RID filtering is added in Task 3.

- [ ] **Step 1: Write codec tests using a synthetic encrypted frame**

```js
// tests/helpers/encode-room-payload.mjs
import CryptoJS from "crypto-js";
import LZString from "lz-string";

export function encodeRoomPayload(value, dateString) {
  const digest = CryptoJS.MD5(dateString).toString();
  const key = CryptoJS.enc.Utf8.parse(digest.slice(0, 16));
  const iv = CryptoJS.enc.Utf8.parse(digest.slice(8, 14));
  const encrypted = CryptoJS.AES.encrypt(JSON.stringify(value), key, {
    iv,
    mode: CryptoJS.mode.CBC,
    padding: CryptoJS.pad.Pkcs7,
  }).toString();
  return LZString.compress(encrypted);
}
```

```js
// tests/unit/room-codec.test.mjs
import assert from "node:assert/strict";
import test from "node:test";
import { decodeRoomFrame, extractContent } from "../../src/ingestion/room-codec.mjs";
import { encodeRoomPayload } from "../helpers/encode-room-payload.mjs";

test("decodes an encrypted Socket.IO room_msg frame", () => {
  const value = { id: 7, rid: 20025, createtime: 1783007408338, msg: "测试" };
  const payload = encodeRoomPayload(value, "2026-07-03");
  const frame = `42/msg,9["room_msg",${JSON.stringify(payload)}]`;
  assert.deepEqual(decodeRoomFrame(frame, "2026-07-03"), value);
});

test("extracts nested text and image URLs", () => {
  const message = JSON.stringify([
    { type: "text", msg: "公告摘要" },
    { type: "pic", url: "https://pic.guhai888.cn/example.jpg" },
  ]);
  assert.deepEqual(extractContent(message), {
    parsed: JSON.parse(message),
    texts: ["公告摘要"],
    imageUrls: ["https://pic.guhai888.cn/example.jpg"],
  });
});

test("rejects a frame with the wrong event name", () => {
  assert.throws(() => decodeRoomFrame('42/msg,["va",1]', "2026-07-03"), /room_msg/);
});
```

- [ ] **Step 2: Run the tests and verify failure**

Run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test tests/unit/room-codec.test.mjs
```

Expected: FAIL with `ERR_MODULE_NOT_FOUND` for `src/ingestion/room-codec.mjs`.

- [ ] **Step 3: Move the codec into a focused module**

Create `src/ingestion/room-codec.mjs` by moving these existing functions without changing their algorithm: `payloadFromFrame`, `derivedKey`, `decodePayload`, `parseNestedMessage`, and `extractContent`. Export this public wrapper:

```js
export function decodeRoomFrame(frame, dateString) {
  return decodePayload(payloadFromFrame(frame), dateString);
}

export { extractContent };
```

In `scripts/mx-websocket/decode-room-msg.mjs`, remove the moved function bodies and import them:

```js
import {
  decodeRoomFrame,
  extractContent,
} from "../../src/ingestion/room-codec.mjs";
```

Replace:

```js
const value = decodePayload(payloadFromFrame(frame), frameDate);
```

with:

```js
const value = decodeRoomFrame(frame, frameDate);
```

- [ ] **Step 4: Run codec and existing CLI smoke tests**

Run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test tests/unit/room-codec.test.mjs
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/mx-websocket/decode-room-msg.mjs
```

Expected: codec tests PASS; CLI exits 1 and prints its usage because `--input` is absent.

- [ ] **Step 5: Commit**

```bash
git add src/ingestion/room-codec.mjs tests/helpers tests/unit/room-codec.test.mjs scripts/mx-websocket/decode-room-msg.mjs
git commit -m "refactor: extract room message codec"
```

---

### Task 3: Enforce RID Filtering Before Any Durable Write

**Files:**
- Create: `src/ingestion/classify-frame.mjs`
- Create: `tests/unit/classify-frame.test.mjs`
- Modify: `scripts/mx-websocket/decode-room-msg.mjs`
- Create: `scripts/quarantine-legacy-output.mjs`
- Test: `tests/integration/manual-decoder-filter.test.mjs`

**Interfaces:**
- Consumes: `decodeRoomFrame()` and `extractContent()` from Task 2.
- Produces: `classifyFrame({ frame, receivedAt, allowedRids }) -> Accepted | Rejected | Failed`.
- `Accepted` contains canonical content; `Rejected` and `Failed` contain no payload, text, or image URL.

- [ ] **Step 1: Write fail-closed classification tests**

```js
// tests/unit/classify-frame.test.mjs
import assert from "node:assert/strict";
import test from "node:test";
import { classifyFrame } from "../../src/ingestion/classify-frame.mjs";
import { encodeRoomPayload } from "../helpers/encode-room-payload.mjs";

const receivedAt = Date.parse("2026-07-03T09:01:00+08:00");
function frameFor(rid) {
  const payload = encodeRoomPayload({ id: 1, oid: 2, rid, msg: "仅目标可见" }, "2026-07-03");
  return `42/msg,["room_msg",${JSON.stringify(payload)}]`;
}

test("accepts an allowlisted RID and stores RID separately", () => {
  const result = classifyFrame({ frame: frameFor(20025), receivedAt, allowedRids: new Set([20025]) });
  assert.equal(result.status, "accepted");
  assert.equal(result.event.rid, 20025);
  assert.equal(result.event.decodedText, "仅目标可见");
});

test("rejects non-allowlisted content without retaining it", () => {
  const result = classifyFrame({ frame: frameFor(23200), receivedAt, allowedRids: new Set([20025]) });
  assert.deepEqual(Object.keys(result).sort(), ["reason", "status"]);
  assert.equal(JSON.stringify(result).includes("仅目标可见"), false);
});

test("empty allowlist rejects all content", () => {
  assert.equal(classifyFrame({ frame: frameFor(20025), receivedAt, allowedRids: new Set() }).status, "rejected");
});

test("decode failure returns only a hash and error class", () => {
  const result = classifyFrame({ frame: "not-decodable", receivedAt, allowedRids: new Set([20025]) });
  assert.deepEqual(Object.keys(result).sort(), ["errorClass", "payloadHash", "status"]);
  assert.match(result.payloadHash, /^[a-f0-9]{64}$/);
});
```

- [ ] **Step 2: Run and verify failure**

Run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test tests/unit/classify-frame.test.mjs
```

Expected: FAIL with `ERR_MODULE_NOT_FOUND` for `classify-frame.mjs`.

- [ ] **Step 3: Implement the discriminated result and stable event identity**

```js
// src/ingestion/classify-frame.mjs
import { createHash } from "node:crypto";
import { decodeRoomFrame, extractContent } from "./room-codec.mjs";

function localDateString(timestamp) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(new Date(timestamp));
  const value = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
  return `${value.year}-${value.month}-${value.day}`;
}

function hash(value) {
  return createHash("sha256").update(value).digest("hex");
}

export function classifyFrame({ frame, receivedAt, allowedRids }) {
  try {
    const value = decodeRoomFrame(frame, localDateString(receivedAt));
    if (!Number.isSafeInteger(value.rid) || !allowedRids.has(value.rid)) {
      return { status: "rejected", reason: "rid_not_allowed" };
    }
    const content = extractContent(value.msg);
    const contentHash = hash(JSON.stringify(content.parsed));
    const sourceIdentity = value.id ?? value.oid ?? `${value.createtime ?? receivedAt}:${contentHash}`;
    return {
      status: "accepted",
      event: {
        eventId: hash(`${value.rid}:${sourceIdentity}`),
        schemaVersion: 1,
        rid: value.rid,
        sourceMessageId: value.id == null ? null : String(value.id),
        oid: value.oid == null ? null : String(value.oid),
        receivedAt,
        sourceCreatedAt: value.createtime ?? null,
        rawPayloadHash: hash(frame),
        rawPayload: frame,
        decodedText: content.texts.join("\n"),
        parsedContent: content,
        contentHash,
      },
    };
  } catch (error) {
    return {
      status: "failed",
      payloadHash: hash(frame),
      errorClass: error instanceof Error ? error.constructor.name : "UnknownError",
    };
  }
}
```

- [ ] **Step 4: Make the manual decoder apply the same allowlist**

Add these imports and configuration immediately after the existing imports:

```js
import { loadAllowedRids } from "../../src/config/load-allowed-rids.mjs";

const configPath = path.resolve(
  option("--config") || path.join(scriptDirectory, "../../config/allowed-rids.yaml"),
);
const allowedRids = loadAllowedRids(configPath);
```

After the existing decode `value` is produced and before returning an `ok: true` result, add:

```js
if (!Number.isSafeInteger(value.rid) || !allowedRids.has(value.rid)) {
  return { index, ok: false, rejected: true, error: "rid_not_allowed" };
}
```

Keep the existing `.filter(({ ok }) => ok)` write path so rejected values never reach the fixed JSON file. Extend `usage()` to show `[--config allowed-rids.yaml]`.

```js
// scripts/quarantine-legacy-output.mjs
import { access, mkdir, rename } from "node:fs/promises";
import path from "node:path";

const source = path.resolve("data/mx-websocket/decoded-room-messages.json");
const destination = path.resolve("data/quarantine/decoded-room-messages.pre-rid-filter.json");
try {
  await access(source);
} catch {
  console.log("No legacy output to quarantine");
  process.exit(0);
}
try {
  await access(destination);
  throw new Error(`Quarantine destination already exists: ${destination}`);
} catch (error) {
  if (error.code !== "ENOENT") throw error;
}
await mkdir(path.dirname(destination), { recursive: true });
await rename(source, destination);
console.log(`Quarantined legacy output: ${destination}`);
```

```js
// tests/integration/manual-decoder-filter.test.mjs
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { encodeRoomPayload } from "../helpers/encode-room-payload.mjs";

test("manual decoder writes only configured RID", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-cli-"));
  const input = path.join(directory, "frames.json");
  const output = path.join(directory, "decoded.json");
  const config = path.join(directory, "allowed.yaml");
  const make = (rid, msg) => `42/msg,["room_msg",${JSON.stringify(encodeRoomPayload({ id: rid, rid, msg }, "2026-07-03"))}]`;
  await writeFile(input, JSON.stringify([make(20025, "allowed-marker"), make(23200, "rejected-marker")]));
  await writeFile(config, "allowed_rids: [20025]\n");
  const result = spawnSync(process.execPath, [
    "scripts/mx-websocket/decode-room-msg.mjs", "--input", input, "--output", output,
    "--config", config, "--date", "2026-07-03",
  ], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  const text = await readFile(output, "utf8");
  assert.equal(JSON.parse(text).messages.length, 1);
  assert.equal(text.includes("allowed-marker"), true);
  assert.equal(text.includes("rejected-marker"), false);
});
```

- [ ] **Step 5: Run focused and integration tests**

Run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test tests/unit/classify-frame.test.mjs tests/integration/manual-decoder-filter.test.mjs
```

Expected: all tests PASS; integration output contains only the configured RID.

- [ ] **Step 6: Commit**

```bash
git add src/ingestion/classify-frame.mjs tests/unit/classify-frame.test.mjs tests/integration/manual-decoder-filter.test.mjs scripts/mx-websocket/decode-room-msg.mjs scripts/quarantine-legacy-output.mjs
git commit -m "feat: reject non-allowlisted RID content"
```

---

### Task 4: SQLite WAL Event Ledger and Retention

**Files:**
- Create: `src/events/schema.sql`
- Create: `src/events/event-store.mjs`
- Create: `tests/unit/event-store.test.mjs`

**Interfaces:**
- Consumes: accepted canonical events from `classifyFrame()`.
- Produces: `openEventStore(filename) -> EventStore`.
- Produces methods: `insertEvent(event, ingestRunId)`, `incrementCounter(kind, at)`, `purgeExpiredPayloads(now)`, `listEventsByRid(rid)` and `close()`.

- [ ] **Step 1: Write event store tests**

```js
// tests/unit/event-store.test.mjs
import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { openEventStore } from "../../src/events/event-store.mjs";

const receivedAt = Date.parse("2026-07-03T00:00:00Z");
const event = {
  eventId: "event-1", schemaVersion: 1, rid: 20025, sourceMessageId: "7", oid: "8",
  receivedAt, sourceCreatedAt: receivedAt, rawPayloadHash: "a".repeat(64),
  rawPayload: "encrypted-target-frame", decodedText: "允许内容",
  parsedContent: { parsed: "允许内容", texts: ["允许内容"], imageUrls: [] },
  contentHash: "b".repeat(64),
};

test("stores events idempotently with an RID index and payload expiry", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-store-"));
  const store = openEventStore(path.join(directory, "events.sqlite"));
  assert.equal(store.insertEvent(event, "run-1"), true);
  assert.equal(store.insertEvent(event, "run-2"), false);
  assert.deepEqual(store.listEventsByRid(20025).map(({ rid }) => rid), [20025]);
  assert.equal(store.listEventsByRid(23200).length, 0);
  const indexes = store.database.prepare("PRAGMA index_list(events)").all().map(({ name }) => name);
  assert.equal(indexes.includes("events_rid_idx"), true);
  store.purgeExpiredPayloads(Date.parse("2026-08-03T00:00:00Z"));
  assert.equal(store.listEventsByRid(20025)[0].rawPayload, null);
  store.close();
});
```

- [ ] **Step 2: Run and verify failure**

Run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test tests/unit/event-store.test.mjs
```

Expected: FAIL because `event-store.mjs` does not exist.

- [ ] **Step 3: Create the schema**

```sql
-- src/events/schema.sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

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

CREATE TABLE IF NOT EXISTS ingest_counters (
  bucket_start INTEGER NOT NULL,
  kind TEXT NOT NULL,
  count INTEGER NOT NULL,
  PRIMARY KEY(bucket_start, kind)
);
```

- [ ] **Step 4: Implement the store with prepared statements**

```js
// src/events/event-store.mjs
import fs from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

const THIRTY_DAYS_MS = 30 * 24 * 60 * 60 * 1000;

export function openEventStore(filename) {
  fs.mkdirSync(path.dirname(filename), { recursive: true });
  const database = new DatabaseSync(filename);
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

  return {
    database,
    insertEvent(event, ingestRunId) {
      database.exec("BEGIN IMMEDIATE");
      try {
        insertRun.run(ingestRunId, event.receivedAt);
        const result = insertEventStatement.run(
          event.eventId, event.schemaVersion, event.rid, event.sourceMessageId,
          event.oid, event.receivedAt, event.sourceCreatedAt, event.rawPayloadHash,
          event.rawPayload, event.receivedAt + THIRTY_DAYS_MS, event.decodedText,
          JSON.stringify(event.parsedContent), event.contentHash, ingestRunId,
        );
        database.exec("COMMIT");
        return result.changes === 1;
      } catch (error) {
        database.exec("ROLLBACK");
        throw error;
      }
    },
    incrementCounter(kind, at) {
      const bucketStart = Math.floor(at / 3_600_000) * 3_600_000;
      increment.run(bucketStart, kind);
    },
    purgeExpiredPayloads(now) {
      return purge.run(now).changes;
    },
    listEventsByRid(rid) {
      return listByRid.all(rid);
    },
    close() {
      database.close();
    },
  };
}
```

- [ ] **Step 5: Run the store tests**

Run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test tests/unit/event-store.test.mjs
```

Expected: all tests PASS, including duplicate rejection and RID index checks.

- [ ] **Step 6: Commit**

```bash
git add src/events tests/unit/event-store.test.mjs
git commit -m "feat: add indexed event ledger"
```

---

### Task 5: Chrome DevTools Inbound Frame Source

**Files:**
- Create: `src/ingestion/cdp-client.mjs`
- Create: `src/ingestion/cdp-frame-router.mjs`
- Create: `tests/unit/cdp-frame-router.test.mjs`
- Create: `tests/unit/cdp-client.test.mjs`

**Interfaces:**
- Produces: `CdpClient` with `send(method, params)`, `onEvent(listener)`, and `close()`.
- Produces: `createMxFrameRouter({ onFrame, socketUrlPattern }) -> (event) => void`.
- Router emits inbound `Network.webSocketFrameReceived` text payloads when the socket URL matches `/business-api/5/socket.io/`, or when an already-established socket has no replayed creation event but the payload has the strict `42/msg,...["room_msg",...]` signature.

- [ ] **Step 1: Write router tests**

```js
// tests/unit/cdp-frame-router.test.mjs
import assert from "node:assert/strict";
import test from "node:test";
import { createMxFrameRouter } from "../../src/ingestion/cdp-frame-router.mjs";

test("routes only inbound MX socket text frames", () => {
  const frames = [];
  const route = createMxFrameRouter({ onFrame: (frame) => frames.push(frame), now: () => 2_000 });
  route({ method: "Network.webSocketCreated", params: { requestId: "a", url: "wss://mx.2026.naaifu.cn/business-api/5/socket.io/?EIO=4&transport=websocket" } });
  route({ method: "Network.webSocketCreated", params: { requestId: "b", url: "wss://example.com/socket" } });
  route({ method: "Network.webSocketFrameSent", params: { requestId: "a", response: { opcode: 1, payloadData: "outbound" } } });
  route({ method: "Network.webSocketFrameReceived", params: { requestId: "b", response: { opcode: 1, payloadData: "other" }, timestamp: 1 } });
  route({ method: "Network.webSocketFrameReceived", params: { requestId: "preexisting", response: { opcode: 1, payloadData: '42/msg,["room_msg","payload"]' }, timestamp: 1 } });
  route({ method: "Network.webSocketFrameReceived", params: { requestId: "a", response: { opcode: 1, payloadData: "42/msg,[]" }, timestamp: 2 } });
  assert.deepEqual(frames, [
    { payloadData: '42/msg,["room_msg","payload"]', receivedAt: 2_000 },
    { payloadData: "42/msg,[]", receivedAt: 2_000 },
  ]);
});
```

- [ ] **Step 2: Run and verify failure**

Run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test tests/unit/cdp-frame-router.test.mjs
```

Expected: FAIL because the router does not exist.

- [ ] **Step 3: Implement the pure router**

```js
// src/ingestion/cdp-frame-router.mjs
export function createMxFrameRouter({
  onFrame,
  socketUrlPattern = /\/business-api\/5\/socket\.io\//,
  now = () => Date.now(),
}) {
  const matchingRequestIds = new Set();
  return (event) => {
    if (event.method === "Network.webSocketCreated") {
      if (socketUrlPattern.test(event.params.url)) matchingRequestIds.add(event.params.requestId);
      return;
    }
    if (event.method !== "Network.webSocketFrameReceived") return;
    if (event.params.response.opcode !== 1) return;
    const payloadData = event.params.response.payloadData;
    const strictRoomSignature =
      payloadData.startsWith("42/msg,") && payloadData.includes('["room_msg",');
    if (!matchingRequestIds.has(event.params.requestId) && !strictRoomSignature) return;
    onFrame({
      payloadData,
      receivedAt: now(),
    });
  };
}
```

- [ ] **Step 4: Implement and test the CDP request/reply client**

```js
// src/ingestion/cdp-client.mjs
export class CdpClient {
  constructor(url, { webSocketFactory = (value) => new WebSocket(value) } = {}) {
    this.socket = webSocketFactory(url);
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Set();
    this.ready = new Promise((resolve, reject) => {
      this.socket.addEventListener("open", resolve, { once: true });
      this.socket.addEventListener("error", () => reject(new Error("CDP connection failed")), { once: true });
    });
    this.closed = new Promise((resolve) => this.socket.addEventListener("close", resolve, { once: true }));
    this.socket.addEventListener("message", ({ data }) => this.#receive(String(data)));
    this.socket.addEventListener("close", () => {
      for (const { reject } of this.pending.values()) reject(new Error("CDP connection closed"));
      this.pending.clear();
    });
  }

  async send(method, params = {}) {
    await this.ready;
    const id = this.nextId++;
    const response = new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
    this.socket.send(JSON.stringify({ id, method, params }));
    return response;
  }

  onEvent(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  close() {
    this.socket.close();
  }

  #receive(data) {
    const message = JSON.parse(data);
    if (message.id != null) {
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      if (message.error) pending.reject(new Error(message.error.message));
      else pending.resolve(message.result);
      return;
    }
    for (const listener of this.listeners) listener(message);
  }
}
```

```js
// tests/unit/cdp-client.test.mjs
import assert from "node:assert/strict";
import test from "node:test";
import { CdpClient } from "../../src/ingestion/cdp-client.mjs";

class FakeSocket extends EventTarget {
  sent = [];
  send(value) { this.sent.push(value); }
  close() { this.dispatchEvent(new Event("close")); }
  open() { this.dispatchEvent(new Event("open")); }
  receive(value) {
    this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(value) }));
  }
}

test("matches CDP responses and forwards events", async () => {
  const socket = new FakeSocket();
  const client = new CdpClient("ws://test", { webSocketFactory: () => socket });
  const events = [];
  client.onEvent((event) => events.push(event));
  socket.open();
  const pending = client.send("Network.enable");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(socket.sent[0], '{"id":1,"method":"Network.enable","params":{}}');
  socket.receive({ id: 1, result: { enabled: true } });
  assert.deepEqual(await pending, { enabled: true });
  socket.receive({ method: "Network.loadingFinished", params: { requestId: "x" } });
  assert.equal(events[0].method, "Network.loadingFinished");
  client.close();
});
```

- [ ] **Step 5: Run all CDP unit tests**

Run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test tests/unit/cdp-frame-router.test.mjs tests/unit/cdp-client.test.mjs
```

Expected: all tests PASS; sent-frame events never reach `onFrame`.

- [ ] **Step 6: Commit**

```bash
git add src/ingestion/cdp-client.mjs src/ingestion/cdp-frame-router.mjs tests/unit/cdp-*.test.mjs
git commit -m "feat: capture inbound frames through Chrome DevTools"
```

---

### Task 6: Collector Service, Reconnect Loop, and Atomic Ingestion

**Files:**
- Create: `src/ingestion/collector.mjs`
- Create: `src/ingestion/find-mx-target.mjs`
- Create: `scripts/run-collector.mjs`
- Create: `tests/integration/collector.test.mjs`

**Interfaces:**
- Consumes: `classifyFrame`, `EventStore`, `CdpClient`, RID configuration.
- Produces: `Collector.acceptFrame({ payloadData, receivedAt }) -> Promise<classification status>`.
- Produces: long-running `scripts/run-collector.mjs --cdp http://127.0.0.1:9222`.

- [ ] **Step 1: Write the integration test with an in-memory frame source**

```js
// tests/integration/collector.test.mjs
import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { Collector } from "../../src/ingestion/collector.mjs";
import { openEventStore } from "../../src/events/event-store.mjs";
import { encodeRoomPayload } from "../helpers/encode-room-payload.mjs";

function frame(rid, text) {
  const payload = encodeRoomPayload({ id: rid, rid, msg: text }, "2026-07-03");
  return `42/msg,["room_msg",${JSON.stringify(payload)}]`;
}

test("collector persists only one unique allowlisted event", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-collector-"));
  const filename = path.join(directory, "events.sqlite");
  const store = openEventStore(filename);
  const collector = new Collector({ allowedRids: new Set([20025]), store });
  const accepted = frame(20025, "allowed-marker");
  assert.equal(await collector.acceptFrame({ payloadData: accepted, receivedAt: Date.parse("2026-07-03T01:00:00Z") }), "accepted");
  assert.equal(await collector.acceptFrame({ payloadData: frame(23200, "rejected-marker"), receivedAt: Date.parse("2026-07-03T01:01:00Z") }), "rejected");
  assert.equal(await collector.acceptFrame({ payloadData: accepted, receivedAt: Date.parse("2026-07-03T01:02:00Z") }), "duplicate");
  assert.equal(await collector.acceptFrame({ payloadData: "42/msg,[\"room_msg\",\"broken\"]", receivedAt: Date.parse("2026-07-03T01:03:00Z") }), "failed");
  assert.equal(await collector.acceptFrame({ payloadData: "2", receivedAt: Date.parse("2026-07-03T01:04:00Z") }), "ignored");
  assert.equal(store.listEventsByRid(20025).length, 1);
  assert.equal(store.database.prepare("SELECT count(*) AS n FROM events").get().n, 1);
  store.close();
  const bytes = await readFile(filename);
  assert.equal(bytes.includes(Buffer.from("rejected-marker")), false);
});
```

- [ ] **Step 2: Run and verify failure**

Run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test tests/integration/collector.test.mjs
```

Expected: FAIL because `Collector` is not defined.

- [ ] **Step 3: Implement atomic classification and storage**

```js
// src/ingestion/collector.mjs
import { randomUUID } from "node:crypto";
import { classifyFrame } from "./classify-frame.mjs";

export class Collector {
  constructor({ allowedRids, store, now = () => Date.now(), onAccepted = () => {} }) {
    this.allowedRids = allowedRids;
    this.store = store;
    this.now = now;
    this.onAccepted = onAccepted;
    this.runId = randomUUID();
  }

  async acceptFrame({ payloadData, receivedAt = this.now() }) {
    if (!payloadData.startsWith("42/msg,") || !payloadData.includes('["room_msg",')) {
      this.store.incrementCounter("ignored", receivedAt);
      return "ignored";
    }
    const result = classifyFrame({ frame: payloadData, receivedAt, allowedRids: this.allowedRids });
    if (result.status !== "accepted") {
      this.store.incrementCounter(result.status, receivedAt);
      return result.status;
    }
    const inserted = this.store.insertEvent(result.event, this.runId);
    const status = inserted ? "accepted" : "duplicate";
    this.store.incrementCounter(status, receivedAt);
    if (inserted) {
      await Promise.resolve(this.onAccepted(result.event)).catch(() => {
        this.store.incrementCounter("media_failed", this.now());
      });
    }
    return status;
  }
}
```

- [ ] **Step 4: Implement target discovery and reconnect**

```js
// src/ingestion/find-mx-target.mjs
export async function findMxTarget(baseUrl, fetchImpl = fetch) {
  const response = await fetchImpl(`${baseUrl}/json/list`);
  if (!response.ok) throw new Error(`CDP target list failed: ${response.status}`);
  const targets = await response.json();
  const target = targets.find(
    (item) => item.url?.startsWith("https://mx.2026.naaifu.cn/") && item.webSocketDebuggerUrl,
  );
  if (!target) throw new Error("MX Chrome target is not open or not logged in");
  return target.webSocketDebuggerUrl;
}
```

```js
// scripts/run-collector.mjs
import { setTimeout as delay } from "node:timers/promises";
import { loadAllowedRids } from "../src/config/load-allowed-rids.mjs";
import { openEventStore } from "../src/events/event-store.mjs";
import { CdpClient } from "../src/ingestion/cdp-client.mjs";
import { createMxFrameRouter } from "../src/ingestion/cdp-frame-router.mjs";
import { Collector } from "../src/ingestion/collector.mjs";
import { findMxTarget } from "../src/ingestion/find-mx-target.mjs";

function option(name, fallback) {
  const index = process.argv.indexOf(name);
  return index < 0 ? fallback : process.argv[index + 1];
}

const cdpBase = option("--cdp", "http://127.0.0.1:9222");
const store = openEventStore("data/state/events.sqlite");
const collector = new Collector({
  allowedRids: loadAllowedRids("config/allowed-rids.yaml"),
  store,
});
let stopping = false;
let client = null;
const pending = new Set();
for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    stopping = true;
    client?.close();
  });
}

const waits = [1_000, 2_000, 4_000, 8_000, 30_000];
let attempt = 0;
try {
  while (!stopping) {
    try {
      const targetUrl = await findMxTarget(cdpBase);
      client = new CdpClient(targetUrl);
      const route = createMxFrameRouter({
        onFrame: (frame) => {
          const task = collector.acceptFrame(frame).finally(() => pending.delete(task));
          pending.add(task);
        },
      });
      client.onEvent(route);
      await client.send("Network.enable");
      attempt = 0;
      await client.closed;
    } catch (error) {
      if (!stopping) process.stderr.write(`${error.message}\n`);
    }
    if (!stopping) await delay(waits[Math.min(attempt++, waits.length - 1)]);
  }
} finally {
  client?.close();
  await Promise.allSettled(pending);
  store.close();
}
```

- [ ] **Step 5: Run integration and offline self-tests**

Run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test tests/integration/collector.test.mjs
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
```

Expected: PASS; `reports/self-test/latest.json` contains `"ok": true`.

- [ ] **Step 6: Commit**

```bash
git add src/ingestion/collector.mjs src/ingestion/find-mx-target.mjs scripts/run-collector.mjs tests/integration/collector.test.mjs
git commit -m "feat: add reconnecting event collector"
```

---

### Task 7: Allowlisted Media Download and Metadata Storage

**Files:**
- Modify: `src/events/schema.sql`
- Modify: `src/events/event-store.mjs`
- Create: `src/media/download-image.mjs`
- Create: `src/media/process-event-media.mjs`
- Create: `tests/unit/download-image.test.mjs`
- Create: `tests/integration/event-media.test.mjs`

**Interfaces:**
- Consumes: image URLs only from accepted canonical events.
- Produces: SHA-256-addressed image files under `data/media/<first-two-hash-chars>/<hash>.<ext>`.
- Produces: `media` rows keyed by `(event_id, url_hash)` with RID as an indexed separate column.

- [ ] **Step 1: Write media security tests**

```js
// tests/unit/download-image.test.mjs
import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { downloadImage } from "../../src/media/download-image.mjs";

const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xd9]);
const response = (bytes, type = "image/jpeg") => new Response(bytes, {
  status: 200,
  headers: { "content-type": type, "content-length": String(bytes.byteLength) },
});

test("requires HTTPS and an image MIME type", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "a-hunter-media-"));
  await assert.rejects(() => downloadImage({ url: "http://example.com/a.jpg", mediaRoot: root }), /HTTPS/);
  await assert.rejects(() => downloadImage({
    url: "https://example.com/a.txt", mediaRoot: root,
    fetchImpl: async () => response(jpeg, "text/plain"),
  }), /content type/);
});

test("deduplicates identical image bytes", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "a-hunter-media-"));
  const fetchImpl = async () => response(jpeg);
  const first = await downloadImage({ url: "https://example.com/a.jpg", mediaRoot: root, fetchImpl });
  const second = await downloadImage({ url: "https://example.com/b.jpg", mediaRoot: root, fetchImpl });
  assert.equal(first.localPath, second.localPath);
});

test("rejects bodies larger than 10 MiB", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "a-hunter-media-"));
  const bytes = new Uint8Array(10 * 1024 * 1024 + 1);
  await assert.rejects(() => downloadImage({
    url: "https://example.com/large.jpg", mediaRoot: root,
    fetchImpl: async () => response(bytes),
  }), /10 MiB/);
});
```

The integration test creates one accepted event with one image and one rejected event with an image marker, invokes media processing only for the inserted accepted event, and asserts `SELECT rid FROM media` returns only the accepted RID and the rejected marker is absent from SQLite bytes.

- [ ] **Step 2: Run and verify failure**

Run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test tests/unit/download-image.test.mjs tests/integration/event-media.test.mjs
```

Expected: FAIL because the media modules do not exist.

- [ ] **Step 3: Add the media schema**

```sql
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
```

- [ ] **Step 4: Implement bounded HTTPS image download**

```js
// src/media/download-image.mjs
import { createHash, randomUUID } from "node:crypto";
import { mkdir, rename, unlink, writeFile } from "node:fs/promises";
import path from "node:path";

const LIMIT = 10 * 1024 * 1024;
const EXTENSIONS = new Map([
  ["image/jpeg", "jpg"], ["image/png", "png"], ["image/webp", "webp"],
  ["image/gif", "gif"], ["image/avif", "avif"],
]);
const sha256 = (value) => createHash("sha256").update(value).digest("hex");

export async function downloadImage({ url, mediaRoot, fetchImpl = fetch }) {
  const source = new URL(url);
  if (source.protocol !== "https:") throw new Error("Image URL must use HTTPS");
  const response = await fetchImpl(source, { redirect: "follow" });
  if (!response.ok) throw new Error(`Image request failed: ${response.status}`);
  const finalUrl = new URL(response.url || source);
  if (finalUrl.protocol !== "https:") throw new Error("Image redirect must use HTTPS");
  const contentType = response.headers.get("content-type")?.split(";")[0].toLowerCase();
  const extension = EXTENSIONS.get(contentType);
  if (!extension) throw new Error(`Unsupported image content type: ${contentType}`);
  const declared = Number(response.headers.get("content-length") || 0);
  if (declared > LIMIT) throw new Error("Image exceeds 10 MiB");
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > LIMIT) throw new Error("Image exceeds 10 MiB");
    chunks.push(value);
  }
  const bytes = Buffer.concat(chunks.map((chunk) => Buffer.from(chunk)));
  const contentHash = sha256(bytes);
  const directory = path.join(mediaRoot, contentHash.slice(0, 2));
  const localPath = path.join(directory, `${contentHash}.${extension}`);
  const temporary = `${localPath}.${randomUUID()}.tmp`;
  await mkdir(directory, { recursive: true });
  await writeFile(temporary, bytes, { flag: "wx" });
  try {
    await rename(temporary, localPath);
  } catch (error) {
    await unlink(temporary).catch(() => {});
    if (error.code !== "EEXIST") throw error;
  }
  return { sourceUrl: source.href, urlHash: sha256(source.href), contentHash, contentType, localPath };
}
```

```js
// src/media/process-event-media.mjs
import { downloadImage } from "./download-image.mjs";

export async function processEventMedia({ event, store, mediaRoot, fetchImpl = fetch, now = () => Date.now() }) {
  const results = [];
  for (const url of event.parsedContent.imageUrls) {
    const media = await downloadImage({ url, mediaRoot, fetchImpl });
    store.insertMedia({ eventId: event.eventId, rid: event.rid, downloadedAt: now(), ...media });
    results.push(media);
  }
  return results;
}
```

Add this prepared statement beside the other EventStore statements:

```js
const insertMediaStatement = database.prepare(`
  INSERT OR IGNORE INTO media(
    event_id, rid, source_url, url_hash, content_hash, content_type, local_path, downloaded_at
  ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
`);
```

Add this method to the returned EventStore object:

```js
insertMedia(media) {
  return insertMediaStatement.run(
    media.eventId, media.rid, media.sourceUrl, media.urlHash,
    media.contentHash, media.contentType, media.localPath, media.downloadedAt,
  ).changes === 1;
},
```

Use the `onAccepted` constructor addition shown in Task 6. In `run-collector.mjs`, add:

```js
import { processEventMedia } from "../src/media/process-event-media.mjs";

const collector = new Collector({
  allowedRids: loadAllowedRids("config/allowed-rids.yaml"),
  store,
  onAccepted: (event) => processEventMedia({ event, store, mediaRoot: "data/media" }),
});
```

- [ ] **Step 5: Run media and collector integration tests**

Run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test tests/unit/download-image.test.mjs tests/integration/event-media.test.mjs tests/integration/collector.test.mjs
```

Expected: all tests PASS; rejected RID image URLs never appear in database bytes or media paths.

- [ ] **Step 6: Commit**

```bash
git add src/events src/media tests/unit/download-image.test.mjs tests/integration/event-media.test.mjs tests/integration/collector.test.mjs
git commit -m "feat: store media for allowlisted events"
```

---

### Task 8: Operational Smoke Test, Legacy Quarantine, and `agents.md`

**Files:**
- Create: `scripts/smoke-test.mjs`
- Modify: `scripts/self-test.mjs`
- Modify: `scripts/mx-websocket/README.md`
- Modify: `agents.md`
- Create: `tests/integration/self-test-report.test.mjs`

**Interfaces:**
- Produces: offline `scripts/self-test.mjs` and opt-in live `scripts/smoke-test.mjs`.
- Documents: exact start, stop, RID configuration, quarantine, testing, recovery, and privacy commands.

- [ ] **Step 1: Write the self-test report integration test**

```js
// tests/integration/self-test-report.test.mjs
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import test from "node:test";

test("self-test report mirrors nested test status", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-self-test-"));
  for (const [name, assertion, expected] of [["pass", "true", 0], ["fail", "false", 1]]) {
    const fixture = path.join(directory, `${name}.test.mjs`);
    const output = path.join(directory, `${name}-report`);
    await writeFile(fixture, `import test from "node:test"; import assert from "node:assert/strict"; test("fixture",()=>assert.equal(${assertion},true));`);
    const result = spawnSync(process.execPath, ["scripts/self-test.mjs", "--tests", fixture, "--output", output]);
    assert.equal(result.status, expected);
    const report = JSON.parse(await readFile(path.join(output, "latest.json"), "utf8"));
    assert.equal(report.schemaVersion, 1);
    assert.equal(report.ok, expected === 0);
    assert.match(await readFile(path.join(output, "latest.md"), "utf8"), expected === 0 ? /PASS/ : /FAIL/);
  }
});
```

- [ ] **Step 2: Run and verify failure**

Run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test tests/integration/self-test-report.test.mjs
```

Expected: FAIL until `self-test.mjs` supports injected test paths and output directories.

- [ ] **Step 3: Make self-test deterministic and add live smoke checks**

Replace `scripts/self-test.mjs` with:

```js
import { mkdir, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import path from "node:path";

function option(name, fallback) {
  const index = process.argv.indexOf(name);
  return index < 0 ? fallback : process.argv[index + 1];
}
const tests = option("--tests", "tests/unit,tests/integration").split(",");
const output = option("--output", "reports/self-test");
const startedAt = new Date().toISOString();
const result = spawnSync(process.execPath, ["--test", ...tests], { encoding: "utf8" });
const report = {
  schemaVersion: 1, startedAt, finishedAt: new Date().toISOString(),
  ok: result.status === 0, exitCode: result.status, stdout: result.stdout, stderr: result.stderr,
};
await mkdir(output, { recursive: true });
await writeFile(path.join(output, "latest.json"), `${JSON.stringify(report, null, 2)}\n`);
await writeFile(path.join(output, "latest.md"), `# Self Test\n\n- Status: ${report.ok ? "PASS" : "FAIL"}\n- Started: ${startedAt}\n- Finished: ${report.finishedAt}\n`);
process.stdout.write(result.stdout);
process.stderr.write(result.stderr);
process.exitCode = result.status ?? 1;
```

Create `scripts/smoke-test.mjs`:

```js
import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { loadAllowedRids } from "../src/config/load-allowed-rids.mjs";
import { openEventStore } from "../src/events/event-store.mjs";
import { findMxTarget } from "../src/ingestion/find-mx-target.mjs";

const checks = [];
async function check(name, action) {
  try { await action(); checks.push({ name, ok: true }); }
  catch (error) { checks.push({ name, ok: false, error: error.message }); }
}
await check("node", () => {
  const [major, minor] = process.versions.node.split(".").map(Number);
  if (major < 24 || (major === 24 && minor < 18)) throw new Error(`Node 24.18+ required, got ${process.versions.node}`);
});
await check("rid-config", () => {
  const values = loadAllowedRids("config/allowed-rids.yaml");
  checks.push({ name: "rid-active", ok: true, active: values.size > 0 });
});
await check("chrome-target", () => findMxTarget("http://127.0.0.1:9222"));
await check("sqlite", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-smoke-"));
  const store = openEventStore(path.join(directory, "events.sqlite"));
  store.incrementCounter("smoke", Date.now());
  store.close();
});
const report = { schemaVersion: 1, checkedAt: new Date().toISOString(), ok: checks.every(({ ok }) => ok), checks };
await mkdir("reports/self-test", { recursive: true });
await writeFile("reports/self-test/smoke-latest.json", `${JSON.stringify(report, null, 2)}\n`);
console.log(JSON.stringify(report, null, 2));
if (!report.ok) process.exitCode = 1;
```

- [ ] **Step 4: Complete operating documentation**

Update `agents.md` with these exact rules:

- Run the collector only with the Node 24 absolute path.
- Treat empty RID config as intentionally inactive.
- Never add a RID inferred from observed traffic; only the user supplies it.
- Run `scripts/quarantine-legacy-output.mjs` once before the first collector start.
- Run offline self-test before every start and after every code change.
- Run live smoke test only when Chrome debugging is already enabled by the user.
- Never use Computer Use for this workflow; use Chrome DevTools only.
- Stop and ask the user to log in when authorization expires.
- Do not generate reports from a failed data-quality run.

Update `scripts/mx-websocket/README.md` with exact commands:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/smoke-test.mjs
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/quarantine-legacy-output.mjs
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/run-collector.mjs --cdp http://127.0.0.1:9222
```

- [ ] **Step 5: Run the complete acceptance suite**

Run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/smoke-test.mjs
git diff --check
```

Expected: offline report PASS; live smoke PASS with the already-open MX Chrome page; `git diff --check` emits no output.

- [ ] **Step 6: Commit**

```bash
git add agents.md scripts tests/integration/self-test-report.test.mjs
git commit -m "docs: add event foundation operations and self-test"
```

---

## Phase 1 Completion Gate

Phase 1 is complete only when all of the following are true:

1. `config/allowed-rids.yaml` exists and is empty by default.
2. With an empty list, collector integration tests prove zero content rows and zero media files.
3. With a configured synthetic RID, exactly that RID is persisted as an indexed integer column.
4. Rejected and undecodable content cannot be found in SQLite bytes, JSON outputs, logs, or media paths.
5. Duplicate frames produce one event.
6. Raw accepted payloads expire after 30 days while hashes remain.
7. Collector reconnects without navigating or operating the page.
8. Offline self-test and live smoke reports are written and pass.
9. Existing decoded output is preserved in quarantine and excluded from the new event ledger.
10. `agents.md` contains the operating and safety rules above.

After this gate, create separate detailed plans for: Tushare backfill and evidence packs; simulated portfolio, reports, and deterministic evaluation; Champion/Challenger review and promotion.
