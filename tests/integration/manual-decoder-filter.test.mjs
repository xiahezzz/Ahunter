import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { encodeRoomPayload } from "../helpers/encode-room-payload.mjs";

function existingMessage(rid, marker) {
  return {
    decodedAt: "2026-07-03T01:01:00.000Z",
    sourceDate: "2026-07-03",
    value: rid === undefined
      ? { id: marker, msg: marker }
      : { id: marker, rid, msg: marker },
    content: { parsed: marker, texts: [marker], imageUrls: [] },
  };
}

async function runDecoder({ allowedRids, existingMessages = [] }) {
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-cli-"));
  const input = path.join(directory, "frames.json");
  const output = path.join(directory, "decoded.json");
  const config = path.join(directory, "allowed.yaml");
  await writeFile(input, "[]");
  await writeFile(
    output,
    JSON.stringify({
      schemaVersion: 1,
      updatedAt: "2026-07-03T01:01:00.000Z",
      messages: existingMessages,
    }),
  );
  await writeFile(config, `allowed_rids: [${allowedRids.join(", ")}]\n`);

  const result = spawnSync(process.execPath, [
    "scripts/mx-websocket/decode-room-msg.mjs",
    "--input",
    input,
    "--output",
    output,
    "--config",
    config,
    "--date",
    "2026-07-03",
  ], { encoding: "utf8" });

  assert.equal(result.status, 0, result.stderr);
  return readFile(output, "utf8");
}

test("manual decoder writes only configured RID", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-cli-"));
  const input = path.join(directory, "frames.json");
  const output = path.join(directory, "decoded.json");
  const config = path.join(directory, "allowed.yaml");
  const make = (rid, msg) =>
    `42/msg,["room_msg",${JSON.stringify(
      encodeRoomPayload({ id: rid, rid, msg }, "2026-07-03"),
    )}]`;
  await writeFile(
    input,
    JSON.stringify([
      make(20025, "allowed-marker"),
      make(23200, "rejected-marker"),
    ]),
  );
  await writeFile(config, "allowed_rids: [20025]\n");

  const result = spawnSync(process.execPath, [
    "scripts/mx-websocket/decode-room-msg.mjs",
    "--input",
    input,
    "--output",
    output,
    "--config",
    config,
    "--date",
    "2026-07-03",
  ], { encoding: "utf8" });

  assert.equal(result.status, 0, result.stderr);
  const text = await readFile(output, "utf8");
  assert.equal(JSON.parse(text).messages.length, 1);
  assert.equal(text.includes("allowed-marker"), true);
  assert.equal(text.includes("rejected-marker"), false);
});

test("manual decoder removes existing records excluded by a changed allowlist", async () => {
  const text = await runDecoder({
    allowedRids: [20025],
    existingMessages: [
      existingMessage(20025, "existing-allowed-marker"),
      existingMessage(23200, "existing-rejected-marker"),
      existingMessage("20025", "existing-invalid-marker"),
      existingMessage(undefined, "existing-missing-marker"),
    ],
  });

  assert.equal(JSON.parse(text).messages.length, 1);
  assert.equal(text.includes("existing-allowed-marker"), true);
  assert.equal(text.includes("existing-rejected-marker"), false);
  assert.equal(text.includes("existing-invalid-marker"), false);
  assert.equal(text.includes("existing-missing-marker"), false);
});

test("manual decoder removes all existing records for an empty allowlist", async () => {
  const text = await runDecoder({
    allowedRids: [],
    existingMessages: [
      existingMessage(20025, "empty-allowlist-rejected-marker"),
    ],
  });

  assert.equal(JSON.parse(text).messages.length, 0);
  assert.equal(text.includes("empty-allowlist-rejected-marker"), false);
});
