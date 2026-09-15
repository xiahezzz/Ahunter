"""Host-owned views: hidden detail never crosses into optimizer feedback.

The runtime does not receive this object or the generic record store. Its own
phase-bound env queries are a separate interface. API handlers choose the viewer;
viewer identity is never supplied by a candidate's tool arguments.
"""
from __future__ import annotations

from decimal import Decimal

from .contracts import CandidateProposal, ResolvedSpecification
from .candidates import read_candidate
from .registration import record_identity
from .repository import Conflict, Missing
from .resolution import digest, verify_specification
from .sources import SourceRetention


class QueryDenied(PermissionError):
    pass


class ExperimentQueries:
    def __init__(self, records, *, viewer):
        if viewer not in {"owner", "optimizer"}:
            raise ValueError("only host owner/optimizer views are supported")
        self.records = records
        self.viewer = viewer
        self.sources = SourceRetention(records)

    def _candidate_proposal(self, experiment_id, record_id):
        record = self.records.read(record_id)
        if (record["experiment_id"] != experiment_id or record["kind"] != "candidate_proposal"
                or record["value"].get("registration_version") != 1):
            raise Conflict("candidate query requires a typed proposal in this experiment")
        CandidateProposal.model_validate(record["value"]["proposal"])
        return record

    def candidates(self, *, experiment_id, limit, cursor=None):
        page = self.records.page(experiment_id=experiment_id, kind="candidate_proposal", limit=limit, cursor=cursor)
        return {"items": [self._candidate_proposal(experiment_id, item["record_id"]) for item in page["items"]],
                "next_cursor": page["next_cursor"]}

    def candidate(self, *, experiment_id, proposal_id):
        record = self._candidate_proposal(experiment_id, record_identity(experiment_id, "candidate", proposal_id))
        value = record["value"]["proposal"]
        package_id = record_identity(experiment_id, "package", value["package_hash"])
        package = self.records.read(package_id)
        links = self.records.related(record["record_id"])
        expected = [{"relation": "package", "target_id": package_id}]
        if value["parent_proposal_id"] is not None:
            parent_id = record_identity(experiment_id, "candidate", value["parent_proposal_id"])
            self._candidate_proposal(experiment_id, parent_id)
            expected.append({"relation": "parent", "target_id": parent_id})
        if (links != expected or package["experiment_id"] != experiment_id or package["kind"] != "candidate_package"
                or package["value"].get("registration_version") != 1):
            raise Conflict("candidate package or lineage binding differs from its proposal")
        try:
            sealed = read_candidate(value["package_hash"], artifacts=self.records.artifacts)
        except (ValueError, FileNotFoundError) as error:
            raise Conflict("candidate original package bytes failed integrity verification") from error
        if sealed.model_dump(mode="json") != package["value"]["package"]:
            raise Conflict("candidate package record differs from its sealed manifest")
        return {"proposal": record, "package": package, "links": links, "execution_available": False}

    def scopes(self, record_id, _seen=None):
        seen = set() if _seen is None else set(_seen)
        if record_id in seen:
            raise Conflict("cyclic experiment record scope")
        seen.add(record_id)
        record = self.records.read(record_id)
        value = record["value"]
        if record["kind"] in {"candidate_package", "candidate_proposal"} and value.get("registration_version") == 1:
            return ()
        if record["kind"] in {"test", "test_plan", "definition"} and value.get("registration_version") == 1:
            if record["kind"] == "definition":
                definition = record
            else:
                definition = self.records.read(value["definition_id"])
                if definition["kind"] != "definition" or definition["experiment_id"] != record["experiment_id"]:
                    raise Conflict("invalid record definition scope")
            sealed = ResolvedSpecification.model_validate(definition["value"]["specification"])
            spec = verify_specification(sealed)
            selected = ({value["task_id"]} if record["kind"] == "test" else
                        {sample["task_id"] for sample in value["plan"]["tests"]} if record["kind"] == "test_plan" else
                        {task.task_id for task in spec.tasks})
            if not selected <= {task.task_id for task in spec.tasks}:
                raise Conflict("record references unknown task scope")
            dates = {task.task_id: [d.isoformat() for d in task.trading_dates] for task in sealed.tasks}
            return tuple({"task_id": task.task_id, "role": task.role, "trading_dates": dates[task.task_id],
                          "specification_hash": sealed.specification_hash} for task in spec.tasks if task.task_id in selected)
        # Descendant audit/attempt/source/evaluation facts inherit their test scope.
        # Candidate and definition links are handled only by typed registrations.
        inherited = {}
        for link in self.records.related(record_id):
            if link["relation"] in {"test", "plan", "target", "source", "evaluation", "comparison", "rescore_of", "rerun_of"}:
                for scope in self.scopes(link["target_id"], seen):
                    inherited[digest(scope)] = scope
        if inherited:
            return tuple(inherited[key] for key in sorted(inherited))
        # Legacy/unscoped facts are never accidentally treated as public feedback.
        return ({"role": "unknown", "task_id": None, "trading_dates": [], "specification_hash": None},)

    def _authorize(self, record_id, *, audit_identity, action):
        scopes = self.scopes(record_id)
        hidden = any(scope["role"] != "tuning" for scope in scopes)
        if not hidden:
            return
        if self.viewer != "owner":
            raise QueryDenied("hidden experiment detail is unavailable in optimizer views")
        if not audit_identity:
            raise QueryDenied("owner hidden-detail reads require an explicit audit identity")
        target = self.records.read(record_id)
        # Exposure commits before releasing any value, trace, or artifact bytes.
        self.records.put(experiment_id=target["experiment_id"], kind="exposure",
                         record_id=record_identity(target["experiment_id"], "exposure", audit_identity),
                         submission_identity=audit_identity,
                         value={"viewer": "owner", "action": action, "target_id": record_id,
                                "target_hash": target["content_hash"], "scopes": scopes},
                         links=(("target", record_id),))

    def detail(self, record_id, *, audit_identity=None):
        self._authorize(record_id, audit_identity=audit_identity, action="record_detail")
        record = self.records.read(record_id)
        if record["kind"] == "source_document":
            return {**record, "source": self.sources.describe(record_id)}
        return record

    def events(self, test_id, *, after, limit, audit_identity=None):
        self._authorize(test_id, audit_identity=audit_identity, action=f"events:{after}:{limit}")
        return self.records.events(test_id, after=after, limit=limit)

    def artifact(self, record_id, content_hash, *, audit_identity=None):
        row = self.records.db.execute("SELECT retention FROM lagent_artifact_links WHERE record_id=? AND content_hash=?",
                                      (record_id, content_hash)).fetchone()
        if row is None:
            raise Missing("artifact is not linked to this record")
        self._authorize(record_id, audit_identity=audit_identity, action="artifact:" + content_hash)
        if row[0] == "source_policy":
            return self.sources.read_bytes(record_id)
        return self.records.artifacts.read_bytes(content_hash)

    def page(self, *, experiment_id, kind, limit, cursor=None):
        page = self.records.page(experiment_id=experiment_id, kind=kind, limit=limit, cursor=cursor)
        items = []
        for record in page["items"]:
            scopes = self.scopes(record["record_id"])
            hidden = any(scope["role"] != "tuning" for scope in scopes)
            # Listing is an overview, never a hidden record/trace download.
            item = {key: record[key] for key in ("record_id", "sequence", "kind", "created_at")}
            item.update(roles=sorted({scope["role"] for scope in scopes}), detail_hidden=hidden)
            if record["kind"] == "test":
                item["status"] = self.records.status(record["record_id"])
            items.append(item)
        return {"items": items, "next_cursor": page["next_cursor"]}

    def require_comparison_scope(self, record_id):
        record = self.records.read(record_id)
        if record["kind"] != "comparison":
            raise QueryDenied("only completed comparisons expose selection feedback")
        scopes = self.scopes(record_id)
        if not scopes or any(scope["role"] not in {"tuning", "selection_validation"} for scope in scopes):
            raise QueryDenied("final holdout or unknown scope cannot become optimizer feedback")
        return record

    def comparison_feedback(self, record_id):
        record = self.require_comparison_scope(record_id)
        value = record["value"]
        if value.get("status") != "completed":
            raise QueryDenied("comparison feedback is unavailable before completion")
        summary = value.get("public_summary", {})
        decision = summary.get("decision")
        if decision not in {"eligible", "promoted", "not_improved", "unstable", "inconclusive", "tuning_only", "stale_baseline"}:
            raise QueryDenied("comparison feedback has an invalid decision")
        # Parse an explicit numeric/status allowlist. No free-text reasons, arrays,
        # task-level scores, artifacts or tool messages can be smuggled through.
        result = {"record_id": record_id}
        for key in ("baseline_mean_return", "candidate_mean_return", "mean_improvement", "worst_paired_difference"):
            raw = summary.get(key)
            if key in summary and raw is None and decision == "inconclusive":
                result[key] = None
                continue
            if isinstance(raw, bool) or not isinstance(raw, (str, int, float, Decimal)):
                raise QueryDenied("comparison feedback has invalid numeric fields")
            try:
                number = Decimal(str(raw))
                if not number.is_finite():
                    raise ValueError
            except (ValueError, ArithmeticError) as exc:
                raise QueryDenied("comparison feedback has invalid numeric fields") from exc
            result[key] = str(number)
        for key in ("positive_repeats", "planned_repeats"):
            number = summary.get(key)
            if key == "positive_repeats" and key in summary and number is None and decision == "inconclusive":
                result[key] = None
                continue
            if type(number) is not int or number < 0:
                raise QueryDenied("comparison feedback has invalid repeat counts")
            result[key] = number
        if result["planned_repeats"] <= 0 or (result["positive_repeats"] is not None and result["positive_repeats"] > result["planned_repeats"]):
            raise QueryDenied("comparison feedback has inconsistent repeat counts")
        result["decision"] = decision
        return result

    def declare_exposure(self, experiment_id, *, submission_identity, periods, note):
        """Record owner-reported knowledge from outside this application's views."""
        from datetime import date
        if self.viewer != "owner":
            raise QueryDenied("only the owner can declare external exposure")
        if not periods or not isinstance(note, str) or not note.strip():
            raise ValueError("exposure requires date periods and an explanatory note")
        normalized = []
        for period in periods:
            if set(period) != {"start", "end"}:
                raise ValueError("exposure period requires start and end")
            start, end = date.fromisoformat(period["start"]), date.fromisoformat(period["end"])
            if end < start:
                raise ValueError("invalid exposure period")
            normalized.append({"start": start.isoformat(), "end": end.isoformat()})
        return self.records.put(experiment_id=experiment_id, kind="exposure",
                                record_id=record_identity(experiment_id, "exposure", submission_identity),
                                submission_identity=submission_identity,
                                value={"viewer": "owner", "action": "owner_declared", "periods": normalized, "note": note})

    def export(self, record_id, *, audit_identity=None):
        self._authorize(record_id, audit_identity=audit_identity, action="record_export")
        record = self.records.read(record_id)
        manifest = []
        for content_hash, retention, size in self.records.db.execute(
                "SELECT l.content_hash, l.retention, a.byte_size FROM lagent_artifact_links l "
                "JOIN research_artifacts a USING(content_hash) WHERE l.record_id=? ORDER BY l.content_hash", (record_id,)):
            if retention == "source_policy":
                availability = self.sources.describe(record_id)["availability"]
            else:
                availability = "available" if self.records.artifacts.verify(content_hash) else "integrity_failure"
            manifest.append({"content_hash": content_hash, "byte_size": size, "retention": retention, "availability": availability})
        state = ("integrity_failure" if any(item["availability"] == "integrity_failure" for item in manifest) else
                 "metadata_and_hash_only" if any(item["availability"] != "available" for item in manifest) else "exact_bytes")
        return {"schema_version": 1, "record": record, "links": self.records.related(record_id),
                "artifacts": manifest, "record_replay": state}
