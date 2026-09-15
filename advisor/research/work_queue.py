"""Shared scheduling index for business Requests and historical experiment Tests."""
from dataclasses import dataclass
from datetime import datetime, timedelta
import json

from .experiments.records import Lease
from .experiments.registration import record_identity
from .experiments.repository import Conflict
from .experiments.records import Fenced
from .experiments.resolution import encode


@dataclass(frozen=True)
class ResearchWork:
    work_id: str
    kind: str
    state: str
    source_status: str
    claimed_by: str | None
    service_owner_id: str | None
    input_record_id: str | None
    cancel_requested: bool


def read_work(connection, work_id):
    row = connection.execute("SELECT work_id, kind, state, source_status, claimed_by, service_owner_id, input_record_id, cancel_requested "
                             "FROM research_work_queue WHERE work_id=?", (work_id,)).fetchone()
    if row is None:
        raise LookupError("shared Research work does not exist")
    return ResearchWork(*row[:-1], bool(row[-1]))


def next_work(connection):
    row = connection.execute("SELECT work_id FROM research_work_queue WHERE state='running' OR (state='queued' AND (kind='experiment' OR cancel_requested=0)) "
        "ORDER BY CASE state WHEN 'running' THEN 0 ELSE 1 END, priority, julianday(enqueued_at), work_id LIMIT 1").fetchone()
    return read_work(connection, row[0]) if row else None


def require_service(connection, owner_id, now):
    row = connection.execute("SELECT owner_id, expires_at FROM research_service_leases WHERE lease_name='research-service'").fetchone()
    if row is None or row[0] != owner_id or datetime.fromisoformat(row[1]) <= now:
        raise Fenced("shared Research Service lease expired or changed")


def recover_experiment_work(connection, *, service_owner_id, claimed_by, now):
    require_service(connection, service_owner_id, now)
    # Source Test state and all domain checkpoints remain intact. Revoking the
    # scheduling claim immediately fences its old Test lease, even before reuse.
    connection.execute("UPDATE research_work_queue SET state='queued', claimed_by=NULL, service_owner_id=NULL "
        "WHERE kind='experiment' AND state='running' AND (claimed_by IS NULL OR claimed_by<>? OR service_owner_id IS NULL OR service_owner_id<>?)",
        (claimed_by, service_owner_id))


class ExperimentWorkQueue:
    def __init__(self, records):
        self.records, self.db = records, records.db

    def enqueue_fixture(self, test_id, descriptor):
        """Internal fixture admission; formal capability preflight is not replaced."""
        test = self.records._test(test_id)
        if test["value"].get("registration_version") != 1 or descriptor.get("evidence_kind") != "fixture":
            raise Conflict("shared experiment work requires a typed Test and explicit fixture input")
        artifact = self.records.artifacts.put_json(json.loads(encode(descriptor)))
        record_id = record_identity(test["experiment_id"], "service-input", test_id)
        prepared = self.records.prepare(experiment_id=test["experiment_id"], kind="service_input", record_id=record_id,
            submission_identity="service-input:" + test_id,
            value={"test_id": test_id, "descriptor_hash": artifact.content_hash, "queue_version": 1, "formal_ready": False},
            links=(("test", test_id),), artifact_hashes=(artifact.content_hash,))
        with self.records._transaction():
            existing = self.db.execute("SELECT input_record_id FROM research_work_queue WHERE work_id=?", (test_id,)).fetchone()
            if existing:
                self.records._insert(prepared)  # Full immutable-input identity check.
                if existing[0] != record_id:
                    raise Conflict("shared work input binding changed")
            else:
                if self.records.status(test_id) != "queued" or self.db.execute("SELECT 1 FROM lagent_worker_leases WHERE test_id=?", (test_id,)).fetchone():
                    raise Conflict("admit experiment work before its first worker claim")
                self.records._insert(prepared)
                self.db.execute("INSERT INTO research_work_queue(work_id,kind,state,source_status,priority,enqueued_at,input_record_id) "
                    "VALUES (?,'experiment','queued','queued',0,?,?)", (test_id, self.records._now().isoformat(), record_id))
        return read_work(self.db, test_id)

    def descriptor(self, work):
        record = self.records.read(work.input_record_id)
        if (work.kind != "experiment" or record["kind"] != "service_input" or record["value"]["test_id"] != work.work_id
                or self.records.related(record["record_id"]) != [{"relation": "test", "target_id": work.work_id}]):
            raise Conflict("shared work input is bound to another Test")
        return self.records.artifacts.read_json(record["value"]["descriptor_hash"])

    def claim(self, work_id, *, service_owner_id, claimed_by, lease_seconds):
        work = read_work(self.db, work_id)
        if work.kind != "experiment":
            raise Conflict("expected experiment work")
        self.descriptor(work)
        require_service(self.db, service_owner_id, self.records._now())
        row = self.db.execute("SELECT worker_id,generation,expires_at FROM lagent_worker_leases WHERE test_id=?", (work_id,)).fetchone()
        if (work.state == "running" and work.claimed_by == claimed_by and work.service_owner_id == service_owner_id
                and row and row[0] == claimed_by and datetime.fromisoformat(row[2]) > self.records._now()):
            lease = Lease(work_id, claimed_by, row[1])
            self.records._assert_lease(lease)
            return lease
        def guard():
            require_service(self.db, service_owner_id, self.records._now())
            head = next_work(self.db)
            if head is None or head.work_id != work_id or head != work or work.state not in {"queued", "running"}:
                raise Fenced("shared queue head or ownership changed")
            if work.state == "running" and (work.claimed_by != claimed_by or work.service_owner_id != service_owner_id):
                raise Fenced("recover the abandoned service claim before taking its Test")
            self.db.execute("UPDATE research_work_queue SET state='running',claimed_by=?,service_owner_id=? WHERE work_id=?",
                            (claimed_by, service_owner_id, work_id))
        return self.records.claim(work_id, worker_id=claimed_by, lease_seconds=lease_seconds, guard=guard, supersede=True)

    def cancel(self, work_id, *, guard=None):
        with self.records._transaction():
            # Read the current queue state under the writer lock, including on a
            # retry racing with a service completion or evaluation park.
            work = read_work(self.db, work_id)
            if work.kind != "experiment":
                raise Conflict("business cancellation uses its Request API")
            if guard is not None:
                guard()
            if work.state == "finished" or work.cancel_requested:
                return work
            test = self.records._test(work_id)
            prepared = self.records.prepare(experiment_id=test["experiment_id"], kind="service_control",
                record_id=record_identity(test["experiment_id"], "service-cancel", work_id),
                submission_identity="service-cancel:" + work_id, value={"test_id": work_id, "operation": "cancel"}, links=(("test", work_id),))
            self.records._insert(prepared)
            self.db.execute("UPDATE research_work_queue SET cancel_requested=1, state=CASE WHEN state='waiting' THEN 'queued' ELSE state END WHERE work_id=?", (work_id,))
        return read_work(self.db, work_id)

    def park_evaluation(self, lease):
        if self.records.status(lease.test_id) != "evaluating":
            raise Conflict("only an evaluating Test can wait for its evaluator")
        episode = self.records.projection(lease.test_id, "episode")
        clock = self.records.projection(lease.test_id, "phase_clock")
        if not episode or episode["value"]["status"] != "ready_for_evaluation" or not clock or clock["value"]["status"] != "finished":
            raise Conflict("evaluation wait requires a finished Episode replay")
        for name in ("candidate_processes", "model_invocations"):
            projection = self.records.projection(lease.test_id, name)
            if projection and any(not c["quiescent"] or c["status"] == "prepared" for c in projection["value"]["calls"].values()):
                raise Conflict("evaluation wait requires physical cleanup")
        def guard():
            self.db.execute("UPDATE research_work_queue SET state='waiting' WHERE work_id=?", (lease.test_id,))
        self.records.commit(lease, phase_id="service", action_id="evaluation-wait:" + str(lease.generation), attempt=0,
            kind="service_evaluation_wait", payload={"reason": "evaluator_required"}, simulated_at=None,
            updates=(), guard=guard)

    @staticmethod
    def heartbeat(connection, lease, *, service_owner_id, now, lease_seconds):
        """Called inside the Service peer transaction, after singleton renewal."""
        require_service(connection, service_owner_id, now)
        work = read_work(connection, lease.test_id)
        if work.state != "running" or work.claimed_by != lease.worker_id or work.service_owner_id != service_owner_id:
            raise Fenced("shared work heartbeat ownership changed")
        cursor = connection.execute("UPDATE lagent_worker_leases SET expires_at=? WHERE test_id=? AND worker_id=? AND generation=? AND julianday(expires_at)>julianday(?)",
            ((now + timedelta(seconds=lease_seconds)).isoformat(), lease.test_id, lease.worker_id, lease.generation, now.isoformat()))
        if cursor.rowcount != 1:
            raise Fenced("experiment heartbeat lost its lease")
