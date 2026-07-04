import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import { classifyFrame } from "../../src/ingestion/classify-frame.mjs";
import { MX_DISCLAIMER } from "../../src/ingestion/content-normalizer.mjs";
import { encodeRoomPayload } from "../helpers/encode-room-payload.mjs";

const receivedAt = Date.parse("2026-07-03T09:01:00+08:00");

function frameFor(rid) {
  const payload = encodeRoomPayload(
    { id: 1, oid: 2, rid, msg: "仅目标可见" },
    "2026-07-03",
  );
  return `42/msg,["room_msg",${JSON.stringify(payload)}]`;
}

test("accepts an allowlisted RID and stores RID separately", () => {
  const result = classifyFrame({
    frame: frameFor(20025),
    receivedAt,
    allowedRids: new Set([20025]),
  });
  assert.equal(result.status, "accepted");
  assert.equal(result.event.rid, 20025);
  assert.equal(result.event.decodedText, "仅目标可见");
});

test("stores cleaned MX content as a schema-version-2 event", () => {
  const msg = JSON.stringify([
    { type: "pic", url: "https://example.com/evidence.png" },
    { type: "text", msg: MX_DISCLAIMER },
  ]);
  const payload = encodeRoomPayload({ id: 1, rid: 20025, msg }, "2026-07-03");
  const frame = `42/msg,["room_msg",${JSON.stringify(payload)}]`;

  const result = classifyFrame({
    frame,
    receivedAt,
    allowedRids: new Set([20025]),
  });

  assert.equal(result.event.schemaVersion, 2);
  assert.equal(result.event.decodedText, "");
  assert.deepEqual(result.event.parsedContent.imageUrls, [
    "https://example.com/evidence.png",
  ]);
  assert.equal(
    JSON.stringify(result.event.parsedContent).includes(MX_DISCLAIMER),
    false,
  );
});

test("preserves legacy fallback identity when cleaning changes content", () => {
  const rawParsed = [{ type: "text", msg: MX_DISCLAIMER }];
  const payload = encodeRoomPayload(
    { rid: 20025, msg: JSON.stringify(rawParsed) },
    "2026-07-03",
  );
  const frame = `42/msg,["room_msg",${JSON.stringify(payload)}]`;
  const legacyContentHash = createHash("sha256")
    .update(JSON.stringify(rawParsed))
    .digest("hex");
  const expectedEventId = createHash("sha256")
    .update(`20025:${receivedAt}:${legacyContentHash}`)
    .digest("hex");

  const result = classifyFrame({
    frame,
    receivedAt,
    allowedRids: new Set([20025]),
  });

  assert.equal(result.event.eventId, expectedEventId);
});

test("rejects non-allowlisted content without retaining it", () => {
  const result = classifyFrame({
    frame: frameFor(23200),
    receivedAt,
    allowedRids: new Set([20025]),
  });
  assert.deepEqual(Object.keys(result).sort(), ["reason", "status"]);
  assert.equal(JSON.stringify(result).includes("仅目标可见"), false);
});

test("empty allowlist rejects all content", () => {
  assert.equal(
    classifyFrame({
      frame: frameFor(20025),
      receivedAt,
      allowedRids: new Set(),
    }).status,
    "rejected",
  );
});

test("decode failure returns only a hash and error class", () => {
  const result = classifyFrame({
    frame: "not-decodable",
    receivedAt,
    allowedRids: new Set([20025]),
  });
  assert.deepEqual(Object.keys(result).sort(), [
    "errorClass",
    "payloadHash",
    "status",
  ]);
  assert.match(result.payloadHash, /^[a-f0-9]{64}$/);
});
