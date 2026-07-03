#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  decodeRoomFrame,
  extractContent,
} from "../../src/ingestion/room-codec.mjs";
import { loadAllowedRids } from "../../src/config/load-allowed-rids.mjs";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const configPath = path.resolve(
  option("--config") || path.join(scriptDirectory, "../../config/allowed-rids.yaml"),
);
const allowedRids = loadAllowedRids(configPath);
const defaultOutputPath = path.resolve(
  scriptDirectory,
  "../../data/mx-websocket/decoded-room-messages.json",
);

function option(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : undefined;
}

function usage() {
  console.error(
    "Usage: node decode-room-msg.mjs --input <raw.json> [--date YYYY-MM-DD] [--output decoded.json] [--config allowed-rids.yaml]",
  );
}

function localDateString(date = new Date()) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function rawString(item) {
  if (typeof item === "string") return item;
  if (item && typeof item.data === "string") return item.data;
  if (item && typeof item.payload === "string") return item.payload;
  return null;
}

function itemDateString(item, fallback) {
  if (!item || typeof item !== "object" || item.at == null) return fallback;
  const date = new Date(item.at);
  return Number.isNaN(date.getTime()) ? fallback : localDateString(date);
}

const inputPath = option("--input");
if (!inputPath) {
  usage();
  process.exit(1);
}

const dateString = option("--date") || localDateString();
const outputPath = path.resolve(option("--output") || defaultOutputPath);
const input = JSON.parse(fs.readFileSync(path.resolve(inputPath), "utf8"));
const items = Array.isArray(input) ? input : [input];

const messages = items.map((item, index) => {
  try {
    const frame = rawString(item);
    if (!frame) throw new Error("Input item has no string frame or payload");
    const frameDate = itemDateString(item, dateString);
    const value = decodeRoomFrame(frame, frameDate);
    if (!Number.isSafeInteger(value.rid) || !allowedRids.has(value.rid)) {
      return { index, ok: false, rejected: true, error: "rid_not_allowed" };
    }
    return {
      index,
      ok: true,
      decodedAt: new Date().toISOString(),
      sourceDate: frameDate,
      value,
      content: extractContent(value.msg),
    };
  } catch (error) {
    return {
      index,
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    };
  }
});

function messageIdentity(message) {
  const value = message.value || {};
  if (value.id != null) return `id:${value.id}`;
  if (value.oid != null) return `oid:${value.oid}`;
  return `fallback:${value.rid ?? value.mb ?? ""}:${value.createtime ?? ""}:${value.msg ?? ""}`;
}

let existingMessages = [];
if (fs.existsSync(outputPath)) {
  const existing = JSON.parse(fs.readFileSync(outputPath, "utf8"));
  if (!Array.isArray(existing.messages)) {
    throw new Error(`Existing output has an invalid schema: ${outputPath}`);
  }
  existingMessages = existing.messages;
}

const successfulMessages = messages
  .filter(({ ok }) => ok)
  .map(({ ok: _ok, index: _index, ...message }) => message);
const merged = new Map(
  existingMessages.map((message) => [messageIdentity(message), message]),
);
for (const message of successfulMessages) {
  merged.set(messageIdentity(message), message);
}

const output = {
  schemaVersion: 1,
  updatedAt: new Date().toISOString(),
  messages: [...merged.values()].sort(
    (a, b) => Number(a.value?.createtime || 0) - Number(b.value?.createtime || 0),
  ),
};

fs.mkdirSync(path.dirname(outputPath), { recursive: true });
const temporaryPath = `${outputPath}.tmp`;
fs.writeFileSync(temporaryPath, `${JSON.stringify(output, null, 2)}\n`);
fs.renameSync(temporaryPath, outputPath);

const failed = messages.filter(({ ok }) => !ok).length;
console.error(
  `Stored ${successfulMessages.length} decoded message(s), ${output.messages.length} total; ${failed} frame(s) failed.`,
);
console.error(`Fixed output: ${outputPath}`);
