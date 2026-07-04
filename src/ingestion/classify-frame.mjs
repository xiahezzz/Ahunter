import { createHash } from "node:crypto";
import {
  decodeRoomFrame,
  extractContent,
  parseNestedMessage,
} from "./room-codec.mjs";

function localDateString(timestamp) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(timestamp));
  const value = Object.fromEntries(
    parts.map(({ type, value: partValue }) => [type, partValue]),
  );
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

    const identityParsed = parseNestedMessage(value.msg);
    const identityContentHash = hash(JSON.stringify(identityParsed));
    const content = extractContent(identityParsed);
    const contentHash = hash(JSON.stringify(content.parsed));
    const sourceIdentity =
      value.id ??
      value.oid ??
      `${value.createtime ?? receivedAt}:${identityContentHash}`;
    return {
      status: "accepted",
      event: {
        eventId: hash(`${value.rid}:${sourceIdentity}`),
        schemaVersion: 2,
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
