import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from advisor.db.migrate import migrate_database
from advisor.research.artifacts import ArtifactStore
from advisor.research.experiments.candidates import read_candidate, seal_candidate
from advisor.research.experiments.contracts import (
    CalendarResolution, CandidateProposal, ExperimentDraft, TestPlan,
    date_search_boundary, original_case,
)
from advisor.research.experiments.repository import ExperimentStore, Conflict
from advisor.research.experiments.resolution import digest, resolve


# Deliberate calendar fixture, not a fallback weekday generator or historical evidence.
SESSIONS = tuple(date(2026, 8, d) for d in (3, 4, 5, 6, 7, 10, 11, 12, 13, 14, 17, 18, 19, 20, 21))


def calendar(ref, period):
    return CalendarResolution(calendar_ref=ref, content_hash=digest([d.isoformat() for d in SESSIONS]),
                              coverage_start=date(2026, 8, 1), coverage_end=date(2026, 8, 31),
                              sessions=SESSIONS, complete=True)


def test_original_preset_resolves_all_roles_without_claiming_capability():
    result = resolve(original_case(), calendar=calendar)
    assert result.status == "resolved", result.errors
    spec = result.specification
    effective = json.loads(spec.effective_json)
    assert effective["account"]["initial_cash"] == "200000"
    assert effective["model"] == {"model": "gpt-5.5", "reasoning_effort": "high", "token_cap": None}
    assert effective["budget"]["task_cost_limit"] == "calibration_derived"
    assert effective["evaluate"]["positive_repeat_fraction"] == {"numerator": 2, "denominator": 3}
    assert [task.trading_dates[-1].isoformat() for task in spec.tasks] == ["2026-08-07", "2026-08-14", "2026-08-21"]
    assert spec.tasks[0].initial_as_of.isoformat() == "2026-08-02T23:05:00+08:00"
    assert spec.capability_preflight_required


def test_decimal_normalization_and_input_order_have_stable_identity():
    first = original_case()
    other = dict(reversed(list(first.items())))
    other["account"] = {**first["account"], "initial_cash": "200000.0000"}
    a = resolve(first, calendar=calendar).specification
    b = resolve(other, calendar=calendar).specification
    assert a.specification_hash == b.specification_hash
    assert a.raw_json != b.raw_json


@pytest.mark.parametrize("section,key,value", [
    ("clock", "timezone", "Asia/Tokyo"),
    ("execution", "market_rule_ref", "sh-sz-historical@2"),
    ("account", "initial_cash", "320000"),
    ("runtime", "subagent_concurrency", 7),
    ("evaluate", "minimum_improvement", "0.002"),
    ("clock", "auction_as_of", "09:23:00"),
])
def test_conditions_change_identity(section, key, value):
    raw = original_case()
    before = resolve(raw, calendar=calendar).specification
    raw[section][key] = value
    after = resolve(raw, calendar=calendar).specification
    assert after is not None
    assert before.specification_hash != after.specification_hash


def test_units_are_exported_and_hashed(monkeypatch):
    from advisor.research.experiments.contracts import AccountSettings
    before = resolve(original_case(), calendar=calendar).specification
    metadata = AccountSettings.model_fields["initial_cash"].json_schema_extra
    monkeypatch.setitem(metadata, "unit", "CNY_cent")
    after = resolve(original_case(), calendar=calendar).specification
    assert before.specification_hash != after.specification_hash
    assert 'CNY_cent' in after.field_schema_json


def test_preset_sources_and_snapshot_are_detached_from_mutation():
    preset = original_case()
    raw = {"name": "my experiment", "account": {"initial_cash": "250000"}}
    result = resolve(raw, preset=preset, preset_ref="original-case@1", calendar=calendar)
    spec = result.specification
    assert json.loads(spec.provenance_json)["account.initial_cash"]["source"] == "input"
    assert json.loads(spec.provenance_json)["clock.timezone"]["source"] == "original-case@1"
    raw["account"]["initial_cash"] = "1"
    preset["clock"]["timezone"] = "UTC"
    assert json.loads(spec.effective_json)["account"]["initial_cash"] == "250000"
    assert json.loads(spec.effective_json)["clock"]["timezone"] == "Asia/Shanghai"
    with pytest.raises(ValueError, match="preset_ref"):
        resolve({}, preset=preset, calendar=calendar)


@pytest.mark.parametrize("mutate,code", [
    (lambda r: r["model"].pop("token_cap"), "missing_configuration"),
    (lambda r: r["runtime"].update(subagent_concurrency=None), "invalid_configuration"),
    (lambda r: r["runtime"].update(subagent_concurrency=True), "invalid_configuration"),
    (lambda r: r["tasks"][0]["period"].update(trading_days=-1), "invalid_configuration"),
    (lambda r: r["tasks"][0]["period"].update(end="2026-08-07"), "invalid_configuration"),
    (lambda r: r["clock"].update(auction_submit_at="09:25:00"), "invalid_configuration"),
    (lambda r: r["clock"].update(postauction_event_cutoff="09:25:05"), "invalid_configuration"),
    (lambda r: r["clock"].update(timezone="unknown/timezone"), "invalid_configuration"),
    (lambda r: r["clock"].update(auction_as_of="09:24:00+08:00"), "invalid_configuration"),
    (lambda r: r["budget"].update(mode="explicit"), "invalid_configuration"),
    (lambda r: r["account"].update(policy_ref="margin_v1"), "account_policy_unsupported"),
    (lambda r: r["account"].update(available_credit="100"), "account_policy_unsupported"),
    (lambda r: r["account"].update(initial_cash="0"), "invalid_initial_nav"),
    (lambda r: r["trace"].update(mandatory_audit=False), "invalid_configuration"),
    (lambda r: r["runtime"].update(heartbeat_seconds=60), "invalid_configuration"),
])
def test_invalid_and_missing_values_are_durable_error_data(mutate, code):
    raw = original_case()
    mutate(raw)
    result = resolve(raw, calendar=calendar)
    assert result.status == "unresolved"
    assert result.specification is None
    assert code in {e.code for e in result.errors}
    assert json.loads(result.raw_json) == raw


def test_arbitrary_duration_and_range_use_injected_sessions():
    raw = original_case()
    raw["tasks"][0]["period"]["trading_days"] = 7
    result = resolve(raw, calendar=calendar)
    assert result.specification.tasks[0].trading_dates[-1] == date(2026, 8, 11)
    raw["tasks"][0]["period"] = {"mode": "range", "start": "2026-08-03", "end": "2026-08-11"}
    ranged = resolve(raw, calendar=calendar)
    assert ranged.specification.tasks[0].trading_dates == result.specification.tasks[0].trading_dates


@pytest.mark.parametrize("changes", [
    {"complete": False}, {"calendar_ref": "other@1"},
    {"sessions": SESSIONS[:-1]}, {"coverage_start": date(2026, 8, 4), "sessions": SESSIONS[1:]},
])
def test_calendar_does_not_shorten_or_trust_incomplete_observations(changes):
    def incomplete(ref, period):
        return calendar(ref, period).model_dump() | changes
    result = resolve(original_case(), calendar=incomplete)
    assert result.specification is None
    assert "calendar_coverage_missing" in {e.code for e in result.errors}


def test_calendar_content_version_changes_identity():
    def revised(ref, period):
        return calendar(ref, period).model_copy(update={"content_hash": "a" * 64})
    assert resolve(original_case(), calendar=calendar).specification.specification_hash != resolve(original_case(), calendar=revised).specification.specification_hash


def test_numeric_schema_metadata_covers_every_business_number():
    schema = ExperimentDraft.model_json_schema()
    for name, definition in schema["$defs"].items():
        for field, prop in definition.get("properties", {}).items():
            variants = prop.get("anyOf", [prop])
            if any(v.get("type") in {"number", "integer"} for v in variants):
                assert prop.get("unit"), (name, field)
                assert prop.get("null_semantics"), (name, field)
                assert prop.get("default_source"), (name, field)


def test_search_boundary_keeps_previous_local_day():
    result = date_search_boundary(datetime.fromisoformat("2026-08-02T23:05:00+08:00"), source_timezone="Asia/Shanghai")
    assert result["last_permitted_date_inclusive"] == "2026-08-01"
    with pytest.raises(ValueError, match="timezone-aware"):
        date_search_boundary(datetime(2026, 8, 2), source_timezone="Asia/Shanghai")


@pytest.fixture
def candidate(tmp_path):
    root = tmp_path / "candidate"
    root.mkdir()
    for name in ("main.py", "prompt.md", "requirements.lock", "io.json"):
        (root / name).write_text(name)
    request = {"source_paths": ["main.py"], "prompt_paths": ["prompt.md"],
               "dependency_lock_paths": ["requirements.lock"], "contract_paths": ["io.json"],
               "entrypoint": "main.py", "input_contract": "phase@1", "output_contract": "actions@1",
               "allowed_config": {"memory_strategy": "episode-only"}}
    store = ArtifactStore(tmp_path / "artifacts")
    yield root, request, store
    store.close()


def test_candidate_seals_all_bytes_and_keeps_proposal_identity_separate(candidate):
    root, request, artifacts = candidate
    package = seal_candidate(root, request, artifacts=artifacts)
    again = seal_candidate(root, request, artifacts=artifacts)
    assert package == again
    proposals = [CandidateProposal(proposal_id=f"proposal-{i}", package_hash=package.package_hash,
                                  parent_proposal_id=None, hypothesis="test", source=source, diff_artifact_hash=None)
                 for i, source in enumerate(("manual", "coding_task"))]
    assert proposals[0].proposal_id != proposals[1].proposal_id
    assert proposals[0].package_hash == proposals[1].package_hash
    (root / "main.py").write_text("changed")
    assert read_candidate(package.package_hash, artifacts=artifacts) == package
    original = next(f for f in package.files if f.path == "main.py")
    assert artifacts.read_bytes(original.content_hash) == b"main.py"
    assert seal_candidate(root, request, artifacts=artifacts).package_hash != package.package_hash


@pytest.mark.parametrize("path", ["../outside.py", "/tmp/outside.py", "./main.py", "dir/../main.py", "dir//main.py"])
def test_candidate_rejects_paths_outside_manifest(candidate, path):
    root, request, artifacts = candidate
    request["source_paths"] = [path]
    request["entrypoint"] = path
    with pytest.raises(ValueError, match="relative|normalized"):
        seal_candidate(root, request, artifacts=artifacts)


def test_candidate_rejects_symlinks_and_platform_config(candidate):
    root, request, artifacts = candidate
    request["allowed_config"]["budget"] = "unlimited"
    with pytest.raises(ValidationError):
        seal_candidate(root, request, artifacts=artifacts)
    request["allowed_config"].pop("budget")
    (root / "main.py").unlink()
    (root / "main.py").symlink_to(root / "prompt.md")
    with pytest.raises(ValueError, match="symlink"):
        seal_candidate(root, request, artifacts=artifacts)


def test_candidate_corruption_is_detected(candidate):
    root, request, artifacts = candidate
    package = seal_candidate(root, request, artifacts=artifacts)
    content_hash = package.files[0].content_hash
    (artifacts.root / content_hash[:2] / content_hash[2:]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="hash mismatch"):
        read_candidate(package.package_hash, artifacts=artifacts)


def test_invalid_draft_can_be_saved_without_becoming_runnable(tmp_path):
    path = tmp_path / "state.sqlite"
    migrate_database(path)
    with sqlite3.connect(path) as db:
        store = ExperimentStore(db)
        raw = {"name": "unfinished"}
        draft = store.create(raw, "submission-1")
        assert draft["raw_config"] == raw
        assert draft["validation_errors"]
        assert draft["execution_available"] is False
        assert store.create(raw, "submission-1") == draft
        with pytest.raises(Conflict):
            store.create({"name": "changed"}, "submission-1")


def test_testplan_rejects_duplicate_samples_and_false_seed_claims():
    sample = {"test_id": "t1", "proposal_id": "p1", "task_id": "task1", "repeat_id": "r0", "repeat_index": 0, "seed": None}
    raw = {"plan_id": "plan1", "specification_hash": "a" * 64, "tests": [sample], "seed_support": "unsupported"}
    assert TestPlan.model_validate(raw).tests[0].seed is None
    raw["tests"].append({**sample, "test_id": "t2"})
    with pytest.raises(ValueError, match="duplicate"):
        TestPlan.model_validate(raw)
    raw["tests"].pop()
    raw["seed_support"] = "supported"
    with pytest.raises(ValueError, match="seed"):
        TestPlan.model_validate(raw)


def test_calibration_is_a_derivation_and_unknown_reserves_are_not_null_or_zero():
    raw = original_case()
    model = ExperimentDraft.model_validate(raw)
    assert model.budget.environment_reserve == "unresolved"
    raw["budget"]["environment_reserve"] = None
    assert resolve(raw, calendar=calendar).status == "unresolved"
    raw["budget"].update(mode="explicit", task_cost_limit="100", environment_reserve="10", evaluation_reserve="5")
    assert resolve(raw, calendar=calendar).status == "resolved"
    raw["budget"]["environment_reserve"] = "95"
    assert resolve(raw, calendar=calendar).status == "unresolved"


def test_same_day_initial_research_and_mixed_duration_validation():
    raw = original_case()
    raw["tasks"][0]["research_start_date"] = "2026-08-03"
    assert "invalid_phase_order" in {e.code for e in resolve(raw, calendar=calendar).errors}
    raw = original_case()
    raw["tasks"][1]["role"] = "tuning"
    raw["tasks"][1]["period"]["trading_days"] = 6
    result = resolve(raw, calendar=calendar)
    assert result.status == "resolved"
    assert [len(t.trading_dates) for t in result.specification.tasks[:2]] == [5, 6]
    raw["evaluate"]["allow_mixed_durations"] = True
    assert resolve(raw, calendar=calendar).status == "resolved"


def test_every_effective_leaf_has_provenance_and_corrupt_exports_are_rejected():
    from advisor.research.experiments.resolution import _leaves, verify_specification
    sealed = resolve({"account": {"initial_cash": "200000.00"}}, preset=original_case(),
                     preset_ref="original-case@1", calendar=calendar).specification
    provenance = json.loads(sealed.provenance_json)
    for path, effective in _leaves(json.loads(sealed.effective_json)):
        assert provenance[path]["effective_value"] == effective
    assert provenance["account.initial_cash"]["raw_value"] == "200000.00"
    assert provenance["runtime.max_steps"]["raw_present"] is False
    assert verify_specification(sealed).account.initial_cash == Decimal("200000")
    corrupted = sealed.model_copy(update={"effective_json": sealed.effective_json.replace('200000', '300000')})
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_specification(corrupted)


def test_empty_draft_preserves_errors_and_preflight_stays_blocked(tmp_path):
    from advisor.research.experiments.preflight import inspect
    path = tmp_path / "state.sqlite"
    migrate_database(path)
    with sqlite3.connect(path) as db:
        draft = ExperimentStore(db).create({}, "empty-draft")
        assert draft["validation_errors"]
        report = inspect(None, draft, None)
        assert report["status"] == "blocked"
        assert report["model_calls"] == 0
