"""Owner cancellation requests; running cleanup remains owned by ResearchService."""
from ..work_queue import ExperimentWorkQueue, read_work
from .records import TERMINAL
from .registration import record_identity
from .repository import Conflict, Missing


class ExperimentControl:
    def __init__(self, records):
        self.records = records

    def cancel(self, experiment_id, test_id, *, submission_identity, reason):
        records = self.records
        test = records._test(test_id)
        if test["experiment_id"] != experiment_id or test["value"].get("registration_version") != 1:
            raise Missing("experiment has no typed Test with this identity")
        if any(not isinstance(value, str) or not value.strip() for value in (submission_identity, reason)):
            raise ValueError("cancellation requires a stable identity and reason")
        identity = record_identity(experiment_id, "cancel-request", submission_identity)
        prepared = records.prepare(experiment_id=experiment_id, kind="service_control", record_id=identity,
            submission_identity="cancel-request:" + submission_identity,
            value={"test_id": test_id, "operation": "cancel_request", "reason": reason}, links=(("test", test_id),))
        queued = records.db.execute("SELECT 1 FROM research_work_queue WHERE work_id=?", (test_id,)).fetchone()
        if queued:
            ExperimentWorkQueue(records).cancel(test_id, guard=lambda: records._insert(prepared))
        elif records.status(test_id) in TERMINAL:
            with records._transaction():
                records._insert(prepared)
        else:
            def unstarted():
                if (records.db.execute("SELECT 1 FROM research_work_queue WHERE work_id=?", (test_id,)).fetchone()
                        or records.db.execute("SELECT 1 FROM lagent_worker_leases WHERE test_id=?", (test_id,)).fetchone()
                        or records.db.execute("SELECT 1 FROM lagent_records WHERE kind='service_input' AND "
                            "json_extract(value_json,'$.test_id')=?", (test_id,)).fetchone()):
                    raise Conflict("Test admission or ownership changed; reconcile its shared queue before cancellation")
                records._insert(prepared)
            records._commit(test_id, lease=None, phase_id="lifecycle", action_id=identity, attempt=0,
                kind="owner_cancellation", payload={"reason": reason, "request_id": identity}, simulated_at=None,
                transition_to="cancelled", guard=unstarted)
        # Observe status and scheduling from one committed snapshot while the
        # original worker may concurrently finish its cleanup.
        records.db.execute("BEGIN")
        try:
            status = records.status(test_id)
            queued = records.db.execute("SELECT 1 FROM research_work_queue WHERE work_id=?", (test_id,)).fetchone()
            work = read_work(records.db, test_id) if queued else None
            return {"request": records.read(identity), "test_id": test_id, "status": status,
                    "terminal": status in TERMINAL,
                    "cancel_pending": bool(work and work.cancel_requested and status not in TERMINAL),
                    "queue_state": work.state if work else None, "execution_available": False}
        finally:
            records.db.rollback()  # Read-only snapshot; the request already committed.
