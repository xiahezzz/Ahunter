"""Append-only experiment facts and atomic event/projection/outbox commits.

This is storage inside the existing Research control plane, not another queue or
scheduler. Only host modules use this interface. Model invocations and other
external effects must never execute while its SQLite transaction is held.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import base64
import json
from typing import Callable

from .repository import Conflict, Missing
from .resolution import digest, encode

KINDS = frozenset({"definition", "candidate_package", "candidate_proposal", "test_plan", "test",
                   "phase", "attempt", "invocation", "cost", "ledger", "evaluation", "comparison",
                   "selection", "exposure", "correction", "preflight", "source_document", "source_expiration", "data_bundle", "bundle_import", "query", "search_request", "search_response", "snapshot", "valuation", "replay_program", "service_input", "service_control"})
TERMINAL = frozenset({"completed", "blocked", "failed", "cancelled"})
TRANSITIONS = {"created": {"preflight", "cancelled"}, "preflight": {"queued", "blocked", "failed", "cancelled"},
               "queued": {"running", "cancelled", "blocked", "failed"},
               "running": {"evaluating", "blocked", "failed", "cancelled"},
               "evaluating": {"completed", "blocked", "failed", "cancelled"}}


class Fenced(Conflict):
    pass


@dataclass(frozen=True)
class Lease:
    test_id: str
    worker_id: str
    generation: int


@dataclass(frozen=True)
class ProjectionUpdate:
    name: str
    expected_sequence: int | None
    value: dict


@dataclass(frozen=True)
class OutboxMessage:
    outbox_id: str
    destination: str
    payload: dict


class ExperimentRecords:
    def __init__(self, connection, *, artifacts=None, clock: Callable[[], datetime] | None = None,
                 fault: Callable[[str], None] | None = None):
        self.db = connection
        self.artifacts = artifacts
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.fault = fault or (lambda point: None)
        self.db.execute("PRAGMA foreign_keys = ON")

    def _now(self):
        current = self.clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("storage clock must be timezone-aware")
        return current.astimezone(timezone.utc)

    @contextmanager
    def _transaction(self):
        if self.db.in_transaction:
            raise Conflict("experiment writes require an independent transaction boundary")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.fault("before_commit")
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        self.fault("after_commit")

    @staticmethod
    def _checked(value_json, value_hash):
        value = json.loads(value_json)
        if digest(value) != value_hash:
            raise Conflict("experiment record integrity check failed")
        return value

    def read(self, record_id):
        row = self.db.execute("SELECT record_sequence, experiment_id, kind, value_json, value_hash, created_at "
                              "FROM lagent_records WHERE record_id=?", (record_id,)).fetchone()
        if row is None:
            raise Missing("experiment record not found")
        return {"record_id": record_id, "sequence": row[0], "experiment_id": row[1], "kind": row[2],
                "value": self._checked(row[3], row[4]), "content_hash": row[4], "created_at": row[5]}

    def put(self, **request):
        return self.put_many((request,))[0]

    def prepare(self, *, experiment_id, kind, record_id, submission_identity, value,
                links=(), artifact_hashes=(), source_policy_hashes=()):
        """Detach inputs and verify sealed bytes before a database transaction."""
        if kind not in KINDS or not isinstance(record_id, str) or not record_id or not isinstance(submission_identity, str) or not submission_identity:
            raise ValueError("record kind and identities are required")
        links = tuple(sorted(set(links)))
        artifact_hashes = tuple(sorted(set(artifact_hashes)))
        source_policy_hashes = tuple(sorted(set(source_policy_hashes)))
        if set(artifact_hashes) & set(source_policy_hashes):
            raise ValueError("artifact cannot have two retention policies in one record")
        request = {"record_id": record_id, "value": value, "links": links, "artifact_hashes": artifact_hashes}
        # Preserve the fingerprint of records written before source-policy support.
        if source_policy_hashes:
            request["source_policy_hashes"] = source_policy_hashes
        request = json.loads(encode(request))
        refs = []
        for content_hash in (*artifact_hashes, *source_policy_hashes):
            if self.artifacts is None:
                raise ValueError("artifact store required")
            data = self.artifacts.read_bytes(content_hash)
            refs.append((content_hash, len(data), "source_policy" if content_hash in source_policy_hashes else "permanent"))
        return {"experiment_id": experiment_id, "kind": kind, "submission_identity": submission_identity,
                "request": request, "refs": refs}

    def put_many(self, requests):
        prepared = [self.prepare(**request) for request in requests]
        with self._transaction():
            return [self._insert(item) for item in prepared]

    def _insert(self, prepared):
        """Host registration composes these writes inside one owned transaction."""
        if not self.db.in_transaction:
            raise Conflict("record insertion requires a transaction")
        experiment_id, kind = prepared["experiment_id"], prepared["kind"]
        submission_identity = prepared["submission_identity"]
        request, refs = prepared["request"], prepared["refs"]
        record_id, value, links = request["record_id"], request["value"], request["links"]
        fingerprint = digest(request)
        previous = self.db.execute("SELECT record_id, input_hash FROM lagent_records "
                                   "WHERE experiment_id=? AND kind=? AND submission_identity=?",
                                   (experiment_id, kind, submission_identity)).fetchone()
        if previous is not None:
            if previous[1] != fingerprint:
                raise Conflict("record submission identity has different content")
            return self.read(previous[0])
        if self.db.execute("SELECT 1 FROM lagent_records WHERE record_id=?", (record_id,)).fetchone() is not None:
            raise Conflict("record identity is already registered under another submission")
        for relation, target_id in links:
            if not relation:
                raise ValueError("record relation is required")
            target = self.read(target_id)
            if target["experiment_id"] != experiment_id:
                raise Conflict("record link crosses experiment boundary")
        self.db.execute("INSERT INTO lagent_records(record_id, experiment_id, kind, submission_identity, "
                        "input_hash, value_json, value_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (record_id, experiment_id, kind, submission_identity, fingerprint,
                         encode(value), digest(value), self._now().isoformat()))
        self.fault("after_record")
        for relation, target_id in links:
            self.db.execute("INSERT INTO lagent_record_links VALUES (?, ?, ?)", (record_id, relation, target_id))
        for content_hash, size, retention in refs:
            # A source expiration may have run between prepare() and BEGIN.
            # Recheck under the same write lock used by reference changes/expiry.
            if len(self.artifacts.read_bytes(content_hash)) != size:
                raise Conflict("artifact changed before reference commit")
            existing = self.db.execute("SELECT byte_size FROM research_artifacts WHERE content_hash=?", (content_hash,)).fetchone()
            if existing is not None and existing[0] != size:
                raise Conflict("shared artifact size differs from sealed bytes")
            self.db.execute("INSERT INTO research_artifacts(content_hash, media_type, byte_size, relative_path, "
                            "retention_class, availability, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT(content_hash) DO UPDATE SET availability='available', "
                            "retention_class=CASE WHEN excluded.retention_class='experiment_permanent' THEN excluded.retention_class "
                            "ELSE research_artifacts.retention_class END",
                            (content_hash, "application/octet-stream", size, f"{content_hash[:2]}/{content_hash[2:]}",
                             "experiment_permanent" if retention == "permanent" else "source_policy", "available", self._now().isoformat()))
            self.db.execute("INSERT INTO lagent_artifact_links VALUES (?, ?, ?)", (record_id, content_hash, retention))
        self.fault("after_artifact_references")
        return self.read(record_id)

    def related(self, record_id):
        self.read(record_id)
        return [{"relation": row[0], "target_id": row[1]} for row in self.db.execute(
            "SELECT relation, target_id FROM lagent_record_links WHERE record_id=? ORDER BY relation, target_id", (record_id,))]

    def page(self, *, experiment_id, kind=None, limit, cursor=None):
        if type(limit) is not int or limit <= 0 or (kind is not None and kind not in KINDS):
            raise ValueError("invalid page limit or record kind")
        if cursor is None:
            ceiling = self.db.execute("SELECT COALESCE(MAX(record_sequence), 0) FROM lagent_records").fetchone()[0]
            after = 0
        else:
            try:
                decoded = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
                if set(decoded) != {"experiment_id", "kind", "after", "ceiling"}:
                    raise ValueError
                if decoded["experiment_id"] != experiment_id or decoded["kind"] != kind:
                    raise ValueError
                after, ceiling = decoded["after"], decoded["ceiling"]
                if type(after) is not int or type(ceiling) is not int or not 0 <= after <= ceiling:
                    raise ValueError
            except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                raise ValueError("invalid or mismatched record cursor") from exc
        rows = self.db.execute("SELECT record_id, record_sequence FROM lagent_records WHERE experiment_id=? "
                               "AND (? IS NULL OR kind=?) AND record_sequence>? AND record_sequence<=? "
                               "ORDER BY record_sequence LIMIT ?", (experiment_id, kind, kind, after, ceiling, limit + 1)).fetchall()
        next_cursor = None
        if len(rows) > limit:
            token = {"experiment_id": experiment_id, "kind": kind, "after": rows[limit - 1][1], "ceiling": ceiling}
            next_cursor = base64.urlsafe_b64encode(encode(token).encode()).decode("ascii")
        return {"items": [self.read(row[0]) for row in rows[:limit]], "next_cursor": next_cursor}

    def _test(self, test_id):
        record = self.read(test_id)
        if record["kind"] != "test":
            raise Conflict("expected a Test Record")
        return record

    def projection(self, test_id, name):
        row = self.db.execute("SELECT event_sequence, value_json, value_hash FROM lagent_projections "
                              "WHERE test_id=? AND name=?", (test_id, name)).fetchone()
        latest = self.db.execute(
            "SELECT MAX(e.event_sequence) FROM lagent_events e, json_each(e.value_json, '$.updates') u "
            "WHERE e.test_id=? AND json_extract(u.value, '$.name')=?", (test_id, name)).fetchone()[0]
        if row is None:
            if latest is not None:
                raise Conflict("projection missing; rebuild from durable events")
            return None
        if row[0] != latest:
            raise Conflict("projection is stale; rebuild from durable events")
        value = self._checked(row[1], row[2])
        event = self.event(row[0])
        if event["test_id"] != test_id or not any(u["name"] == name and u["value"] == value for u in event["value"]["updates"]):
            raise Conflict("projection is not backed by its event")
        return {"sequence": row[0], "value": value}

    def status(self, test_id):
        self._test(test_id)
        state = self.projection(test_id, "status")
        return state["value"]["status"] if state else "created"

    def claim(self, test_id, *, worker_id, lease_seconds, guard=None, supersede=False):
        if not worker_id or type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("positive lease duration and worker identity required")
        with self._transaction():
            if supersede and guard is None:
                raise ValueError("superseding a lease requires a shared service ownership guard")
            if guard is not None:
                guard()
            if supersede:
                work = self.db.execute("SELECT state,claimed_by,service_owner_id FROM research_work_queue WHERE work_id=? AND kind='experiment'", (test_id,)).fetchone()
                service = self.db.execute("SELECT owner_id,expires_at FROM research_service_leases WHERE lease_name='research-service'").fetchone()
                if (work is None or work[0] != "running" or work[1] != worker_id or service is None
                        or service[0] != work[2] or datetime.fromisoformat(service[1]) <= self._now()):
                    raise Fenced("lease supersession lacks shared service ownership")
            if self.status(test_id) not in {"queued", "running", "evaluating"}:
                raise Fenced("only queued or active tests can be claimed")
            row = self.db.execute("SELECT worker_id, generation, expires_at FROM lagent_worker_leases WHERE test_id=?", (test_id,)).fetchone()
            current = self._now()
            if row is not None and datetime.fromisoformat(row[2]) > current and not supersede:
                raise Fenced("test already has a live worker lease")
            generation = row[1] + 1 if row else 1
            self.db.execute("INSERT INTO lagent_worker_leases VALUES (?, ?, ?, ?) ON CONFLICT(test_id) "
                            "DO UPDATE SET worker_id=excluded.worker_id, generation=excluded.generation, expires_at=excluded.expires_at",
                            (test_id, worker_id, generation, (current + timedelta(seconds=lease_seconds)).isoformat()))
            attempt_id = f"{test_id}:worker-attempt:{generation}"
            value = {"test_id": test_id, "worker_id": worker_id, "generation": generation}
            experiment_id = self._test(test_id)["experiment_id"]
            self.db.execute("INSERT INTO lagent_records(record_id, experiment_id, kind, submission_identity, "
                            "input_hash, value_json, value_hash, created_at) VALUES (?, ?, 'attempt', ?, ?, ?, ?, ?)",
                            (attempt_id, experiment_id, attempt_id, digest(value), encode(value), digest(value), current.isoformat()))
            self.db.execute("INSERT INTO lagent_record_links VALUES (?, 'test', ?)", (attempt_id, test_id))
            return Lease(test_id, worker_id, generation)

    def _assert_lease(self, lease):
        row = self.db.execute("SELECT worker_id, generation, expires_at FROM lagent_worker_leases WHERE test_id=?", (lease.test_id,)).fetchone()
        if (row is None or row[0] != lease.worker_id or row[1] != lease.generation
                or datetime.fromisoformat(row[2]) <= self._now() or self.status(lease.test_id) in TERMINAL):
            raise Fenced("worker lease is expired, superseded or terminal")
        work = self.db.execute("SELECT state, claimed_by, service_owner_id FROM research_work_queue WHERE work_id=? AND kind='experiment'", (lease.test_id,)).fetchone()
        if work is None and self.db.execute("SELECT 1 FROM lagent_records r JOIN lagent_record_links l ON l.record_id=r.record_id "
            "WHERE r.kind='service_input' AND l.relation='test' AND l.target_id=?", (lease.test_id,)).fetchone():
            raise Fenced("shared Research queue binding is missing")
        if work is not None:
            service = self.db.execute("SELECT owner_id, expires_at FROM research_service_leases WHERE lease_name='research-service'").fetchone()
            if (work[0] != "running" or work[1] != lease.worker_id or service is None or service[0] != work[2]
                    or datetime.fromisoformat(service[1]) <= self._now()):
                raise Fenced("shared Research Service no longer owns this Test")

    def renew(self, lease, *, lease_seconds):
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("positive lease duration required")
        with self._transaction():
            self._assert_lease(lease)
            self.db.execute("UPDATE lagent_worker_leases SET expires_at=? WHERE test_id=? AND generation=?",
                            ((self._now() + timedelta(seconds=lease_seconds)).isoformat(), lease.test_id, lease.generation))

    def event(self, sequence):
        row = self.db.execute("SELECT experiment_id, test_id, phase_id, action_id, attempt, generation, kind, "
                              "value_json, value_hash, actual_at, simulated_at FROM lagent_events WHERE event_sequence=?", (sequence,)).fetchone()
        if row is None:
            raise Missing("experiment event not found")
        return {"sequence": sequence, "experiment_id": row[0], "test_id": row[1], "phase_id": row[2],
                "action_id": row[3], "attempt": row[4], "generation": row[5], "kind": row[6],
                "value": self._checked(row[7], row[8]), "actual_at": row[9], "simulated_at": row[10]}

    def commit(self, lease, *, phase_id, action_id, attempt, kind, payload, simulated_at,
               updates=(), outbox=(), guard=None):
        if any(u.name == "status" for u in updates):
            raise ValueError("lifecycle status changes must use transition")
        return self._commit(lease.test_id, lease=lease, phase_id=phase_id, action_id=action_id, attempt=attempt,
                            kind=kind, payload=payload, simulated_at=simulated_at, updates=updates, outbox=outbox, guard=guard)

    def _commit(self, test_id, *, lease, phase_id, action_id, attempt, kind, payload, simulated_at,
                updates=(), outbox=(), transition_to=None, guard=None):
        if not action_id or not kind or type(attempt) is not int or attempt < 0:
            raise ValueError("event identity, kind and nonnegative attempt required")
        if simulated_at is not None:
            if simulated_at.tzinfo is None or simulated_at.utcoffset() is None:
                raise ValueError("simulated_at must be timezone-aware")
            simulated_at = simulated_at.isoformat()
        if len({u.name for u in updates}) != len(updates) or any(not u.name for u in updates):
            raise ValueError("projection names must be nonempty and unique")
        value = {"payload": payload, "updates": [vars(u) for u in updates], "outbox": [vars(m) for m in outbox]}
        value = json.loads(encode(value))
        fingerprint = digest({"kind": kind, "value": value, "simulated_at": simulated_at, "transition_to": transition_to})
        with self._transaction():
            record = self._test(test_id)
            # A durable retry can return the original result even after lease expiry;
            # it can never create another write under a stale generation.
            prior = self.db.execute("SELECT event_sequence, input_hash FROM lagent_events WHERE "
                                    "test_id=? AND phase_id=? AND action_id=? AND attempt=?",
                                    (test_id, phase_id, action_id, attempt)).fetchone()
            if prior is not None:
                if prior[1] != fingerprint:
                    raise Conflict("event identity has different content")
                return self.event(prior[0])
            if lease is not None:
                if lease.test_id != test_id:
                    raise Fenced("lease belongs to another test")
                self._assert_lease(lease)
            elif self.status(test_id) not in {"created", "preflight", "queued"}:
                raise Fenced("active test writes require its worker lease")
            if transition_to is not None:
                current = self.status(test_id)
                if transition_to not in TRANSITIONS.get(current, set()):
                    raise Conflict("invalid or terminal test status transition")
                if transition_to == "running" and lease is None:
                    raise Fenced("starting a test requires worker ownership")
                projection = self.projection(test_id, "status")
                value["updates"] = [{"name": "status", "expected_sequence": projection["sequence"] if projection else None,
                                     "value": {"status": transition_to, **payload}}]
            for update in value["updates"]:
                current = self.projection(test_id, update["name"])
                if (current["sequence"] if current else None) != update["expected_sequence"]:
                    raise Conflict("projection revision changed")
            self.fault("before_event")
            if guard is not None:
                guard()
            cursor = self.db.execute("INSERT INTO lagent_events(experiment_id, test_id, phase_id, action_id, attempt, generation, "
                                     "kind, input_hash, value_json, value_hash, actual_at, simulated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                     (record["experiment_id"], test_id, phase_id, action_id, attempt, lease.generation if lease else 0,
                                      kind, fingerprint, encode(value), digest(value), self._now().isoformat(), simulated_at))
            sequence = cursor.lastrowid
            self.fault("after_event")
            for update in value["updates"]:
                self.db.execute("INSERT INTO lagent_projections VALUES (?, ?, ?, ?, ?) ON CONFLICT(test_id, name) "
                                "DO UPDATE SET event_sequence=excluded.event_sequence, value_json=excluded.value_json, value_hash=excluded.value_hash",
                                (test_id, update["name"], sequence, encode(update["value"]), digest(update["value"])))
            self.fault("after_projection")
            for message in value["outbox"]:
                if not message["outbox_id"] or not message["destination"]:
                    raise ValueError("outbox identity and destination required")
                self.db.execute("INSERT INTO lagent_outbox VALUES (?, ?, ?, ?, ?)",
                                (message["outbox_id"], sequence, message["destination"], encode(message["payload"]), digest(message["payload"])))
            self.fault("after_outbox")
            return self.event(sequence)

    def transition(self, test_id, target, *, action_id, lease=None, reason=None):
        return self._commit(test_id, lease=lease, phase_id="", action_id=action_id, attempt=0,
                            kind="status_transition", payload={"reason": reason}, simulated_at=None, transition_to=target)

    def events(self, test_id, *, after, limit):
        if type(after) is not int or after < 0 or type(limit) is not int or limit <= 0:
            raise ValueError("invalid event pagination")
        self._test(test_id)
        rows = self.db.execute("SELECT event_sequence FROM lagent_events WHERE test_id=? AND event_sequence>? "
                              "ORDER BY event_sequence LIMIT ?", (test_id, after, limit + 1)).fetchall()
        return {"items": [self.event(row[0]) for row in rows[:limit]],
                "next_after": rows[limit - 1][0] if len(rows) > limit else None}

    def replay(self, test_id):
        self._test(test_id)
        result = {}
        for row in self.db.execute("SELECT event_sequence FROM lagent_events WHERE test_id=? ORDER BY event_sequence", (test_id,)).fetchall():
            event = self.event(row[0])
            for update in event["value"]["updates"]:
                current = result.get(update["name"])
                if (current["sequence"] if current else None) != update["expected_sequence"]:
                    raise Conflict("event replay projection revision mismatch")
                result[update["name"]] = {"sequence": event["sequence"], "value": update["value"]}
        return result

    def rebuild(self, lease):
        with self._transaction():
            # Lease check without the status projection: that projection may be lost.
            row = self.db.execute("SELECT worker_id, generation, expires_at FROM lagent_worker_leases WHERE test_id=?", (lease.test_id,)).fetchone()
            if row is None or row[0] != lease.worker_id or row[1] != lease.generation or datetime.fromisoformat(row[2]) <= self._now():
                raise Fenced("rebuild requires the current worker lease")
            projected = self.replay(lease.test_id)
            self.db.execute("DELETE FROM lagent_projections WHERE test_id=?", (lease.test_id,))
            for name, item in projected.items():
                self.db.execute("INSERT INTO lagent_projections VALUES (?, ?, ?, ?, ?)",
                                (lease.test_id, name, item["sequence"], encode(item["value"]), digest(item["value"])))
            return projected

    def pending_outbox(self, *, limit):
        if type(limit) is not int or limit <= 0:
            raise ValueError("positive outbox limit required")
        rows = self.db.execute("SELECT o.outbox_id, o.event_sequence, o.destination, o.payload_json, o.payload_hash "
                              "FROM lagent_outbox o LEFT JOIN lagent_outbox_deliveries d USING(outbox_id) "
                              "WHERE d.outbox_id IS NULL ORDER BY o.event_sequence, o.outbox_id LIMIT ?", (limit,)).fetchall()
        return [{"outbox_id": row[0], "event_sequence": row[1], "destination": row[2],
                 "payload": self._checked(row[3], row[4])} for row in rows]

    def acknowledge_outbox(self, outbox_id, receipt):
        # Delivery is at-least-once. Receivers must deduplicate by outbox_id; model
        # calls with unknown provider outcomes must be reconciled, never blind-retried.
        with self._transaction():
            row = self.db.execute("SELECT receipt_json, receipt_hash FROM lagent_outbox_deliveries WHERE outbox_id=?", (outbox_id,)).fetchone()
            if row is not None:
                previous = self._checked(row[0], row[1])
                if digest(previous) != digest(receipt):
                    raise Conflict("outbox acknowledgement has different content")
                return previous
            self.db.execute("INSERT INTO lagent_outbox_deliveries VALUES (?, ?, ?, ?)",
                            (outbox_id, encode(receipt), digest(receipt), self._now().isoformat()))
            return receipt
