import assert from "node:assert/strict";
import test from "node:test";
import {
  decodeRoomFrame,
  extractContent,
} from "../../src/ingestion/room-codec.mjs";
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
