import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { promisify } from "node:util";

import { openEventStore } from "../../src/events/event-store.mjs";
import { migrateDisclaimerContent } from "../../src/events/migrate-disclaimer-content.mjs";
import { MX_DISCLAIMER } from "../../src/ingestion/content-normalizer.mjs";

const receivedAt = Date.parse("2026-07-03T00:00:00Z");
const imageBytes = new Uint8Array([0xff, 0xd8, 0xff, 0xd9]);
const execFileAsync = promisify(execFile);
const migrationScript = path.resolve("scripts/migrate-disclaimer-content.mjs");

function hash(value) {
  return createHash("sha256").update(value).digest("hex");
}

function event(eventId, parsed) {
  return {
    eventId,
    schemaVersion: 1,
    rid: 20025,
    sourceMessageId: eventId,
    oid: null,
    receivedAt,
    sourceCreatedAt: receivedAt,
    rawPayloadHash: hash(`raw:${eventId}`),
    rawPayload: `encrypted:${eventId}`,
    decodedText: JSON.stringify(parsed),
    parsedContent: {
      parsed,
      texts: [MX_DISCLAIMER],
      imageUrls: [],
    },
    contentHash: hash(JSON.stringify(parsed)),
  };
}

async function fixture() {
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-disclaimer-migration-"));
  const filename = path.join(directory, "events.sqlite");
  const imagePath = path.join(directory, "image.jpg");
  await writeFile(imagePath, imageBytes);
  return { directory, filename, imagePath };
}

test("migrates historical disclaimer content without changing event or media identity", async (t) => {
  const { directory, filename, imagePath } = await fixture();
  const store = openEventStore(filename);
  t.after(async () => {
    try {
      store.close();
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  });

  const bodyParsed = [
    { type: "text", msg: "正文" },
    { type: "text", msg: MX_DISCLAIMER },
  ];
  const imageUrl = "https://images.example.com/historical.jpg";
  const imageParsed = [
    { type: "pic", url: imageUrl },
    { type: "text", msg: MX_DISCLAIMER },
  ];
  const imageEvent = event("image-event", imageParsed);
  imageEvent.parsedContent.imageUrls = [imageUrl];

  const bodyEvent = event("body-event", bodyParsed);
  bodyEvent.oid = "body-oid";
  imageEvent.oid = "image-oid";
  assert.equal(store.insertEvent(bodyEvent, "migration-run"), true);
  assert.equal(store.insertEvent(imageEvent, "migration-run"), true);
  const urlHash = hash(imageUrl);
  const imageHashBefore = hash(await readFile(imagePath));
  store.completeMediaJob({
    eventId: "image-event",
    rid: 20025,
    sourceUrl: imageUrl,
    urlHash,
    contentHash: imageHashBefore,
    contentType: "image/jpeg",
    localPath: imagePath,
    downloadedAt: receivedAt,
  });

  const identityQuery = `
    SELECT event_id, rid, source_message_id, oid, received_at, source_created_at,
           raw_payload_hash, raw_payload, raw_payload_expires_at
    FROM events
    ORDER BY event_id
  `;
  const identityBefore = store.database.prepare(identityQuery).all();
  const report = migrateDisclaimerContent(store.database);

  assert.equal(report.scanned, 2);
  assert.equal(report.updated, 2);
  assert.equal(report.eventsBefore, report.eventsAfter);
  assert.equal(report.mediaBefore, report.mediaAfter);
  assert.equal(report.mediaJobsBefore, report.mediaJobsAfter);
  assert.equal(report.orphansAfter, 0);
  assert.deepEqual(store.database.prepare(identityQuery).all(), identityBefore);

  const rows = store.database
    .prepare(`
      SELECT event_id, schema_version, decoded_text, parsed_content_json, content_hash
      FROM events
      ORDER BY event_id
    `)
    .all();
  assert.deepEqual(rows.map(({ schema_version: version }) => version), [2, 2]);
  for (const row of rows) {
    assert.equal(row.decoded_text.includes(MX_DISCLAIMER), false);
    assert.equal(row.parsed_content_json.includes(MX_DISCLAIMER), false);
  }
  const bodyRow = rows.find(({ event_id: id }) => id === "body-event");
  const imageRow = rows.find(({ event_id: id }) => id === "image-event");
  assert.equal(bodyRow.decoded_text, "正文");
  assert.equal(bodyRow.content_hash, hash(JSON.stringify([{ type: "text", msg: "正文" }])));
  assert.equal(imageRow.decoded_text, "");
  assert.equal(imageRow.content_hash, hash(JSON.stringify([{ type: "pic", url: imageUrl }])));

  const media = store.database
    .prepare(`
      SELECT m.local_path, m.content_hash
      FROM media AS m
      JOIN events AS e ON e.event_id = m.event_id
      WHERE e.event_id = 'image-event'
    `)
    .get();
  assert.equal(media.local_path, imagePath);
  assert.equal(media.content_hash, imageHashBefore);
  assert.equal(hash(await readFile(media.local_path)), imageHashBefore);
  assert.equal(migrateDisclaimerContent(store.database).updated, 0);
});

test("rolls back all event updates when a later historical row is corrupt", async (t) => {
  const { directory, filename } = await fixture();
  const store = openEventStore(filename);
  t.after(async () => {
    try {
      store.close();
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  });
  const originalParsed = [{ type: "text", msg: "原文" }, { type: "text", msg: MX_DISCLAIMER }];
  const original = event("a-first", originalParsed);

  store.insertEvent(original, "rollback-run");
  store.insertEvent(event("broken", "会损坏"), "rollback-run");
  store.database.exec("UPDATE events SET parsed_content_json = '{' WHERE event_id = 'broken'");

  assert.throws(() => migrateDisclaimerContent(store.database));
  const first = store.database
    .prepare("SELECT schema_version, decoded_text, parsed_content_json FROM events WHERE event_id = 'a-first'")
    .get();
  assert.equal(first.schema_version, 1);
  assert.equal(first.decoded_text, original.decodedText);
  assert.equal(first.parsed_content_json, JSON.stringify(original.parsedContent));
});

test("CLI prints aggregate-only JSON and redacts rejected arguments", async (t) => {
  const { directory, filename } = await fixture();
  let store = openEventStore(filename);
  t.after(async () => {
    try {
      store?.close();
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  });
  store.insertEvent(
    event("cli-secret-event", [{ type: "text", msg: "secret body" }, { type: "text", msg: MX_DISCLAIMER }]),
    "cli-run",
  );
  store.close();
  store = undefined;

  const { stdout, stderr } = await execFileAsync(process.execPath, [
    migrationScript,
    "--database",
    filename,
  ]);
  assert.equal(stderr, "");
  assert.deepEqual(JSON.parse(stdout), {
    scanned: 1,
    updated: 1,
    eventsBefore: 1,
    eventsAfter: 1,
    mediaBefore: 0,
    mediaAfter: 0,
    mediaJobsBefore: 0,
    mediaJobsAfter: 0,
    orphansBefore: 0,
    orphansAfter: 0,
  });
  assert.equal(stdout.includes("secret body"), false);
  assert.equal(stdout.includes("cli-secret-event"), false);

  await assert.rejects(
    execFileAsync(process.execPath, [migrationScript, "--database"]),
    ({ stderr: missingValueError }) => {
      assert.match(missingValueError, /requires a value/);
      return true;
    },
  );

  await assert.rejects(
    execFileAsync(process.execPath, [migrationScript, "--database", ""]),
    ({ stdout: emptyStdout, stderr: emptyValueError }) => {
      assert.equal(emptyStdout, "");
      assert.match(emptyValueError, /requires a value/);
      return true;
    },
  );

  const optionToken = "--secret-debugger-token";
  await assert.rejects(
    execFileAsync(process.execPath, [migrationScript, "--database", optionToken]),
    ({ stdout: optionStdout, stderr: optionValueError }) => {
      assert.equal(optionStdout.includes(optionToken), false);
      assert.equal(optionValueError.includes(optionToken), false);
      assert.match(optionValueError, /requires a value/);
      return true;
    },
  );

  const secretArgument = "https://private.example/event-id-secret-debugger";
  await assert.rejects(
    execFileAsync(process.execPath, [migrationScript, secretArgument]),
    ({ stdout: unexpectedStdout, stderr: unexpectedError }) => {
      assert.equal(unexpectedStdout.includes(secretArgument), false);
      assert.equal(unexpectedError.includes(secretArgument), false);
      assert.equal(unexpectedError, "Unexpected argument\n");
      return true;
    },
  );
});
