from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from advisor.db.migrate import migrate_database
from advisor.research.artifacts import ArtifactStore
from advisor.research.experiments.candidates import seal_candidate
from advisor.research.experiments.contracts import CandidateProposal, original_case
from advisor.research.experiments.queries import ExperimentQueries, QueryDenied
from advisor.research.experiments.records import ExperimentRecords
from advisor.research.experiments.registration import ExperimentRegistry, record_identity
from advisor.research.experiments.repository import Conflict, ExperimentStore
from advisor.research.experiments.resolution import resolve
from advisor.research.experiments.sources import SourceRetention
from tests.advisor.research.test_experiment_contracts import calendar


@pytest.fixture
def registry(tmp_path, request):
    path = tmp_path / "state.sqlite"
    migrate_database(path)
    db = sqlite3.connect(path)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    clock = [datetime(2026, 9, 9, tzinfo=timezone.utc)]
    records = ExperimentRecords(db, artifacts=artifacts, clock=lambda: clock[0])
    registry = ExperimentRegistry(records)
    raw = original_case()
    draft = ExperimentStore(db).create(raw, "fixture-experiment")
    experiment = draft["experiment_id"]
    sealed = resolve(raw, calendar=calendar).specification
    definition = registry.definition(experiment, sealed, submission_identity="definition-1")
    root = tmp_path / "candidate"
    root.mkdir()
    for name in ("main.py", "prompt.md", "requirements.lock", "io.json"):
        (root / name).write_text(name)
    if getattr(request, "param", None) is not None:
        (root / "main.py").write_text(request.param)
    package = seal_candidate(root, {"source_paths": ["main.py"], "prompt_paths": ["prompt.md"],
                                   "dependency_lock_paths": ["requirements.lock"], "contract_paths": ["io.json"],
                                   "entrypoint": "main.py", "input_contract": "phase@1", "output_contract": "actions@1", "allowed_config": {}},
                             artifacts=artifacts)
    yield registry, experiment, definition, package, clock
    artifacts.close()
    db.close()


def candidate(registry, experiment, package, name="baseline", parent=None):
    proposal = CandidateProposal(proposal_id=name, package_hash=package.package_hash, parent_proposal_id=parent,
                                 hypothesis="test sealed candidate", source="manual", diff_artifact_hash=None)
    return registry.candidate(experiment, proposal, submission_identity="submit-" + name)


def plan_input(definition, *, names=("baseline",), task="august-tuning", plan_id="plan-1", repeats=3):
    return {"plan_id": plan_id, "specification_hash": definition["value"]["specification"]["specification_hash"],
            "seed_support": "unsupported", "tests": [
                {"test_id": f"{plan_id}-{name}-{index}", "proposal_id": name, "task_id": task,
                 "repeat_id": f"{plan_id}-repeat-{index}", "repeat_index": index, "seed": None}
                for index in range(repeats) for name in names]}


def register_plan(fixture, *, task="august-tuning", purpose="tuning", plan_id="plan-1"):
    registry, experiment, definition, package, _ = fixture
    candidate(registry, experiment, package)
    return registry.test_plan(experiment, definition["record_id"], plan_input(definition, task=task, plan_id=plan_id),
                              submission_identity=plan_id, purpose=purpose)


def test_registration_preserves_identity_and_independent_duplicate_proposals(registry):
    service, experiment, definition, package, _ = registry
    first = candidate(service, experiment, package)
    second = candidate(service, experiment, package, name="branch", parent="baseline")
    assert second["duplicate_content"]
    assert first["package"]["record_id"] == second["package"]["record_id"]
    assert first["proposal"]["record_id"] != second["proposal"]["record_id"]
    assert service.records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='test'").fetchone()[0] == 0
    assert {item["relation"] for item in service.records.related(second["proposal"]["record_id"])} == {"package", "parent"}
    assert candidate(service, experiment, package)["proposal"] == first["proposal"]
    with pytest.raises(Conflict):
        service.candidate(experiment, {**first["proposal"]["value"]["proposal"], "hypothesis": "changed"}, submission_identity="submit-baseline")
    sealed = definition["value"]["specification"]
    assert service.definition(experiment, sealed, submission_identity="definition-1") == definition
    with pytest.raises(ValueError, match="hash mismatch"):
        service.definition(experiment, {**sealed, "effective_json": sealed["effective_json"].replace('200000', '300000')}, submission_identity="bad")


def test_missing_parent_rolls_back_registration_and_cross_experiment_ids_do_not_alias(registry):
    service, experiment, definition, package, _ = registry
    with pytest.raises(LookupError):
        candidate(service, experiment, package, name="orphan", parent="missing")
    assert service.records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='candidate_package'").fetchone()[0] == 0
    first = candidate(service, experiment, package)
    other = ExperimentStore(service.records.db).create({}, "other-experiment")["experiment_id"]
    second = candidate(service, other, package)
    assert first["proposal"]["record_id"] != second["proposal"]["record_id"]
    with pytest.raises(Conflict, match="experiment"):
        service.test_plan(other, definition["record_id"], plan_input(definition), submission_identity="bad", purpose="tuning")


@pytest.mark.parametrize("point", ["after_record", "after_artifact_references", "before_commit", "after_commit"])
def test_candidate_package_and_proposal_commit_atomically(registry, point):
    service, experiment, _, package, _ = registry
    def fail(actual):
        if actual == point:
            raise KeyboardInterrupt("registration interrupted")
    service.records.fault = fail
    with pytest.raises(KeyboardInterrupt):
        candidate(service, experiment, package)
    service.records.fault = lambda _: None
    counts = dict(service.records.db.execute("SELECT kind, COUNT(*) FROM lagent_records GROUP BY kind"))
    assert counts.get("candidate_package", 0) == counts.get("candidate_proposal", 0) == int(point == "after_commit")
    first = candidate(service, experiment, package)
    assert candidate(service, experiment, package) == first


@pytest.mark.parametrize("fail_after", [1, 2, 3, 4])
def test_predeclared_sample_set_is_never_partially_registered(registry, fail_after):
    service, experiment, definition, package, _ = registry
    candidate(service, experiment, package)
    count = 0
    def fail(point):
        nonlocal count
        if point == "after_record":
            count += 1
            if count == fail_after:
                raise OSError("sample insertion interrupted")
    service.records.fault = fail
    with pytest.raises(OSError):
        register_plan(registry)
    service.records.fault = lambda _: None
    assert service.records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind IN ('test_plan', 'test')").fetchone()[0] == 0
    first = register_plan(registry)
    assert register_plan(registry) == first
    assert len(first["tests"]) == 3
    assert not first["execution_available"]
    assert all(service.records.status(item["record_id"]) == "created" for item in first["tests"])


def test_plan_requires_complete_interleaved_matrix_and_exact_conditions(registry):
    service, experiment, definition, package, _ = registry
    for name in ("baseline", "branch"):
        candidate(service, experiment, package, name)
    raw = plan_input(definition, names=("baseline", "branch"))
    def submit(raw, purpose="tuning"):
        return service.test_plan(experiment, definition["record_id"], raw, submission_identity="plan-1", purpose=purpose)
    with pytest.raises(Conflict, match="complete"):
        submit({**raw, "tests": raw["tests"][:-1]})
    with pytest.raises(Conflict, match="interleaved"):
        submit({**raw, "tests": sorted(raw["tests"], key=lambda s: s["proposal_id"])})
    with pytest.raises(Conflict, match="specification"):
        submit({**raw, "specification_hash": "a" * 64})
    with pytest.raises(Conflict, match="role"):
        submit(raw, purpose="selection_validation")
    with pytest.raises(ValueError, match="holdout"):
        submit(raw, purpose="final_holdout")
    assert len(submit(raw)["tests"]) == 6
    with pytest.raises(Conflict):
        submit({**raw, "tests": list(reversed(raw["tests"]))})


def test_calibration_and_selection_plans_keep_their_own_roles(registry):
    calibrated = register_plan(registry, purpose="calibration", plan_id="calibration")
    selection = register_plan(registry, task="august-selection", purpose="selection_validation", plan_id="selection")
    assert all(item["value"]["purpose"] == "calibration" for item in calibrated["tests"])
    assert all(item["value"]["role"] == "selection_validation" for item in selection["tests"])


def test_rerun_is_linked_and_cannot_replace_original_comparison_sample(registry):
    service, experiment, *_ = registry
    original = register_plan(registry)["tests"][0]
    with pytest.raises(Conflict, match="terminal"):
        service.rerun(experiment, original["record_id"], rerun_identity="again", reason="fixture")
    service.records.transition(original["record_id"], "cancelled", action_id="cancel")
    rerun = service.rerun(experiment, original["record_id"], rerun_identity="again", reason="fixture")
    assert not rerun["value"]["eligible_for_original_comparison"]
    assert service.records.status(original["record_id"]) == "cancelled"
    assert service.records.status(rerun["record_id"]) == "created"
    assert service.rerun(experiment, original["record_id"], rerun_identity="again", reason="fixture") == rerun


def hidden_test(fixture):
    return register_plan(fixture, task="august-selection", purpose="selection_validation", plan_id="hidden")["tests"][0]


def test_hidden_details_and_artifacts_require_audit_before_release(registry):
    service, experiment, definition, package, _ = registry
    hidden = hidden_test(registry)
    records = service.records
    optimizer = ExperimentQueries(records, viewer="optimizer")
    owner = ExperimentQueries(records, viewer="owner")
    for query in (optimizer, owner):
        with pytest.raises(QueryDenied):
            query.detail(hidden["record_id"])
    with pytest.raises(QueryDenied):
        optimizer.detail(definition["record_id"], audit_identity="cannot-opt-in")
    assert owner.detail(hidden["record_id"], audit_identity="audit-1") == hidden
    assert owner.detail(hidden["record_id"], audit_identity="audit-1") == hidden
    exposures = records.page(experiment_id=experiment, kind="exposure", limit=10)["items"]
    assert len(exposures) == 1
    assert exposures[0]["value"]["scopes"][0]["role"] == "selection_validation"
    assert exposures[0]["value"]["scopes"][0]["trading_dates"][-1] == "2026-08-14"
    artifact = records.artifacts.put_text("SECRET-FUTURE-RESULT")
    child = records.put(experiment_id=experiment, kind="evaluation", record_id="hidden-eval", submission_identity="hidden-eval",
                        value={"result": "SECRET-FUTURE-RESULT"}, links=(("test", hidden["record_id"]),), artifact_hashes=(artifact.content_hash,))
    with pytest.raises(QueryDenied):
        optimizer.artifact(child["record_id"], artifact.content_hash)
    def fail(point):
        if point == "before_commit":
            raise OSError("audit storage unavailable")
    records.fault = fail
    with pytest.raises(OSError, match="audit"):
        owner.artifact(child["record_id"], artifact.content_hash, audit_identity="audit-failure")
    records.fault = lambda _: None
    assert owner.artifact(child["record_id"], artifact.content_hash, audit_identity="audit-good") == b"SECRET-FUTURE-RESULT"
    with pytest.raises(LookupError):
        owner.artifact(hidden["record_id"], artifact.content_hash, audit_identity="unlinked")


def test_tuning_feedback_is_readable_and_overview_never_leaks_detail(registry):
    service, experiment, *_ = registry
    tuning = register_plan(registry)["tests"][0]
    hidden = hidden_test(registry)
    optimizer = ExperimentQueries(service.records, viewer="optimizer")
    assert optimizer.detail(tuning["record_id"]) == tuning
    all_items, cursor = [], None
    while True:
        page = optimizer.page(experiment_id=experiment, kind="test", limit=2, cursor=cursor)
        all_items.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(all_items) == 6
    assert all("value" not in item for item in all_items)
    assert next(item for item in all_items if item["record_id"] == hidden["record_id"])["detail_hidden"]
    with pytest.raises(QueryDenied):
        optimizer.events(hidden["record_id"], after=0, limit=10)


def test_selection_feedback_is_explicit_aggregate_allowlist_and_holdout_never_feedback(registry):
    service, experiment, definition, *_ = registry
    hidden = hidden_test(registry)
    records = service.records
    summary = {"baseline_mean_return": "0.1", "candidate_mean_return": "0.2", "mean_improvement": "0.1", "worst_paired_difference": "0.05",
               "positive_repeats": 3, "planned_repeats": 3, "decision": "promoted", "trace": "SECRET"}
    comparison = records.put(experiment_id=experiment, kind="comparison", record_id="compare", submission_identity="compare",
                             value={"status": "completed", "public_summary": summary, "trace": "SECRET"}, links=(("test", hidden["record_id"]),))
    optimizer = ExperimentQueries(records, viewer="optimizer")
    feedback = optimizer.comparison_feedback(comparison["record_id"])
    assert feedback["decision"] == "promoted"
    assert "SECRET" not in json.dumps(feedback)
    with pytest.raises(QueryDenied):
        optimizer.detail(comparison["record_id"])
    # Fixture for the future selection-freeze registrar, using a genuine holdout scope.
    final = records.put(experiment_id=experiment, kind="test", record_id="holdout-fixture", submission_identity="holdout-fixture",
                        value={"registration_version": 1, "definition_id": definition["record_id"], "task_id": "august-holdout"})
    holdout = records.put(experiment_id=experiment, kind="comparison", record_id="final-compare", submission_identity="final-compare",
                          value=comparison["value"], links=(("test", final["record_id"]),))
    with pytest.raises(QueryDenied):
        optimizer.comparison_feedback(holdout["record_id"])
    unknown = records.put(experiment_id=experiment, kind="cost", record_id="unscoped", submission_identity="unscoped", value={"secret": "SECRET"})
    with pytest.raises(QueryDenied):
        optimizer.detail(unknown["record_id"])


def source(fixture, *, source_id="news", text="original article", expiry_hours=1):
    service, experiment, _, _, clock = fixture
    test = register_plan(fixture)["tests"][0]
    ref = service.records.artifacts.put_text(text)
    retention = SourceRetention(service.records)
    document = {"source_id": source_id, "content_hash": ref.content_hash, "source_ref": "fixture-provider@1", "title": "article",
                "url": "https://example.com/article", "fetched_at": clock[0].isoformat(), "expires_at": (clock[0] + timedelta(hours=expiry_hours)).isoformat(),
                "retention_policy_ref": "fixture-source-retention@1"}
    return retention, retention.register(experiment, test["record_id"], document, submission_identity=source_id)


@pytest.mark.parametrize("point", ["before_source_unlink", "after_source_unlink", "after_commit"])
def test_source_expiry_is_resumable_and_keeps_metadata(registry, point):
    service, _, _, _, clock = registry
    retention, document = source(registry)
    original_hash = document["value"]["document"]["content_hash"]
    clock[0] += timedelta(hours=2)
    assert retention.describe(document["record_id"])["replay"] == "metadata_and_hash_only"
    with pytest.raises(FileNotFoundError):
        retention.read_bytes(document["record_id"])
    def fail(actual):
        if actual == point:
            raise OSError("expiry interrupted")
    service.records.fault = fail
    with pytest.raises(OSError):
        retention.expire(document["record_id"], submission_identity="expiry")
    service.records.fault = lambda _: None
    result = retention.expire(document["record_id"], submission_identity="expiry")
    assert result["bytes_deleted"]
    assert not service.artifacts.verify(original_hash)
    assert retention.expire(document["record_id"], submission_identity="expiry")["expiration"] == result["expiration"]
    assert service.records.read(document["record_id"]) == document
    assert result["document"]["content_hash"] == original_hash


def test_source_expiry_never_deletes_live_shared_or_permanent_bytes(registry):
    service, experiment, _, _, clock = registry
    retention, first = source(registry)
    _, second = source(registry, source_id="newer-license", expiry_hours=5)
    clock[0] += timedelta(hours=2)
    assert not retention.expire(first["record_id"], submission_identity="first-expiry")["bytes_deleted"]
    assert retention.read_bytes(second["record_id"]) == b"original article"
    content_hash = first["value"]["document"]["content_hash"]
    service.records.put(experiment_id=experiment, kind="candidate_package", record_id="permanent", submission_identity="permanent",
                        value={"test": "shared internal artifact"}, artifact_hashes=(content_hash,))
    clock[0] += timedelta(hours=5)
    assert not retention.expire(second["record_id"], submission_identity="second-expiry")["bytes_deleted"]
    assert service.artifacts.read_bytes(content_hash) == b"original article"
    with pytest.raises(FileNotFoundError):
        retention.read_bytes(second["record_id"])


def test_prepared_reference_cannot_commit_after_source_bytes_expire(registry):
    service, experiment, _, _, clock = registry
    retention, document = source(registry)
    content_hash = document["value"]["document"]["content_hash"]
    prepared = service.records.prepare(experiment_id=experiment, kind="cost", record_id="racing", submission_identity="racing",
                                       value={}, artifact_hashes=(content_hash,))
    clock[0] += timedelta(hours=2)
    retention.expire(document["record_id"], submission_identity="expiry")
    with pytest.raises(FileNotFoundError):
        with service.records._transaction():
            service.records._insert(prepared)
    assert service.records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE record_id='racing'").fetchone()[0] == 0


def test_owner_external_exposure_survives_task_renaming(registry):
    service, experiment, *_ = registry
    owner = ExperimentQueries(service.records, viewer="owner")
    periods = [{"start": "2026-08-17", "end": "2026-08-21"}]
    declared = owner.declare_exposure(experiment, submission_identity="external-seen", periods=periods, note="Viewed these dates outside the app")
    assert declared["value"]["periods"] == periods
    assert owner.declare_exposure(experiment, submission_identity="external-seen", periods=periods, note="Viewed these dates outside the app") == declared
    with pytest.raises(QueryDenied):
        ExperimentQueries(service.records, viewer="optimizer").declare_exposure(experiment, submission_identity="fake", periods=periods, note="discard")


def test_record_export_separates_hash_audit_from_original_replay(registry):
    service, _, _, _, clock = registry
    retention, document = source(registry)
    owner = ExperimentQueries(service.records, viewer="owner")
    before = owner.export(document["record_id"])
    assert before["schema_version"] == 1
    assert before["record_replay"] == "exact_bytes"
    clock[0] += timedelta(hours=2)
    after = owner.export(document["record_id"])
    assert after["record_replay"] == "metadata_and_hash_only"
    assert after["record"]["content_hash"] == before["record"]["content_hash"]
    hidden = hidden_test(registry)
    with pytest.raises(QueryDenied):
        ExperimentQueries(service.records, viewer="optimizer").export(hidden["record_id"])


def test_expiry_does_not_follow_a_tampered_artifact_symlink(registry):
    service, experiment, _, _, clock = registry
    retention, document = source(registry)
    original_hash = document["value"]["document"]["content_hash"]
    protected = service.artifacts.put_text("protected independent bytes")
    original = service.artifacts.root / original_hash[:2] / original_hash[2:]
    target = service.artifacts.root / protected.content_hash[:2] / protected.content_hash[2:]
    original.unlink()
    original.symlink_to(target)
    clock[0] += timedelta(hours=2)
    retention.expire(document["record_id"], submission_identity="expiry")
    assert service.artifacts.read_bytes(protected.content_hash) == b"protected independent bytes"
    assert not original.is_symlink()


def test_source_registration_retry_after_expiry_does_not_restore_original(registry):
    service, experiment, _, _, clock = registry
    retention, original = source(registry)
    test_id = service.records.related(original["record_id"])[0]["target_id"]
    clock[0] += timedelta(hours=2)
    retention.expire(original["record_id"], submission_identity="expiry")
    assert retention.register(experiment, test_id, original["value"]["document"], submission_identity="news") == original
    assert retention.describe(original["record_id"])["availability"] == "unavailable"
    with pytest.raises(Conflict):
        retention.register(experiment, test_id, {**original["value"]["document"], "title": "changed"}, submission_identity="news")
