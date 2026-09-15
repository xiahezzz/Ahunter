"""Resolve owner configuration against original imported calendar evidence.

Successful definitions and unsuccessful resolution attempts are immutable. The
calendar cutoff belongs to each task's initial research, never to the caller.
"""
from datetime import datetime
import json
from zoneinfo import ZoneInfo

from .contracts import ExperimentDraft
from .data.bundles import HistoricalBundles
from .registration import ExperimentRegistry, record_identity
from .repository import Conflict, Missing
from .resolution import digest, encode, resolve, validation_errors


class DefinitionResolutions:
    def __init__(self, records):
        self.records = records
        self.registry = ExperimentRegistry(records)

    def register(self, experiment_id, config, *, calendar_bundle_id, submission_identity):
        if not isinstance(config, dict) or not isinstance(submission_identity, str) or not submission_identity.strip():
            raise ValueError("definition resolution requires config and submission identity")
        records = self.records
        request = json.loads(encode({"config": config, "calendar_bundle_id": calendar_bundle_id}))
        identity = record_identity(experiment_id, "definition-resolution", submission_identity)
        prior = records.db.execute("SELECT 1 FROM lagent_records WHERE record_id=?", (identity,)).fetchone()
        if prior:
            record = records.read(identity)
            if record["value"].get("request") != request:
                raise Conflict("definition resolution identity has different inputs")
            return self._result(record)
        bundle = records.read(calendar_bundle_id)
        if bundle["kind"] != "data_bundle" or bundle["experiment_id"] != experiment_id:
            raise Missing("calendar bundle does not belong to this experiment")
        bundles = HistoricalBundles(records)
        bundle, manifest = bundles.read(calendar_bundle_id)
        errors = validation_errors(request["config"])
        cutoffs, providers = {}, {}
        if not errors:
            spec = ExperimentDraft.model_validate(request["config"])
            for task in spec.tasks:
                at = datetime.combine(task.research_start_date, spec.clock.initial_research_at, ZoneInfo(spec.clock.timezone))
                key = digest(task.period)
                cutoffs[key] = min(cutoffs.get(key, at), at)

        def calendar(ref, period):
            key = digest(period)
            if key not in providers:
                try:
                    providers[key] = bundles.calendar(calendar_bundle_id, as_of=cutoffs[key])
                except FileNotFoundError as error:
                    raise ValueError("original calendar bytes are unavailable") from error
            return providers[key](ref, period)

        outcome = resolve(request["config"], calendar=calendar)
        source = {"bundle_id": calendar_bundle_id, "bundle_hash": bundle["content_hash"],
                  "manifest_hash": bundle["value"]["manifest_hash"], "origin": manifest.origin,
                  "source_acceptance": bundle["value"]["source_acceptance"],
                  "period_cutoffs": {key: at.isoformat() for key, at in sorted(cutoffs.items())}}
        definition = None
        if outcome.specification is not None:
            definition = self.registry._prepare_definition(experiment_id, outcome.specification,
                submission_identity=submission_identity, calendar_source=source)
        value = {"scope": "definition_resolution", "request": request, "calendar_source": source,
                 "status": "resolved" if definition else "blocked", "execution_available": False,
                 "errors": [error.model_dump(mode="json") for error in outcome.errors],
                 "definition_id": definition["request"]["record_id"] if definition else None}
        artifact = records.artifacts.put_json(value)
        links = (("data_bundle", calendar_bundle_id),)
        if definition:
            links += (("target", definition["request"]["record_id"]),)
        prepared = records.prepare(experiment_id=experiment_id, kind="preflight", record_id=identity,
            submission_identity="definition-resolution:" + submission_identity, value=value, links=links,
            artifact_hashes=(artifact.content_hash,))
        with records._transaction():
            # Recheck the immutable decision under the writer lock. A concurrent
            # retry must never add a definition beside an earlier blocked result.
            prior = records.db.execute("SELECT 1 FROM lagent_records WHERE record_id=?", (identity,)).fetchone()
            if prior:
                record = records.read(identity)
                if record["value"].get("request") != request:
                    raise Conflict("definition resolution identity has different inputs")
            else:
                from .selection import require_selection_open
                require_selection_open(records, experiment_id)
                if definition:
                    records._insert(definition)
                record = records._insert(prepared)
        return self._result(record)

    def _result(self, record):
        value = record["value"]
        definition = self.records.read(value["definition_id"]) if value["definition_id"] else None
        return {"resolution": record, "definition": definition, "status": value["status"],
                "errors": value["errors"], "execution_available": False}
