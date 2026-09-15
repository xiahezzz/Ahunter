import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { openEventStore } from "../../src/events/event-store.mjs";
import {
  ListenerControlPlaneError,
  openListenerControlPlane,
} from "../../src/services/listener-control-plane.mjs";

function clock(initial = 1_000_000) {
  let value = initial;
  return {
    now: () => value,
    set(next) { value = next; },
  };
}

async function control({ initial } = {}) {
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-listener-control-"));
  const file = path.join(directory, "events.sqlite");
  if (initial) {
    const events = openEventStore(file);
    events.database.exec(`
      INSERT INTO ingest_runs(run_id, started_at) VALUES ('legacy', 1);
      INSERT INTO events(event_id, schema_version, rid, received_at, raw_payload_hash,
        raw_payload_expires_at, decoded_text, parsed_content_json, content_hash, ingest_run_id)
      VALUES ('event-1', 2, 20025, 1, 'a', 2, 'safe', '{"imageUrls":[]}', 'b', 'legacy');
    `);
    events.close();
  }
  const fake = clock();
  return { file, fake, plane: openListenerControlPlane(file, { now: fake.now, ttlMs: 10_000 }) };
}

test("only one concurrent instance claims a lease and expiry permits one atomic takeover", async () => {
  const { file, fake, plane } = await control();
  const other = openListenerControlPlane(file, { now: fake.now, ttlMs: 10_000 });
  try {
    assert.equal(plane.claim("listener-a").claimed, true);
    assert.deepEqual(other.claim("listener-b"), {
      claimed: false,
      code: "already_running",
      snapshot: other.readSnapshot(),
    });
    fake.set(1_010_001);
    assert.equal(other.claim("listener-b").claimed, true);
    assert.throws(() => plane.heartbeat("listener-a"), new ListenerControlPlaneError("not_holder"));
  } finally {
    plane.close();
    other.close();
  }
});

test("holder transitions independent state and activity fields without treating silence as failure", async () => {
  const { plane, fake } = await control();
  try {
    plane.claim("listener-a");
    const listening = plane.transition("listener-a", {
      readiness: "listening",
      health: "healthy",
      reasonCode: "network_enabled",
    }, { activity: { lastFrameAt: 1_000_000 } });
    assert.equal(listening.liveness, "live");
    assert.equal(listening.readiness, "listening");
    assert.equal(listening.health, "healthy");
    assert.equal(listening.activity.connectedAt, 1_000_000);
    assert.equal(listening.activity.lastAcceptedEventAt, null);

    fake.set(1_001_000);
    const silent = plane.heartbeat("listener-a");
    assert.equal(silent.readiness, "listening");
    assert.equal(silent.health, "healthy");
    assert.equal(silent.activity.lastFrameAt, 1_000_000);
  } finally {
    plane.close();
  }
});

test("only a current holder can update or release and a normal release preserves activity", async () => {
  const { plane, fake } = await control();
  try {
    plane.claim("listener-a");
    assert.throws(() => plane.transition("listener-b", {
      readiness: "connecting", health: "healthy", reasonCode: "connecting",
    }), new ListenerControlPlaneError("not_holder"));
    plane.transition("listener-a", {
      readiness: "listening", health: "healthy", reasonCode: "network_enabled",
    }, { activity: { lastAcceptedEventAt: 1_000_000 } });
    fake.set(1_001_000);
    const released = plane.release("listener-a");
    assert.equal(released.liveness, "offline");
    assert.equal(released.activity.lastAcceptedEventAt, 1_000_000);
    assert.throws(() => plane.release("listener-a"), new ListenerControlPlaneError("not_holder"));
  } finally {
    plane.close();
  }
});

test("control-plane migration preserves legacy event tables and rejects unsafe updates", async () => {
  const { plane } = await control({ initial: true });
  try {
    assert.equal(plane.database.prepare("SELECT count(*) AS count FROM events").get().count, 1);
    assert.equal(plane.database.prepare("SELECT count(*) AS count FROM ingest_runs").get().count, 1);
    assert.throws(
      () => plane.claim("listener-a", { at: Date.now() + 60_000 }),
      new ListenerControlPlaneError("invalid_time"),
    );
    assert.throws(
      () => plane.claim("invalid instance id"),
      new ListenerControlPlaneError("invalid_instance"),
    );
  } finally {
    plane.close();
  }
});
