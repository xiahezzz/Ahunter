import CryptoJS from "crypto-js";
import LZString from "lz-string";
import { normalizeMxContent } from "./content-normalizer.mjs";

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

export function parseNestedMessage(message) {
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
  const parsed = normalizeMxContent(parseNestedMessage(message));
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

export function decodeRoomFrame(frame, dateString) {
  return decodePayload(payloadFromFrame(frame), dateString);
}

export { extractContent };
