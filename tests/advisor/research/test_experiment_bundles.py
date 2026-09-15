from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from advisor.db.migrate import migrate_database
from advisor.research.artifacts import ArtifactStore
from advisor.research.experiments.contracts import original_case
from advisor.research.experiments.data.bundles import HistoricalBundles, BundleInvalid
from advisor.research.experiments.data.coverage import coverage_report, queue_quality
from advisor.research.experiments.records import ExperimentRecords
from advisor.research.experiments.repository import Conflict, ExperimentStore


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "input"
    root.mkdir()
    evidence = b"Synthetic fixture evidence; not historical market data."
    (root / "evidence.txt").write_bytes(evidence)
    rows = {name: [] for name in ("calendar", "universe", "rules", "fees", "status", "corporate_actions", "daily", "minute", "auction", "queue_snapshot", "queue")}
    def add(dataset, row_id, payload, *, event="2026-08-03T00:00:00+08:00", published="2026-07-01T00:00:00+08:00", security="SH600000", day="2026-08-03"):
        rows[dataset].append({"row_id": row_id, "security": security, "trade_date": day,
                              "event_at": event, "published_at": published, "version_public_at": published,
                              "collected_at": "2026-09-09T00:00:00+08:00", "payload": payload})
    for day in (1, 2, 3):
        add("calendar", f"calendar-{day}", {"is_open": day == 3}, event=f"2026-08-{day:02}T00:00:00+08:00", security=None, day=f"2026-08-{day:02}")
    add("universe", "membership", {"exchange": "SH", "board": "main", "security_type": "a_share", "state": "normal", "market_rule_ref": "sh-rule@1", "fee_ref": "sh-fees@1"})
    rule = {"exchange": "SH", "board": "main", "state": "normal", "effective_from": "2026-07-06", "effective_to": "2026-08-31", "official_source_refs": ["fixture-proof"],
            "tick": "0.01", "buy_minimum": 100, "buy_increment": 100, "maximum_order_quantity": 1000000, "fill_increment": 1,
            "price_limit_fraction": "0.1", "price_cage_fraction": "0.02", "price_cage_ticks": 10,
            "bought_shares_sell_lag": 1, "sold_cash_reuse_lag": 0, "sold_cash_withdraw_lag": 1,
            "continuous": [{"start": "09:30:00", "end": "09:32:00"}],
            "opening_auction": {"start": "09:15:00", "end": "09:25:00"}, "closing_auction": {"start": "14:57:00", "end": "15:00:00"},
            "accept_orders": [{"start": "09:15:00", "end": "15:00:00"}],
            "cancel_forbidden": [{"start": "09:20:00", "end": "09:25:00"}], "minute_seconds": 60}
    add("rules", "sh-rule@1", rule, security=None, day=None)
    fee = {"exchange": "SH", "effective_from": "2023-08-28", "effective_to": "2026-08-31", "official_source_refs": ["fixture-proof"],
           "items": [{"fee_id": name, "side": side, "basis": "turnover", "rate": rate, "minimum": "0", "quantum": "0.01", "rounding": "ROUND_HALF_UP", "included_in_commission": False}
                     for name, side, rate in (("stamp_duty", "sell", "0.0005"), ("transfer_fee", "both", "0.00001"))]}
    add("fees", "sh-fees@1", fee, security=None, day=None)
    add("status", "status", {"trading": "active", "queue_ranges": [{"session": session, "start_sequence": 1, "end_sequence": 0, "complete": True, "start_at": "2026-08-03T" + start + "+08:00", "end_at": "2026-08-03T" + end + "+08:00"} for session, start, end in (("open", "09:15:00", "09:25:00"), ("continuous", "09:30:00", "09:32:00"), ("close", "14:57:00", "15:00:00"))],
                             "corporate_actions_complete": True, "suspension_evidence_ref": None})
    add("corporate_actions", "actions", {"actions": []})
    price = {"open": "10", "high": "10", "low": "10", "close": "10", "volume": 0, "volume_unit": "share", "adjustment": "raw"}
    add("daily", "daily", {**price, "interval_start": "2026-08-03T09:30:00+08:00", "interval_end": "2026-08-03T15:00:00+08:00"}, event="2026-08-03T15:00:00+08:00", published="2026-08-03T15:00:00+08:00")
    for minute in (30, 31):
        start, end = f"2026-08-03T09:{minute}:00+08:00", f"2026-08-03T09:{minute+1}:00+08:00"
        add("minute", f"minute-{minute}", {**price, "interval_start": start, "interval_end": end}, event=end, published=end)
    for session, clock in (("open", "09:25:00"), ("close", "15:00:00")):
        instant = f"2026-08-03T{clock}+08:00"
        add("auction", session, {"session": session, "price": None, "volume": 0, "volume_unit": "share"}, event=instant, published=instant)
    for session, clock in (("open", "09:15:00"), ("continuous", "09:30:00"), ("close", "14:57:00")):
        instant = f"2026-08-03T{clock}+08:00"
        add("queue_snapshot", f"snapshot-{session}", {"session": session, "last_sequence": 0, "complete": True, "orders": []}, event=instant, published=instant)
    manifest = {"schema_version": 1, "adapter": "local_historical_bundle_v1", "bundle_ref": "august-2026-historical@1", "origin": "fixture", "calendar_ref": "sh-sz-2026@1",
                "coverage_start": "2026-08-01", "coverage_end": "2026-08-03", "market_timezone": "Asia/Shanghai",
                "sources": [{"source_id": "fixture-proof", "path": "evidence.txt", "publisher": "explicit synthetic fixture", "url": "https://example.com/fixture", "content_hash": hashlib.sha256(evidence).hexdigest(),
                             "published_at": "2026-07-01T00:00:00+08:00", "retrieved_at": "2026-09-09T00:00:00+08:00", "semantics": "historical_publication"}],
                "coverage": [{"trade_date": "2026-08-03", "securities": ["SH600000"], "universe_proof_ref": "fixture-proof"}], "files": []}
    def write():
        manifest["files"] = []
        for dataset, values in rows.items():
            payload = b"".join((json.dumps(row, ensure_ascii=False) + "\n").encode() for row in values)
            (root / f"{dataset}.jsonl").write_bytes(payload)
            manifest["files"].append({"path": f"{dataset}.jsonl", "dataset": dataset, "sha256": hashlib.sha256(payload).hexdigest(), "row_count": len(values),
                                      "timestamp_timezone": "Asia/Shanghai", "time_semantics": "historical_publication", "proof_refs": ["fixture-proof"], "publication_latency_ms": 0})
        (root / "manifest.json").write_text(json.dumps(manifest))
    write()
    database = tmp_path / "state.sqlite"
    migrate_database(database)
    connection = sqlite3.connect(database)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    records = ExperimentRecords(connection, artifacts=artifacts)
    spec = original_case()
    spec["tasks"] = [spec["tasks"][0]]
    spec["tasks"][0]["period"]["trading_days"] = 1
    experiment = ExperimentStore(connection).create(spec, "exp")["experiment_id"]
    yield HistoricalBundles(records), experiment, root, spec, rows, manifest, write
    artifacts.close()
    connection.close()


def imported(fixture):
    bundles, experiment, root, *_ = fixture
    return bundles.import_bundle(experiment, root, submission_identity="import-1")


def test_complete_fixture_import_and_report_never_claim_real_market_readiness(bundle):
    bundles, _, root, spec, *_ = bundle
    first = imported(bundle)
    assert imported(bundle) == first
    report = coverage_report(bundles, first["record_id"], spec)
    assert report["coverage_status"] == "passed", report
    assert report["origin"] == "fixture"
    assert not report["formal_ready"]
    assert report["cells"][0]["scenarios"] == {"close": "passed", "continuous": "passed", "open": "passed"}
    assert report["return"] is None and report["model_calls"] == 0
    (root / "minute.jsonl").write_text("host file changed")
    assert len(list(bundles.rows(first["record_id"], "minute"))) == 2


@pytest.mark.parametrize("mutate,code", [
    (lambda rows, manifest: rows["calendar"].pop(0), "calendar_coverage_missing"),
    (lambda rows, manifest: rows["minute"].pop(), "continuous_minute_coverage_missing"),
    (lambda rows, manifest: rows["rules"].clear(), "market_rule_missing"),
    (lambda rows, manifest: rows["fees"].clear(), "fee_schedule_missing"),
    (lambda rows, manifest: rows["queue_snapshot"].clear(), "queue_snapshot_or_completeness_missing"),
    (lambda rows, manifest: rows["corporate_actions"].clear(), "corporate_action_coverage_missing"),
    (lambda rows, manifest: manifest["coverage"][0]["securities"].append("SZ000001"), "historical_universe_coverage_mismatch"),
    (lambda rows, manifest: rows["auction"][0].update(version_public_at="2026-08-03T09:25:06+08:00"), "open_auction_late_or_wrong_event_time"),
    (lambda rows, manifest: rows["daily"][0].update(version_public_at="2026-09-08T00:00:00+08:00"), "daily_late_or_unproved_version"),
])
def test_coverage_failures_remain_explicit_without_shortening_task(bundle, mutate, code):
    bundles, _, _, spec, rows, manifest, write = bundle
    mutate(rows, manifest)
    write()
    record = imported(bundle)
    report = coverage_report(bundles, record["record_id"], spec)
    assert report["coverage_status"] == "blocked"
    assert code in json.dumps(report, ensure_ascii=False)
    assert report["requested_tasks"][0]["period"]["trading_days"] == 1


@pytest.mark.parametrize("change", ["adjusted", "wrong_timezone", "negative_volume", "duplicate", "wrong_interval", "unknown_fee"])
def test_invalid_rows_roll_back_index_and_keep_failure_record(bundle, change):
    bundles, _, _, _, rows, _, write = bundle
    if change == "adjusted": rows["daily"][0]["payload"]["adjustment"] = "qfq"
    if change == "wrong_timezone": rows["minute"][0]["event_at"] = "2026-08-03T09:31:00+00:00"
    if change == "negative_volume": rows["minute"][0]["payload"]["volume"] = -1
    if change == "duplicate": rows["minute"].append({**rows["minute"][0], "row_id": "another-id"})
    if change == "wrong_interval": rows["minute"][0]["payload"]["interval_start"] = "2026-08-03T09:30:30+08:00"
    if change == "unknown_fee": rows["fees"][0]["payload"]["items"].pop()
    write()
    with pytest.raises(BundleInvalid): imported(bundle)
    assert bundles.records.db.execute("SELECT COUNT(*) FROM lagent_bundle_rows").fetchone()[0] == 0
    assert bundles.records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='data_bundle'").fetchone()[0] == 0
    assert bundles.records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='bundle_import'").fetchone()[0] == 1


def test_hash_conflicts_and_same_bundle_identity_changes_are_rejected(bundle):
    bundles, experiment, root, _, rows, _, write = bundle
    (root / "daily.jsonl").write_text("tampered")
    with pytest.raises(BundleInvalid) as exc: imported(bundle)
    assert exc.value.issues[0]["code"] == "bundle_file_missing_or_corrupt"
    write()
    first = imported(bundle)
    rows["daily"][0]["payload"]["high"] = "11"
    write()
    with pytest.raises(Conflict): imported(bundle)
    with pytest.raises(Conflict): bundles.import_bundle(experiment, root, submission_identity="different-key")
    assert bundles.read(first["record_id"])[0] == first


def test_observation_time_is_not_backfilled_from_trade_date(bundle):
    bundles, _, root, spec, _, manifest, _ = bundle
    file = next(file for file in manifest["files"] if file["dataset"] == "daily")
    file["time_semantics"] = "observed_only"
    file["proof_refs"] = []
    (root / "manifest.json").write_text(json.dumps(manifest))
    record = imported(bundle)
    row = next(bundles.rows(record["record_id"], "daily"))
    assert row["available_at"] == datetime.fromisoformat("2026-09-09T00:00:00+08:00")
    assert row["time_quality"] == "observed_only"
    assert coverage_report(bundles, record["record_id"], spec)["coverage_status"] == "blocked"


def test_import_interruption_and_retry_does_not_duplicate_rows(bundle):
    bundles, *_ = bundle
    def fail(point):
        if point == "after_bundle_row": raise KeyboardInterrupt("import interrupted")
    bundles.records.fault = fail
    with pytest.raises(KeyboardInterrupt): imported(bundle)
    assert bundles.records.db.execute("SELECT COUNT(*) FROM lagent_bundle_rows").fetchone()[0] == 0
    bundles.records.fault = lambda _: None
    first = imported(bundle)
    before = bundles.records.db.execute("SELECT COUNT(*) FROM lagent_bundle_rows").fetchone()[0]
    assert imported(bundle) == first
    assert bundles.records.db.execute("SELECT COUNT(*) FROM lagent_bundle_rows").fetchone()[0] == before


def test_daily_only_and_changed_real_label_never_pass_formal_preflight(bundle):
    bundles, _, _, spec, rows, manifest, write = bundle
    for dataset in ("minute", "auction", "queue", "queue_snapshot"):
        rows[dataset].clear()
    manifest["origin"] = "real"
    write()
    record = imported(bundle)
    report = coverage_report(bundles, record["record_id"], spec)
    assert report["coverage_status"] == "blocked"
    assert report["source_acceptance"] == "pending_independent_review"
    assert not report["formal_ready"]


def test_queue_reconstruction_checks_counterparties_sequence_and_time_interval(bundle):
    bundles, _, _, spec, rows, _, write = bundle
    initial = rows["queue_snapshot"][0]["payload"]
    initial["orders"] = [{"order_id": "b", "side": "buy", "price": "10", "quantity": 100, "priority": 1},
                         {"order_id": "s", "side": "sell", "price": "10", "quantity": 100, "priority": 2}]
    initial["last_sequence"] = 2
    span = rows["status"][0]["payload"]["queue_ranges"][0]
    span.update(start_sequence=3, end_sequence=3)
    rows["queue"] = [{**rows["auction"][0], "row_id": "match", "payload": {"session": "open", "event_type": "match", "sequence": 3,
                                                                            "order_id": "b", "contra_order_id": "s", "side": "buy", "price": "10", "quantity": 100}}]
    rows["auction"][0]["payload"].update(price="10", volume=100)
    write()
    record = imported(bundle)
    assert coverage_report(bundles, record["record_id"], spec)["coverage_status"] == "passed"
    snapshots = list(bundles.rows(record["record_id"], "queue_snapshot"))
    events = list(bundles.rows(record["record_id"], "queue"))
    assert queue_quality(snapshots, events, span) == []
    assert "queue_event_sequence_gap" in queue_quality(snapshots, events, {**span, "end_sequence": 4})
    events[0]["row"].payload["quantity"] = 101
    assert "queue_reconstruction_failed" in queue_quality(snapshots, events, span)


def test_shortened_queue_capture_is_not_execution_coverage(bundle):
    bundles, _, _, spec, rows, _, write = bundle
    rows["status"][0]["payload"]["queue_ranges"][0]["end_at"] = "2026-08-03T09:24:00+08:00"
    write()
    result = coverage_report(bundles, imported(bundle)["record_id"], spec)
    assert "queue_time_coverage_missing" in json.dumps(result)
    assert result["coverage_status"] == "blocked"


@pytest.mark.parametrize("target", ["manifest.json", "daily.jsonl"])
def test_duplicate_json_fields_are_rejected_and_failure_is_retained(bundle, target):
    bundles, _, root, _, _, manifest, _ = bundle
    path = root / target
    value = path.read_text()
    key = "origin" if target == "manifest.json" else "row_id"
    path.write_text(value.replace("{", '{"' + key + '":"ambiguous",', 1))
    if target != "manifest.json":
        next(file for file in manifest["files"] if file["path"] == target)["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(BundleInvalid): imported(bundle)
    assert bundles.records.db.execute("SELECT COUNT(*) FROM lagent_bundle_rows").fetchone()[0] == 0
    assert bundles.records.db.execute("SELECT COUNT(*) FROM lagent_records WHERE kind='bundle_import'").fetchone()[0] == 1


def test_coverage_export_is_immutable_auditable_and_not_execution_authorization(bundle):
    bundles, _, _, spec, *_ = bundle
    record = imported(bundle)
    result = bundles.preflight(record["record_id"], spec, submission_identity="coverage-1")
    assert result == bundles.preflight(record["record_id"], spec, submission_identity="coverage-1")
    report = json.loads(bundles.artifacts.read_bytes(result["value"]["report_hash"]))
    assert report["coverage_status"] == "passed" and not report["formal_ready"]
    assert result["value"]["status"] == "blocked"
    spec["tasks"][0]["period"]["trading_days"] = 2
    with pytest.raises(Conflict):
        bundles.preflight(record["record_id"], spec, submission_identity="coverage-1")


def test_index_time_tampering_fails_closed_against_sealed_source_semantics(bundle):
    bundles, *_ = bundle
    record = imported(bundle)
    db = bundles.records.db
    db.execute("DROP TRIGGER lagent_bundle_rows_no_update")
    db.execute("UPDATE lagent_bundle_rows SET available_at='2000-01-01T00:00:00+00:00' WHERE dataset='daily'")
    db.commit()
    with pytest.raises(Conflict, match="time metadata"):
        list(bundles.rows(record["record_id"], "daily"))
