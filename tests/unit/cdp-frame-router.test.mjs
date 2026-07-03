import assert from "node:assert/strict";
import test from "node:test";
import { createMxFrameRouter } from "../../src/ingestion/cdp-frame-router.mjs";

test("routes only inbound MX socket text frames", () => {
  const frames = [];
  const route = createMxFrameRouter({
    onFrame: (frame) => frames.push(frame),
    now: () => 2_000,
  });

  route({
    method: "Network.webSocketCreated",
    params: {
      requestId: "a",
      url: "wss://mx.2026.naaifu.cn/business-api/5/socket.io/?EIO=4&transport=websocket",
    },
  });
  route({
    method: "Network.webSocketCreated",
    params: { requestId: "b", url: "wss://example.com/socket" },
  });
  route({
    method: "Network.webSocketFrameSent",
    params: {
      requestId: "a",
      response: { opcode: 1, payloadData: "outbound" },
    },
  });
  route({
    method: "Network.webSocketFrameReceived",
    params: {
      requestId: "a",
      response: { opcode: 2, payloadData: "binary" },
    },
  });
  route({
    method: "Network.webSocketFrameReceived",
    params: {
      requestId: "b",
      response: { opcode: 1, payloadData: "other" },
      timestamp: 1,
    },
  });
  route({
    method: "Network.webSocketFrameReceived",
    params: {
      requestId: "preexisting",
      response: {
        opcode: 1,
        payloadData: '42/msg,["room_msg","payload"]',
      },
      timestamp: 1,
    },
  });
  route({
    method: "Network.webSocketFrameReceived",
    params: {
      requestId: "a",
      response: { opcode: 1, payloadData: "42/msg,[]" },
      timestamp: 2,
    },
  });

  assert.deepEqual(frames, [
    { payloadData: '42/msg,["room_msg","payload"]', receivedAt: 2_000 },
    { payloadData: "42/msg,[]", receivedAt: 2_000 },
  ]);
});

test("requires the exact room message signature for unknown sockets", () => {
  const frames = [];
  const route = createMxFrameRouter({ onFrame: (frame) => frames.push(frame) });

  for (const payloadData of [
    '42/msg,["other","room_msg",{}]',
    'prefix42/msg,["room_msg",{}]',
    '42/msg, ["room_msg",{}]',
  ]) {
    route({
      method: "Network.webSocketFrameReceived",
      params: {
        requestId: "unknown",
        response: { opcode: 1, payloadData },
      },
    });
  }

  assert.deepEqual(frames, []);
});
