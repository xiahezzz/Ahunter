from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3
from threading import Barrier

import pytest

from advisor.db.migrate import migrate_database
from advisor.research.artifacts import ArtifactStore
from advisor.research.repository import ResearchRepository
from advisor.research.experiments.repository import Conflict, ExperimentStore
from advisor.research.experiments.records import ExperimentRecords, Fenced, OutboxMessage, ProjectionUpdate


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "state.sqlite"
    migrate_database(path)
    connection = sqlite3.connect(path)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    host = ResearchRepository(connection)
    experiments = host.experiment_store(artifacts=artifacts)
    draft = experiments.create({"name": "storage fixture"}, "experiment-submission")
    clock = [datetime(2026, 9, 9, tzinfo=timezone.utc)]
    records = experiments.records(clock=lambda: clock[0])
    records.put(experiment_id=draft["experiment_id"], kind="test", record_id="test-1", submission_identity="test-1", value={"repeat_index": 0})
    records.transition("test-1", "preflight", action_id="preflight")
    records.transition("test-1", "queued", action_id="queue")
    lease = records.claim("test-1", worker_id="worker-1", lease_seconds=60)
    records.transition("test-1", "running", action_id="run", lease=lease)
    yield records, lease, draft["experiment_id"], clock, path, artifacts
    artifacts.close()
    connection.close()


def side_effect(records, lease, *, name="cash_freeze", amount="5", revision=None):
    return records.commit(lease, phase_id="auction", action_id=name, attempt=0, kind=name,
                          payload={"amount": amount}, simulated_at=datetime(2026, 8, 3, 9, 24, 30, tzinfo=timezone(timedelta(hours=8))),
                          updates=(ProjectionUpdate(name, revision, {"amount": amount}),),
                          outbox=(OutboxMessage("outbox-" + name, "research-service", {"action": name}),))


@pytest.mark.parametrize("effect", ["cash_freeze", "fill", "commission", "cancel_release", "memory_commit", "cost_reserve", "cost_settle"])
@pytest.mark.parametrize("point", ["before_event", "after_event", "after_projection", "after_outbox", "before_commit", "after_commit"])
def test_each_side_effect_is_exactly_once_across_crash_points(ledger, effect, point):
    records, lease, _, _, _, _ = ledger
    def crash(actual):
        if actual == point:
            raise KeyboardInterrupt("simulated process interruption")
    records.fault = crash
    with pytest.raises(KeyboardInterrupt):
        side_effect(records, lease, name=effect)
    assert not records.db.in_transaction
    records.fault = lambda _: None
    first = side_effect(records, lease, name=effect)
    assert side_effect(records, lease, name=effect) == first
    assert records.projection("test-1", effect)["value"] == {"amount": "5"}
    assert records.db.execute("SELECT COUNT(*) FROM lagent_events WHERE action_id=?", (effect,)).fetchone()[0] == 1
    assert len(records.pending_outbox(limit=100)) == 1
    assert records.replay("test-1")[effect] == records.projection("test-1", effect)


def test_same_action_with_different_content_conflicts(ledger):
    records, lease, *_ = ledger
    side_effect(records, lease)
    with pytest.raises(Conflict, match="different content"):
        side_effect(records, lease, amount="6")
    assert records.projection("test-1", "cash_freeze")["value"] == {"amount": "5"}


def test_loss_of_lease_fences_old_worker_and_recovery_appends_attempt(ledger):
    records, old, experiment_id, clock, _, _ = ledger
    side_effect(records, old)
    with pytest.raises(Fenced, match="live"):
        records.claim("test-1", worker_id="worker-2", lease_seconds=60)
    clock[0] += timedelta(seconds=60)
    new = records.claim("test-1", worker_id="worker-2", lease_seconds=60)
    assert new.generation == old.generation + 1
    with pytest.raises(Fenced):
        side_effect(records, old, name="late_fill")
    with pytest.raises(Fenced):
        records.renew(old, lease_seconds=60)
    # Acknowledging a previously committed action has no new side effect.
    assert side_effect(records, old)["generation"] == old.generation
    assert side_effect(records, new, name="fill")["generation"] == new.generation
    attempts = records.page(experiment_id=experiment_id, kind="attempt", limit=10)["items"]
    assert [item["value"]["generation"] for item in attempts] == [1, 2]
    assert records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='test'").fetchone()[0] == 1


@pytest.mark.parametrize("target", ["completed", "blocked", "failed", "cancelled"])
def test_terminal_history_cannot_be_rewritten_and_reruns_are_linked(ledger, target):
    records, lease, experiment_id, clock, *_ = ledger
    if target == "completed":
        records.transition("test-1", "evaluating", action_id="evaluate", lease=lease)
    terminal = records.transition("test-1", target, action_id="finish", lease=lease, reason="fixture")
    assert records.transition("test-1", target, action_id="finish", lease=lease, reason="fixture") == terminal
    with pytest.raises(Conflict):
        records.transition("test-1", "running", action_id="revive", lease=lease)
    clock[0] += timedelta(seconds=61)
    with pytest.raises(Fenced):
        records.claim("test-1", worker_id="recovery", lease_seconds=60)
    rerun = records.put(experiment_id=experiment_id, kind="test", record_id="rerun-1", submission_identity="rerun-1",
                        value={"repeat_index": 0}, links=(("rerun_of", "test-1"),))
    records.put(experiment_id=experiment_id, kind="evaluation", record_id="rescore-1", submission_identity="rescore-1",
                value={"valid": False}, links=(("test", "test-1"),))
    assert records.status("test-1") == target
    assert records.status(rerun["record_id"]) == "created"
    assert records.related("rerun-1") == [{"relation": "rerun_of", "target_id": "test-1"}]


def test_state_can_only_be_changed_through_legal_lifecycle(ledger):
    records, lease, *_ = ledger
    with pytest.raises(ValueError, match="transition"):
        records.commit(lease, phase_id="", action_id="bypass", attempt=0, kind="edit", payload={}, simulated_at=None,
                       updates=(ProjectionUpdate("status", None, {"status": "completed"}),))
    with pytest.raises(Conflict, match="transition"):
        records.transition("test-1", "completed", action_id="skip-evaluation", lease=lease)


def test_outbox_delivery_is_append_only_and_receipt_is_idempotent(ledger):
    records, lease, *_ = ledger
    side_effect(records, lease)
    message = records.pending_outbox(limit=1)[0]
    receipt = {"request_id": "durable-research-request"}
    assert records.acknowledge_outbox(message["outbox_id"], receipt) == receipt
    assert records.acknowledge_outbox(message["outbox_id"], receipt) == receipt
    assert records.pending_outbox(limit=1) == []
    with pytest.raises(Conflict):
        records.acknowledge_outbox(message["outbox_id"], {"request_id": "other"})
    assert records.db.execute("SELECT COUNT(*) FROM lagent_outbox").fetchone()[0] == 1


def test_lost_projection_is_detected_and_rebuilt_from_events(ledger):
    records, lease, *_ = ledger
    result = side_effect(records, lease)
    records.db.execute("DELETE FROM lagent_projections WHERE name='cash_freeze'")
    records.db.commit()
    with pytest.raises(Conflict, match="missing"):
        records.projection("test-1", "cash_freeze")
    projected = records.rebuild(lease)
    assert projected["cash_freeze"] == {"sequence": result["sequence"], "value": {"amount": "5"}}
    assert records.projection("test-1", "cash_freeze") == projected["cash_freeze"]


def test_stale_projection_cannot_silently_roll_back_an_account(ledger):
    records, lease, *_ = ledger
    first = side_effect(records, lease)
    records.commit(lease, phase_id="auction", action_id="second-freeze", attempt=0, kind="freeze", payload={}, simulated_at=None,
                   updates=(ProjectionUpdate("cash_freeze", first["sequence"], {"amount": "10"}),))
    records.db.execute("UPDATE lagent_projections SET event_sequence=? WHERE name='cash_freeze'", (first["sequence"],))
    records.db.commit()
    with pytest.raises(Conflict, match="stale"):
        records.projection("test-1", "cash_freeze")
    assert records.rebuild(lease)["cash_freeze"]["value"] == {"amount": "10"}


def test_concurrent_children_share_projection_revision_without_double_spend(ledger):
    records, lease, _, clock, path, _ = ledger
    initial = records.commit(lease, phase_id="initial", action_id="balance", attempt=0, kind="budget", payload={}, simulated_at=None,
                             updates=(ProjectionUpdate("budget", None, {"available": "10"}),))
    barrier = Barrier(2)
    def reserve(index):
        db = sqlite3.connect(path)
        peer = ExperimentRecords(db, clock=lambda: clock[0])
        current = peer.projection("test-1", "budget")
        barrier.wait()
        try:
            peer.commit(lease, phase_id="initial", action_id=f"reserve-{index}", attempt=0, kind="reserve", payload={}, simulated_at=None,
                        updates=(ProjectionUpdate("budget", current["sequence"], {"available": str(Decimal(current["value"]["available"]) - 7)}),))
            return "committed"
        except Conflict:
            return "conflict"
        finally:
            db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, (1, 2)))
    assert sorted(results) == ["committed", "conflict"]
    assert records.projection("test-1", "budget")["value"] == {"available": "3"}
    assert records.projection("test-1", "budget")["sequence"] > initial["sequence"]


def test_pagination_retains_all_records_and_excludes_later_appends(ledger):
    records, _, experiment_id, *_ = ledger
    for index in range(9):
        records.put(experiment_id=experiment_id, kind="candidate_proposal", record_id=f"candidate-{index}",
                    submission_identity=f"candidate-{index}", value={"index": index})
    first = records.page(experiment_id=experiment_id, kind="candidate_proposal", limit=3)
    records.put(experiment_id=experiment_id, kind="candidate_proposal", record_id="late", submission_identity="late", value={"index": 99})
    items = first["items"]
    cursor = first["next_cursor"]
    while cursor:
        page = records.page(experiment_id=experiment_id, kind="candidate_proposal", limit=3, cursor=cursor)
        items.extend(page["items"])
        cursor = page["next_cursor"]
    assert [item["value"]["index"] for item in items] == list(range(9))
    assert len(records.page(experiment_id=experiment_id, kind="candidate_proposal", limit=100)["items"]) == 10
    with pytest.raises(ValueError, match="cursor"):
        records.page(experiment_id=experiment_id, kind="test", limit=3, cursor=first["next_cursor"])


@pytest.mark.parametrize("point", ["after_record", "after_artifact_references", "before_commit", "after_commit"])
def test_artifact_commit_crashes_leave_recoverable_orphans_without_false_references(ledger, point):
    records, _, experiment_id, _, _, artifacts = ledger
    sealed = artifacts.put_text("candidate bytes")
    def save():
        return records.put(experiment_id=experiment_id, kind="candidate_package", record_id="package-1", submission_identity="package-1",
                           value={"hash": sealed.content_hash}, artifact_hashes=(sealed.content_hash,))
    def crash(actual):
        if actual == point:
            raise OSError("disk interruption")
    records.fault = crash
    with pytest.raises(OSError):
        save()
    records.fault = lambda _: None
    assert artifacts.verify(sealed.content_hash)
    committed = records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE record_id='package-1'").fetchone()[0]
    assert committed == int(point == "after_commit")
    assert save() == save()
    assert records.db.execute("SELECT COUNT(*) FROM lagent_artifact_links").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        records.db.execute("DELETE FROM research_artifacts WHERE content_hash=?", (sealed.content_hash,))
    records.db.rollback()


def test_corrupt_or_missing_artifacts_never_get_published_as_complete(ledger):
    records, _, experiment_id, _, _, artifacts = ledger
    ref = artifacts.put_text("source")
    (artifacts.root / ref.content_hash[:2] / ref.content_hash[2:]).write_bytes(b"bad")
    with pytest.raises(ValueError, match="hash mismatch"):
        records.put(experiment_id=experiment_id, kind="candidate_package", record_id="bad", submission_identity="bad",
                    value={}, artifact_hashes=(ref.content_hash,))
    assert records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE record_id='bad'").fetchone()[0] == 0


def test_old_database_migration_preserves_drafts_and_is_idempotent(tmp_path):
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.executescript('''CREATE TABLE lagent_experiments (
      experiment_id TEXT PRIMARY KEY, submission_identity TEXT UNIQUE, name TEXT,
      raw_json TEXT, input_hash TEXT, config_json TEXT, config_hash TEXT, created_at TEXT);
      INSERT INTO lagent_experiments VALUES ('old', 'old', 'legacy', '{}', 'raw', '{}', 'config', '2026-09-01');''')
    db.close()
    migrate_database(path)
    migrate_database(path)
    with sqlite3.connect(path) as migrated:
        assert migrated.execute("SELECT name FROM lagent_experiments WHERE experiment_id='old'").fetchone()[0] == "legacy"
        assert migrated.execute("SELECT COUNT(*) FROM lagent_events").fetchone()[0] == 0


@pytest.mark.parametrize("table", ["lagent_records", "lagent_events", "lagent_experiments"])
def test_permanent_facts_reject_sql_delete_and_update(ledger, table):
    records, *_ = ledger
    with pytest.raises(sqlite3.IntegrityError, match="permanent"):
        records.db.execute(f"DELETE FROM {table}")
    records.db.rollback()
    column = "kind" if table != "lagent_experiments" else "name"
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        records.db.execute(f"UPDATE {table} SET {column}={column}")
    records.db.rollback()


def test_preflight_idempotency_rejects_changed_report(ledger):
    records, _, experiment_id, *_ = ledger
    store = ExperimentStore(records.db)
    report = {"status": "blocked", "checks": []}
    first = store.record_preflight(experiment_id, "preflight-1", report)
    assert store.record_preflight(experiment_id, "preflight-1", report) == first
    with pytest.raises(Conflict):
        store.record_preflight(experiment_id, "preflight-1", {"status": "passed", "checks": []})


def test_record_retry_identity_and_cross_experiment_links_fail_closed(ledger):
    records, _, experiment_id, *_ = ledger
    raw = {"experiment_id": experiment_id, "kind": "correction", "record_id": "correction-1", "submission_identity": "correction-1",
           "value": {"cost": "unsettled"}, "links": (("test", "test-1"),)}
    first = records.put(**raw)
    assert records.put(**raw) == first
    with pytest.raises(Conflict, match="different content"):
        records.put(**{**raw, "value": {"cost": "0"}})
    with pytest.raises(Conflict, match="another submission"):
        records.put(**{**raw, "submission_identity": "new-identity"})
    other = ExperimentStore(records.db).create({}, "other-experiment")
    with pytest.raises(Conflict, match="boundary"):
        records.put(**{**raw, "experiment_id": other["experiment_id"], "record_id": "cross-link", "submission_identity": "cross-link"})


def test_artifact_directory_durability_failure_cannot_publish_a_reference(ledger, monkeypatch):
    records, _, experiment_id, _, _, artifacts = ledger
    def fail(_):
        raise OSError("directory fsync failure")
    original_sync = artifacts._sync_directory
    monkeypatch.setattr(artifacts, "_sync_directory", fail)
    with pytest.raises(OSError, match="fsync"):
        artifacts.put_text("new uncommitted artifact")
    assert records.db.execute("SELECT COUNT(*) FROM lagent_artifact_links").fetchone()[0] == 0

    # A retry encountering the renamed but previously uncommitted file must fsync
    # again, rather than treating its mere existence as durable success.
    with pytest.raises(OSError, match="fsync"):
        artifacts.put_text("new uncommitted artifact")
    monkeypatch.setattr(artifacts, "_sync_directory", original_sync)
    ref = artifacts.put_text("new uncommitted artifact")
    assert artifacts.verify(ref.content_hash)
