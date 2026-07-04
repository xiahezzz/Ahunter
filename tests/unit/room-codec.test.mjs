import assert from "node:assert/strict";
import test from "node:test";
import {
  decodeRoomFrame,
  extractContent,
} from "../../src/ingestion/room-codec.mjs";
import { MX_DISCLAIMER } from "../../src/ingestion/content-normalizer.mjs";
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

test("removes the MX disclaimer while preserving image evidence", () => {
  const message = JSON.stringify([
    { type: "pic", url: "https://example.com/evidence.png" },
    { type: "text", msg: MX_DISCLAIMER },
  ]);
  assert.deepEqual(extractContent(message), {
    parsed: [{ type: "pic", url: "https://example.com/evidence.png" }],
    texts: [],
    imageUrls: ["https://example.com/evidence.png"],
  });
});

test("preserves longer text that quotes the MX disclaimer", () => {
  const quoted = `正文引用：${MX_DISCLAIMER}`;
  assert.deepEqual(extractContent(quoted).texts, [quoted]);
});

test("rejects a frame with the wrong event name", () => {
  assert.throws(() => decodeRoomFrame('42/msg,["va",1]', "2026-07-03"), /room_msg/);
});
