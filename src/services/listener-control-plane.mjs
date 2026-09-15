import fs from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

const READYNESS = new Set([
  "starting",
  "waiting_for_chrome",
  "waiting_for_authorization",
  "connecting",
  "listening",
  "stopping",
]);
const HEALTH = new Set(["healthy", "degraded", "failed"]);
const REASON_CODE = /^[a-z][a-z0-9_]{0,63}$/;
const MAX_INSTANCE_ID = 128;
const DEFAULT_TTL_MS = 30_000;

export class ListenerControlPlaneError extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

/**
 * Own the one Listener service lease and its independent liveness/readiness/
 * health snapshot.  Callers only need this module's claim, transition,
 * heartbeat, release and readSnapshot methods; they never construct control
 * plane SQL themselves.
 */
export class ListenerControlPlane {
  constructor(filename, { ttlMs = DEFAULT_TTL_MS, now = () => Date.now() } = {}) {
    if (!Number.isSafeInteger(ttlMs) || ttlMs < 1_000 || ttlMs > 300_000) {
      throw new ListenerControlPlaneError("invalid_ttl");
    }
    if (typeof now !== "function") throw new ListenerControlPlaneError("invalid_clock");
    this.filename = filename;
    this.ttlMs = ttlMs;
    this._now = now;
    this.database = openControlDatabase(filename);
  }

  claim(instanceId, { at = this._now() } = {}) {
    validateInstanceId(instanceId);
    validateTimestamp(at, this._now());
    return this.#transaction(() => {
      const lease = this.#lease();
      if (lease && lease.instance_id !== instanceId && lease.expires_at > at) {
        return Object.freeze({ claimed: false, code: "already_running", snapshot: this.#snapshot(at) });
      }
      const expiresAt = at + this.ttlMs;
      if (!lease) {
        this.database.prepare(`INSERT INTO listener_service_lease(
          singleton, instance_id, started_at, heartbeat_at, expires_at
        ) VALUES (1, ?, ?, ?, ?)`).run(instanceId, at, at, expiresAt);
      } else if (lease.instance_id === instanceId) {
        if (at < lease.heartbeat_at) throw new ListenerControlPlaneError("heartbeat_regressed");
        this.database.prepare(`UPDATE listener_service_lease
          SET heartbeat_at = ?, expires_at = ? WHERE singleton = 1 AND instance_id = ?`
        ).run(at, expiresAt, instanceId);
      } else {
        const changed = this.database.prepare(`UPDATE listener_service_lease
          SET instance_id = ?, started_at = ?, heartbeat_at = ?, expires_at = ?
          WHERE singleton = 1 AND expires_at <= ?`
        ).run(instanceId, at, at, expiresAt, at).changes;
        if (changed !== 1) return Object.freeze({ claimed: false, code: "already_running", snapshot: this.#snapshot(at) });
      }
      this.#ensureStatus(at);
      return Object.freeze({ claimed: true, code: "claimed", snapshot: this.#snapshot(at) });
    });
  }

  heartbeat(instanceId, { at = this._now() } = {}) {
    validateInstanceId(instanceId);
    validateTimestamp(at, this._now());
    return this.#transaction(() => {
      const lease = this.#heldLease(instanceId, at);
      if (at < lease.heartbeat_at) throw new ListenerControlPlaneError("heartbeat_regressed");
      const changes = this.database.prepare(`UPDATE listener_service_lease
        SET heartbeat_at = ?, expires_at = ?
        WHERE singleton = 1 AND instance_id = ? AND expires_at > ?`
      ).run(at, at + this.ttlMs, instanceId, at).changes;
      if (changes !== 1) throw new ListenerControlPlaneError("not_holder");
      return this.#snapshot(at);
    });
  }

  transition(instanceId, next, { at = this._now(), activity = {} } = {}) {
    validateInstanceId(instanceId);
    validateTimestamp(at, this._now());
    if (!next || typeof next !== "object" || !READYNESS.has(next.readiness) || !HEALTH.has(next.health)) {
      throw new ListenerControlPlaneError("invalid_state");
    }
    if (next.reasonCode !== undefined && (!REASON_CODE.test(next.reasonCode) || next.reasonCode.length > 64)) {
      throw new ListenerControlPlaneError("invalid_reason");
    }
    return this.#transaction(() => {
      this.#heldLease(instanceId, at);
      const previous = this.#status();
      if (previous && at < previous.updated_at) throw new ListenerControlPlaneError("state_regressed");
      const resolvedActivity = resolveActivity(previous, activity, at, next.readiness);
      if (!previous) {
        this.database.prepare(`INSERT INTO listener_service_status(
          singleton, readiness, health, reason_code, updated_at,
          connected_at, last_frame_at, last_accepted_event_at
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)`
        ).run(
          next.readiness, next.health, next.reasonCode ?? null, at,
          resolvedActivity.connectedAt, resolvedActivity.lastFrameAt, resolvedActivity.lastAcceptedEventAt,
        );
      } else {
        this.database.prepare(`UPDATE listener_service_status SET
          readiness = ?, health = ?, reason_code = ?, updated_at = ?,
          connected_at = ?, last_frame_at = ?, last_accepted_event_at = ?
          WHERE singleton = 1`
        ).run(
          next.readiness, next.health, next.reasonCode ?? null, at,
          resolvedActivity.connectedAt, resolvedActivity.lastFrameAt, resolvedActivity.lastAcceptedEventAt,
        );
      }
      return this.#snapshot(at);
    });
  }

  release(instanceId, { at = this._now() } = {}) {
    validateInstanceId(instanceId);
    validateTimestamp(at, this._now());
    return this.#transaction(() => {
      const lease = this.#lease();
      if (!lease || lease.instance_id !== instanceId) throw new ListenerControlPlaneError("not_holder");
      if (at < lease.heartbeat_at) throw new ListenerControlPlaneError("heartbeat_regressed");
      this.database.prepare("DELETE FROM listener_service_lease WHERE singleton = 1 AND instance_id = ?").run(instanceId);
      return this.#snapshot(at);
    });
  }

  readSnapshot({ at = this._now() } = {}) {
    validateTimestamp(at, this._now());
    return this.#snapshot(at);
  }

  close() {
    this.database.close();
  }

  #transaction(work) {
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const result = work();
      this.database.exec("COMMIT");
      return result;
    } catch (error) {
      this.database.exec("ROLLBACK");
      throw error;
    }
  }

  #lease() {
    return this.database.prepare(`SELECT instance_id, started_at, heartbeat_at, expires_at
      FROM listener_service_lease WHERE singleton = 1`).get();
  }

  #heldLease(instanceId, at) {
    const lease = this.#lease();
    if (!lease || lease.instance_id !== instanceId || lease.expires_at <= at) {
      throw new ListenerControlPlaneError("not_holder");
    }
    return lease;
  }

  #status() {
    return this.database.prepare(`SELECT readiness, health, reason_code, updated_at,
      connected_at, last_frame_at, last_accepted_event_at
      FROM listener_service_status WHERE singleton = 1`).get();
  }

  #ensureStatus(at) {
    if (this.#status()) return;
    this.database.prepare(`INSERT INTO listener_service_status(
      singleton, readiness, health, reason_code, updated_at,
      connected_at, last_frame_at, last_accepted_event_at
    ) VALUES (1, 'starting', 'healthy', 'starting', ?, NULL, NULL, NULL)`).run(at);
  }

  #snapshot(at) {
    const lease = this.#lease();
    const status = this.#status();
    const live = Boolean(lease && lease.expires_at > at && lease.heartbeat_at <= at);
    return Object.freeze({
      liveness: live ? "live" : "offline",
      readiness: status?.readiness ?? "stopping",
      health: status?.health ?? "failed",
      reasonCode: status?.reason_code ?? "not_started",
      lease: lease && live ? Object.freeze({
        instanceId: lease.instance_id,
        startedAt: lease.started_at,
        heartbeatAt: lease.heartbeat_at,
        expiresAt: lease.expires_at,
      }) : null,
      activity: Object.freeze({
        connectedAt: status?.connected_at ?? null,
        lastFrameAt: status?.last_frame_at ?? null,
        lastAcceptedEventAt: status?.last_accepted_event_at ?? null,
      }),
      updatedAt: status?.updated_at ?? null,
    });
  }
}

export function openListenerControlPlane(filename, options) {
  return new ListenerControlPlane(filename, options);
}

function openControlDatabase(filename) {
  if (filename !== ":memory:") {
    const stat = fs.lstatSync(filename, { throwIfNoEntry: false });
    if (stat?.isSymbolicLink() || (stat && !stat.isFile())) throw new ListenerControlPlaneError("database_unreadable");
    fs.mkdirSync(path.dirname(filename), { recursive: true });
  }
  const database = new DatabaseSync(filename);
  database.exec(fs.readFileSync(new URL("../events/schema.sql", import.meta.url), "utf8"));
  return database;
}

function validateInstanceId(value) {
  if (typeof value !== "string" || value.length < 1 || value.length > MAX_INSTANCE_ID || !/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(value)) {
    throw new ListenerControlPlaneError("invalid_instance");
  }
}

function validateTimestamp(value, now) {
  if (!Number.isSafeInteger(value) || value < 0 || value > now + 5_000) {
    throw new ListenerControlPlaneError("invalid_time");
  }
}

function resolveActivity(previous, activity, at, readiness) {
  if (!activity || typeof activity !== "object") throw new ListenerControlPlaneError("invalid_activity");
  const prior = {
    connectedAt: previous?.connected_at ?? null,
    lastFrameAt: previous?.last_frame_at ?? null,
    lastAcceptedEventAt: previous?.last_accepted_event_at ?? null,
  };
  const result = { ...prior };
  for (const [input, output] of [
    ["connectedAt", "connectedAt"],
    ["lastFrameAt", "lastFrameAt"],
    ["lastAcceptedEventAt", "lastAcceptedEventAt"],
  ]) {
    if (activity[input] === undefined) continue;
    const value = activity[input];
    if (!Number.isSafeInteger(value) || value < 0 || value > at || (prior[output] !== null && value < prior[output])) {
      throw new ListenerControlPlaneError("activity_regressed");
    }
    result[output] = value;
  }
  if (readiness === "listening" && result.connectedAt === null) result.connectedAt = at;
  return result;
}
