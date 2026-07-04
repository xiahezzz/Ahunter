import { randomUUID } from "node:crypto";
import { classifyFrame } from "./classify-frame.mjs";

export class Collector {
  constructor({ allowedRids, store, now = () => Date.now(), onAccepted = () => {} }) {
    this.allowedRids = allowedRids;
    this.store = store;
    this.now = now;
    this.onAccepted = onAccepted;
    this.runId = randomUUID();
  }

  async acceptFrame({ payloadData, receivedAt = this.now() }, { signal } = {}) {
    if (!payloadData.startsWith('42/msg,["room_msg",')) {
      this.store.incrementCounter("ignored", receivedAt);
      return "ignored";
    }

    const result = classifyFrame({
      frame: payloadData,
      receivedAt,
      allowedRids: typeof this.allowedRids === "function" ? this.allowedRids() : this.allowedRids,
    });
    if (result.status !== "accepted") {
      this.store.incrementCounter(result.status, receivedAt);
      if (result.status === "failed") {
        this.store.recordDecodeFailure?.(result.payloadHash, result.errorClass, receivedAt);
      }
      return result.status;
    }

    const inserted = this.store.insertEvent(result.event, this.runId);
    const status = inserted ? "accepted" : "duplicate";
    this.store.incrementCounter(status, receivedAt);
    if (inserted || status === "duplicate") {
      await Promise.resolve()
        .then(() => this.onAccepted(result.event, { signal }))
        .catch(() => {
          this.store.incrementCounter("media_failed", this.now());
        });
    }
    return status;
  }
}
