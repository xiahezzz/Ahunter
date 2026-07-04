import assert from "node:assert/strict";
import test from "node:test";
import { CdpClient } from "../../src/ingestion/cdp-client.mjs";

class FakeSocket extends EventTarget {
  sent = [];
  closeCalls = 0;

  send(value) {
    this.sent.push(value);
  }

  close() {
    this.closeCalls += 1;
    this.dispatchEvent(new Event("close"));
  }

  open() {
    this.dispatchEvent(new Event("open"));
  }

  receive(value) {
    this.dispatchEvent(
      new MessageEvent("message", { data: JSON.stringify(value) }),
    );
  }
}

test("times out a half-open WebSocket and closes it once", async () => {
  const socket = new FakeSocket();
  const client = new CdpClient("ws://test", {
    webSocketFactory: () => socket,
    openTimeoutMs: 5,
  });
  await assert.rejects(client.send("Network.enable"), /open timeout/i);
  client.close();
  assert.equal(socket.closeCalls, 1);
});

test("times out a CDP command that never responds", async () => {
  const socket = new FakeSocket();
  const client = new CdpClient("ws://test", {
    webSocketFactory: () => socket,
    commandTimeoutMs: 5,
  });
  socket.open();
  await assert.rejects(client.send("Network.enable"), /command timeout/i);
  assert.equal(client.pending.size, 0);
  client.close();
  client.close();
  assert.equal(socket.closeCalls, 1);
});

function createClient() {
  const socket = new FakeSocket();
  const client = new CdpClient("ws://test", {
    webSocketFactory: () => socket,
  });
  return { client, socket };
}

test("matches CDP responses and forwards events", async () => {
  const { client, socket } = createClient();
  const events = [];
  const unsubscribe = client.onEvent((event) => events.push(event));
  socket.open();

  const pending = client.send("Network.enable");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(
    socket.sent[0],
    '{"id":1,"method":"Network.enable","params":{}}',
  );

  socket.receive({ id: 1, result: { enabled: true } });
  assert.deepEqual(await pending, { enabled: true });

  socket.receive({
    method: "Network.loadingFinished",
    params: { requestId: "x" },
  });
  assert.equal(events[0].method, "Network.loadingFinished");
  unsubscribe();
  socket.receive({ method: "Network.requestWillBeSent", params: {} });
  assert.equal(events.length, 1);
  client.close();
});

test("rejects a pending request when the socket closes", async () => {
  const { client, socket } = createClient();
  socket.open();

  const pending = client.send("Network.enable");
  await new Promise((resolve) => setImmediate(resolve));
  socket.close();

  await assert.rejects(pending, /CDP connection closed/);
});

test("rejects readiness and send when the socket closes before opening", async () => {
  const { client, socket } = createClient();

  const pending = client.send("Network.enable");
  socket.close();

  await assert.rejects(client.ready, /CDP connection closed/);
  await assert.rejects(pending, /CDP connection closed/);
  assert.deepEqual(socket.sent, []);
});

test("rejects CDP error responses with their message", async () => {
  const { client, socket } = createClient();
  socket.open();

  const pending = client.send("Network.enable");
  await new Promise((resolve) => setImmediate(resolve));
  socket.receive({ id: 1, error: { code: -32601, message: "Unknown method" } });

  await assert.rejects(pending, /Unknown method/);
  client.close();
});

test("exposes a promise that settles when the socket closes", async () => {
  const { client, socket } = createClient();
  socket.open();

  assert.equal(client.closed instanceof Promise, true);
  socket.close();
  await client.closed;
});
