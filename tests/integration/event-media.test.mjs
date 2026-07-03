import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { openEventStore } from "../../src/events/event-store.mjs";
import { Collector } from "../../src/ingestion/collector.mjs";
import { processEventMedia } from "../../src/media/process-event-media.mjs";
import { encodeRoomPayload } from "../helpers/encode-room-payload.mjs";

const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xd9]);

function frame(rid, msg) {
  const payload = encodeRoomPayload({ id: rid, rid, msg }, "2026-07-03");
  return `42/msg,["room_msg",${JSON.stringify(payload)}]`;
}

test("downloads media only for newly inserted allowlisted events", async (t) => {
  const receivedAt = Date.parse("2026-07-03T01:00:00Z");
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-event-media-"));
  const filename = path.join(directory, "events.sqlite");
  const mediaRoot = path.join(directory, "media");
  const store = openEventStore(filename);
  let storeOpen = true;
  t.after(() => {
    if (storeOpen) store.close();
  });
  let fetches = 0;
  const fetchImpl = async () => {
    fetches += 1;
    return new Response(jpeg, {
      status: 200,
      headers: {
        "content-type": "image/jpeg",
        "content-length": String(jpeg.byteLength),
      },
    });
  };
  const collector = new Collector({
    allowedRids: new Set([20025]),
    store,
    onAccepted: (acceptedEvent) =>
      processEventMedia({ event: acceptedEvent, store, mediaRoot, fetchImpl }),
  });
  const accepted = frame(20025, {
    type: "image",
    url: "https://images.example.com/accepted-marker.jpg",
  });

  assert.equal(await collector.acceptFrame({ payloadData: accepted, receivedAt }), "accepted");
  assert.equal(
    await collector.acceptFrame({
      payloadData: frame(23200, {
        type: "image",
        url: "https://rejected-marker.example/image.jpg",
      }),
      receivedAt,
    }),
    "rejected",
  );
  assert.equal(await collector.acceptFrame({ payloadData: accepted, receivedAt }), "duplicate");
  assert.equal(
    await collector.acceptFrame({
      payloadData: '42/msg,["room_msg","broken"]',
      receivedAt,
    }),
    "failed",
  );

  assert.equal(fetches, 1);
  assert.deepEqual(
    store.database.prepare("SELECT rid FROM media").all().map(({ rid }) => rid),
    [20025],
  );
  const row = store.database.prepare("SELECT source_url, local_path FROM media").get();
  assert.equal(row.source_url, "https://images.example.com/accepted-marker.jpg");
  assert.equal(row.local_path.startsWith(mediaRoot), true);
  assert.equal(
    store.database
      .prepare("PRAGMA index_list(media)")
      .all()
      .some(({ name }) => name === "media_rid_idx"),
    true,
  );

  store.close();
  storeOpen = false;
  const bytes = await readFile(filename);
  assert.equal(bytes.includes(Buffer.from("rejected-marker")), false);
});
