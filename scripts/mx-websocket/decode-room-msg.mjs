#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import CryptoJS from "crypto-js";
import LZString from "lz-string";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
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
    "Usage: node decode-room-msg.mjs --input <raw.json> [--date YYYY-MM-DD] [--output decoded.json]",
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

function payloadFromFrame(frame) {
  if (!frame.startsWith("42/msg,")) return frame;

  const arrayStart = frame.indexOf("[");
  if (arrayStart < 0) throw new Error("Socket.IO event payload is missing");
  const packet = JSON.parse(frame.slice(arrayStart));
  if (packet[0] !== "room_msg" || typeof packet[1] !== "string") {
    throw new Error("Socket.IO event is not room_msg");
  }
  return packet[1];
}

function derivedKey(dateString) {
  const digest = CryptoJS.MD5(dateString).toString();
  return {
    key: CryptoJS.enc.Utf8.parse(digest.slice(0, 16)),
    iv: CryptoJS.enc.Utf8.parse(digest.slice(8, 14)),
  };
}

function decodePayload(payload, dateString) {
  const decompressed = LZString.decompress(payload);
  if (!decompressed) throw new Error("LZString decompression failed");

  const { key, iv } = derivedKey(dateString);
  const plaintext = CryptoJS.AES.decrypt(decompressed, key, {
    iv,
    mode: CryptoJS.mode.CBC,
    padding: CryptoJS.pad.Pkcs7,
  }).toString(CryptoJS.enc.Utf8);

  if (!plaintext) throw new Error("AES decryption produced no plaintext");
  return JSON.parse(plaintext);
}

function parseNestedMessage(message) {
  if (typeof message !== "string") return message;
  const trimmed = message.trim();
  if (!trimmed.startsWith("[") && !trimmed.startsWith("{")) return message;

  try {
    return JSON.parse(trimmed);
  } catch {
    return message;
  }
}

function extractContent(message) {
  const parsed = parseNestedMessage(message);
  const texts = [];
  const imageUrls = [];

  function visit(value) {
    if (typeof value === "string") {
      texts.push(value);
      for (const match of value.matchAll(
        /https?:\/\/[^\s"'<>]+?\.(?:avif|gif|jpe?g|png|webp)(?:\?[^\s"'<>]*)?/gi,
      )) {
        imageUrls.push(match[0]);
      }
      return;
    }
    if (Array.isArray(value)) {
      value.forEach(visit);
      return;
    }
    if (!value || typeof value !== "object") return;

    const type = String(value.type || "").toLowerCase();
    const url = value.url || value.src;
    if (["pic", "image", "img"].includes(type) && typeof url === "string") {
      imageUrls.push(url);
    }
    if (type === "text" && typeof value.msg === "string") {
      texts.push(value.msg);
    }

    for (const [key, child] of Object.entries(value)) {
      if (["url", "src", "msg", "type"].includes(key)) continue;
      visit(child);
    }
  }

  visit(parsed);
  return {
    parsed,
    texts: [...new Set(texts)],
    imageUrls: [...new Set(imageUrls)],
  };
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
    const value = decodePayload(payloadFromFrame(frame), frameDate);
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
