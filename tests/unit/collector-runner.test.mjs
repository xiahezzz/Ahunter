import assert from "node:assert/strict";
import test from "node:test";
import { runCollectorLoop } from "../../src/ingestion/collector-runner.mjs";

class FakeClient {
  constructor(onSend) {
    this.onSend = onSend;
    this.closed = new Promise((resolve) => {
      this.resolveClosed = resolve;
    });
  }

  onEvent(listener) {
    this.listener = listener;
  }

  async send() {
    this.onSend(this.listener);
  }

  close() {
    this.resolveClosed();
  }
}

test("collector loop drains accepted work before returning on shutdown", async () => {
  const abortController = new AbortController();
  let release;
  const accepted = new Promise((resolve) => {
    release = resolve;
  });
  const collector = { acceptFrame: () => accepted };
  const clients = [];
  const running = runCollectorLoop({
    cdpBase: "http://127.0.0.1:9222",
    collector,
    signal: abortController.signal,
    findTarget: async () => "ws://mx",
    createClient: () => {
      const client = new FakeClient((listener) => {
        listener({
          method: "Network.webSocketFrameReceived",
          params: {
            requestId: "existing",
            response: {
              opcode: 1,
              payloadData: '42/msg,["room_msg","private-marker"]',
            },
          },
        });
        abortController.abort();
      });
      clients.push(client);
      return client;
    },
  });

  let settled = false;
  running.finally(() => {
    settled = true;
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(settled, false);
  assert.equal(clients.length, 1);
  release("accepted");
  await running;
  assert.equal(settled, true);
});

test("collector loop logs ingestion failures without payload content", async () => {
  const abortController = new AbortController();
  const messages = [];
  await runCollectorLoop({
    cdpBase: "http://127.0.0.1:9222",
    collector: {
      async acceptFrame() {
        throw new Error("private-marker");
      },
    },
    signal: abortController.signal,
    findTarget: async () => "ws://mx",
    createClient: () =>
      new FakeClient((listener) => {
        listener({
          method: "Network.webSocketFrameReceived",
          params: {
            requestId: "existing",
            response: {
              opcode: 1,
              payloadData: '42/msg,["room_msg","private-marker"]',
            },
          },
        });
        abortController.abort();
      }),
    log: (message) => messages.push(message),
  });

  assert.deepEqual(messages, ["Frame ingestion failed (Error)"]);
  assert.equal(messages.join(" ").includes("private-marker"), false);
});
