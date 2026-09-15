"""Paged proposal lineage and immutable observations, without hidden trace detail.

Record and event ceilings travel together so later pages cannot combine an old
candidate set with new Tests, scores or baseline revisions. Original bytes are
verified by the existing candidate/download views, not required to list history.
"""
import base64
from collections import Counter
import json

from .contracts import CandidateProposal
from .queries import ExperimentQueries
from .records import TERMINAL, TRANSITIONS
from .registration import record_identity
from .repository import Conflict, Missing, encode


STATES = frozenset(TRANSITIONS) | TERMINAL
REASONS = frozenset({"selection_frozen", "selection_revision_changed", "comparison_has_no_selection_binding",
    "runtime_acceptance_incomplete", "selection_thresholds_satisfied", "required_samples_invalid",
    "role_excluded_from_selection", "mixed_durations_require_explicit_weights", "eligible", "not_improved", "unstable"})
DECISIONS = frozenset({"promoted", "selection_frozen", "stale_baseline", "inconclusive", "tuning_only", "not_improved", "unstable"})


def identity(record):
    return {key: record[key] for key in ("record_id", "content_hash", "sequence", "created_at")}


class EvolutionTree:
    def __init__(self, records):
        self.records, self.db = records, records.db
        self.queries = ExperimentQueries(records, viewer="owner")
        self._definitions = {}

    def _candidate(self, experiment, record_id, ceiling):
        record = self.queries._candidate_proposal(experiment, record_id)
        if record["sequence"] > ceiling:
            raise Missing("proposal is outside this tree snapshot")
        proposal = CandidateProposal.model_validate(record["value"]["proposal"])
        if record_identity(experiment, "candidate", proposal.proposal_id) != record_id:
            raise Conflict("proposal identity differs from its local name")
        links = self.records.related(record_id)
        parent = record_identity(experiment, "candidate", proposal.parent_proposal_id) if proposal.parent_proposal_id else None
        expected = [{"relation": "package", "target_id": record_identity(experiment, "package", proposal.package_hash)}]
        if parent:
            ancestor = self.queries._candidate_proposal(experiment, parent)
            if ancestor["sequence"] >= record["sequence"]:
                raise Conflict("proposal parent must precede its child")
            expected.append({"relation": "parent", "target_id": parent})
        if links != expected:
            raise Conflict("proposal lineage links differ from its declared parent")
        return record, proposal, parent

    def _bounds(self, experiment, view, proposal, cursor):
        records = self.db.execute("SELECT COALESCE(MAX(record_sequence),0) FROM lagent_records").fetchone()[0]
        events = self.db.execute("SELECT COALESCE(MAX(event_sequence),0) FROM lagent_events").fetchone()[0]
        bound = {"version": 1, "experiment_id": experiment, "view": view, "proposal_id": proposal,
                 "after": 0, "records_through": records, "events_through": events}
        if cursor is None:
            return bound
        try:
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise ValueError
            decoded = json.loads(base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True))
            if not isinstance(decoded, dict) or set(decoded) != set(bound):
                raise ValueError
            if any(decoded[key] != bound[key] for key in ("version", "experiment_id", "view", "proposal_id")):
                raise ValueError
            if any(type(decoded[key]) is not int for key in ("version", "after", "records_through", "events_through")):
                raise ValueError
            if not (0 <= decoded["after"] <= decoded["records_through"] <= records and 0 <= decoded["events_through"] <= events):
                raise ValueError
            return decoded
        except (ValueError, TypeError, UnicodeError, RecursionError) as error:
            raise ValueError("invalid or mismatched evolution cursor") from error

    def _status(self, test_id, events_through):
        row = self.db.execute("SELECT MAX(e.event_sequence) FROM lagent_events e, json_each(e.value_json,'$.updates') u "
            "WHERE e.test_id=? AND e.event_sequence<=? AND json_extract(u.value,'$.name')='status'", (test_id, events_through)).fetchone()
        if row[0] is None:
            return "created"
        event = self.records.event(row[0])
        updates = [u["value"] for u in event["value"]["updates"] if u["name"] == "status"]
        if event["test_id"] != test_id or len(updates) != 1 or updates[0].get("status") not in STATES:
            raise Conflict("invalid original Test status event")
        return updates[0]["status"]

    def _test(self, experiment, record, candidate, bound):
        value = record["value"]
        if (record["kind"] != "test" or record["experiment_id"] != experiment or value.get("registration_version") != 1
                or record_identity(experiment, "candidate", value["sample"]["proposal_id"]) != candidate
                or {"relation": "candidate", "target_id": candidate} not in self.records.related(record["record_id"])):
            raise Conflict("Test is not bound to its original proposal")
        definition_id = value["definition_id"]
        if definition_id not in self._definitions:
            definition = self.records.read(definition_id)
            if definition["experiment_id"] != experiment or definition["kind"] != "definition":
                raise Conflict("Test definition scope differs from its experiment")
            self._definitions[definition_id] = {scope["task_id"]: scope for scope in self.queries.scopes(definition_id)}
        scope = self._definitions[definition_id].get(value["task_id"])
        if scope is None or scope["role"] != value["role"]:
            raise Conflict("Test task scope differs from its definition")
        return {**identity(record), "plan_id": value["plan_id"], "definition_id": value["definition_id"],
                "proposal_id": value["sample"]["proposal_id"], "task_id": value["task_id"], "role": value["role"],
                "purpose": value["purpose"], "repeat_id": value["sample"]["repeat_id"], "repeat_index": value["sample"]["repeat_index"],
                "rerun_of": value.get("rerun_of"), "eligible_for_original_comparison": not bool(value.get("rerun_of")),
                "status": self._status(record["record_id"], bound["events_through"]), "detail_hidden": value["role"] != "tuning"}

    def _selection(self, experiment, record):
        if record["experiment_id"] != experiment or record["kind"] != "selection":
            raise Conflict("selection history scope mismatch")
        value = record["value"]
        result = identity(record)
        if value.get("selection_decision_version") == 1:
            comparison = value["request"]["comparison_id"]
            self.queries.comparison_feedback(comparison)
            if value.get("decision") not in DECISIONS or value.get("reason") not in REASONS:
                raise Conflict("unknown selection decision metadata")
            result.update(comparison_id=comparison, decision=value["decision"], reason=value["reason"],
                          observed_selection_id=value["observed_selection_id"])
        elif value.get("selection_version") != 1:
            raise Conflict("selection history requires a typed original decision")
        if value.get("selection_version") == 1:
            if value.get("operation") not in {"initialize", "promote", "finalize"}:
                raise Conflict("unknown selection operation")
            result.update({key: value[key] for key in ("operation", "definition_id", "initial_baseline_id", "current_baseline_id")})
            result["previous_selection_id"] = value.get("previous_selection_id")
        else:
            result["operation"] = "retain"
        return result

    def _comparison(self, experiment, record, bound):
        value = record["value"]
        if value.get("comparison_version") != 1 or value.get("record_type") != "plan":
            raise Conflict("comparison history requires an original frozen pair")
        self.queries.require_comparison_scope(record["record_id"])
        participants = {record_identity(experiment, "candidate", value[key]) for key in ("baseline", "candidate")}
        if {link["target_id"] for link in self.records.related(record["record_id"]) if link["relation"] == "candidate"} != participants:
            raise Conflict("comparison opponents differ from its original candidate links")
        result_id = record_identity(experiment, "comparison-result", record["record_id"])
        result = self.db.execute("SELECT record_id FROM lagent_records WHERE record_id=? AND record_sequence<=?",
                                 (result_id, bound["records_through"])).fetchone()
        if result:
            completed = self.records.read(result_id)
            if (completed["experiment_id"] != experiment or completed["kind"] != "comparison"
                    or completed["value"].get("comparison_id") != record["record_id"]
                    or completed["value"].get("plan_id") != value["plan_id"]):
                raise Conflict("comparison result differs from its original pair")
        # Results are reached from the original comparison, not a second lineage
        # edge or a duplicate sample/charge. Detailed samples remain audited.
        return {**identity(record), "plan_id": value["plan_id"], "baseline": value["baseline"], "candidate": value["candidate"],
                "baseline_record_id": record_identity(experiment, "candidate", value["baseline"]),
                "candidate_record_id": record_identity(experiment, "candidate", value["candidate"]),
                "selection_id": value.get("selection_id"), "result_id": result_id if result else None,
                "feedback": self.queries.comparison_feedback(result_id) if result else None,
                "status": "completed" if result else "awaiting_result"}

    def page(self, *, experiment_id, view, limit, proposal_id=None, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 200 or view not in {"tree", "tests", "comparisons", "selection"}:
            raise ValueError("invalid evolution view or limit")
        if (view in {"tests", "comparisons"}) != (proposal_id is not None):
            raise ValueError("node views require one proposal")
        if self.db.in_transaction:
            raise Conflict("evolution reads require an independent snapshot")
        self.db.execute("BEGIN")
        try:
            bound = self._bounds(experiment_id, view, proposal_id, cursor)
            candidate_id = record_identity(experiment_id, "candidate", proposal_id) if proposal_id is not None else None
            if candidate_id:
                self._candidate(experiment_id, candidate_id, bound["records_through"])
            kind = {"tree": "candidate_proposal", "tests": "test", "comparisons": "comparison", "selection": "selection"}[view]
            where = "r.experiment_id=? AND r.kind=? AND r.record_sequence<=?"
            args = [experiment_id, kind, bound["records_through"]]
            if candidate_id:
                where += " AND EXISTS (SELECT 1 FROM lagent_record_links l WHERE l.record_id=r.record_id AND l.relation='candidate' AND l.target_id=?)"
                args.append(candidate_id)
            if view == "comparisons":
                where += " AND json_extract(r.value_json,'$.record_type')='plan'"
            total = self.db.execute("SELECT COUNT(*) FROM lagent_records r WHERE " + where, args).fetchone()[0]
            rows = self.db.execute("SELECT r.record_id,r.record_sequence FROM lagent_records r WHERE " + where +
                " AND r.record_sequence>? ORDER BY r.record_sequence LIMIT ?", (*args, bound["after"], limit + 1)).fetchall()
            selection_row = self.db.execute("SELECT record_id FROM lagent_records WHERE experiment_id=? AND kind='selection' "
                "AND json_extract(value_json,'$.selection_version')=1 AND record_sequence<=? ORDER BY record_sequence DESC LIMIT 1",
                (experiment_id, bound["records_through"])).fetchone()
            selection = self._selection(experiment_id, self.records.read(selection_row[0])) if selection_row else None
            items = []
            for row in rows[:limit]:
                record = self.records.read(row[0])
                if view == "tree":
                    record, proposal, parent = self._candidate(experiment_id, row[0], bound["records_through"])
                    counts, roles = Counter(), Counter()
                    tests = self.db.execute("SELECT r.record_id FROM lagent_records r WHERE r.experiment_id=? AND r.kind='test' "
                        "AND r.record_sequence<=? AND EXISTS (SELECT 1 FROM lagent_record_links l WHERE l.record_id=r.record_id "
                        "AND l.relation='candidate' AND l.target_id=?)", (experiment_id, bound["records_through"], row[0]))
                    for (test_id,) in tests:
                        test = self._test(experiment_id, self.records.read(test_id), row[0], bound)
                        counts[test["status"]] += 1
                        roles[test["role"]] += 1
                    items.append({**identity(record), **proposal.model_dump(mode="json"), "parent_record_id": parent,
                        "test_count": sum(counts.values()), "test_status_counts": dict(sorted(counts.items())),
                        "task_role_counts": dict(sorted(roles.items())),
                        "initial_baseline": bool(selection and selection["initial_baseline_id"] == row[0]),
                        "current_baseline": bool(selection and selection["current_baseline_id"] == row[0])})
                elif view == "tests":
                    items.append(self._test(experiment_id, record, candidate_id, bound))
                elif view == "comparisons":
                    items.append(self._comparison(experiment_id, record, bound))
                else:
                    items.append(self._selection(experiment_id, record))
            next_cursor = None
            if len(rows) > limit:
                next_cursor = base64.urlsafe_b64encode(encode({**bound, "after": rows[limit-1][1]}).encode()).decode("ascii")
            return {"items": items, "total": total, "next_cursor": next_cursor, "selection": selection,
                    "snapshot": {key: bound[key] for key in ("records_through", "events_through")}, "execution_available": False}
        finally:
            self.db.rollback()
