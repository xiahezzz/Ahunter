import assert from "node:assert/strict";
import test from "node:test";
import { runCollectorLoop } from "../../src/ingestion/collector-runner.mjs";

class FakeClient {
  constructor(onSend) {
    this.onSend = onSend;
    this.closeCalls = 0;
    this.commands = [];
    this.closed = new Promise((resolve) => {
      this.resolveClosed = resolve;
    });
  }

  onEvent(listener) {
    this.listener = listener;
  }

  async send(method) {
    this.commands.push(method);
    this.onSend(this.listener);
  }

  close() {
    this.closeCalls += 1;
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
  assert.equal(clients[0].closeCalls, 1);
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

test("collector loop caps connection-failure backoff at thirty seconds", async () => {
  const abortController = new AbortController();
  const delays = [];
  await runCollectorLoop({
    cdpBase: "http://127.0.0.1:9222",
    collector: { acceptFrame() {} },
    signal: abortController.signal,
    findTarget: async () => {
      throw new Error("unavailable");
    },
    delay: async (milliseconds) => {
      delays.push(milliseconds);
      if (delays.length === 7) abortController.abort();
    },
    log: () => {},
  });

  assert.deepEqual(delays, [1_000, 2_000, 4_000, 8_000, 30_000, 30_000, 30_000]);
});

test("collector loop reconnects and sends only Network.enable", async () => {
  const abortController = new AbortController();
  const clients = [];
  const delays = [];
  await runCollectorLoop({
    cdpBase: "http://127.0.0.1:9222",
    collector: { acceptFrame() {} },
    signal: abortController.signal,
    findTarget: async () => "ws://mx",
    createClient: () => {
      const client = new FakeClient(() => client.resolveClosed());
      clients.push(client);
      return client;
    },
    delay: async (milliseconds) => {
      delays.push(milliseconds);
      if (clients.length === 3) abortController.abort();
    },
  });

  assert.equal(clients.length, 3);
  assert.deepEqual(
    clients.map(({ commands }) => commands),
    [["Network.enable"], ["Network.enable"], ["Network.enable"]],
  );
  assert.deepEqual(delays, [1_000, 1_000, 1_000]);
  assert.deepEqual(
    clients.map(({ closeCalls }) => closeCalls),
    [1, 1, 1],
  );
});
