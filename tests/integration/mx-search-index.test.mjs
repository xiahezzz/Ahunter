import assert from "node:assert/strict";
import test from "node:test";

import { openEventStore } from "../../src/events/event-store.mjs";

test("MX search index contains only normalized decoded text and indexes new events", () => {
  const store = openEventStore(":memory:");
  try {
    store.insertEvent({
      eventId: "event-search-1",
      schemaVersion: 2,
      rid: 20025,
      sourceMessageId: "source-private-id",
      oid: "private-oid",
      receivedAt: 1_000,
      sourceCreatedAt: 900,
      rawPayloadHash: "a".repeat(64),
      rawPayload: "private raw payload must never be indexed",
      decodedText: "规范化关键词 alpha",
      parsedContent: { imageUrls: [] },
      contentHash: "b".repeat(64),
    }, "run-search");

    const matches = store.database.prepare(
      "SELECT event_id FROM mx_event_search WHERE mx_event_search MATCH ?",
    ).all("alpha").map((row) => row.event_id);
    assert.deepEqual(matches, ["event-search-1"]);
    const columns = store.database.prepare("PRAGMA table_info(mx_event_search)").all().map((row) => row.name);
    assert.deepEqual(columns, ["event_id", "decoded_text"]);
  } finally {
    store.close();
  }
});
