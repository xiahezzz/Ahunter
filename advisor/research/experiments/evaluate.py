"""Exact selection arithmetic and immutable, source-backed Episode assessments.

The arithmetic reducer accepts host-validated outcomes; it never grants validity
or changes a baseline. Assessments below derive evidence from existing records.
Current fixture replay cannot supply formal acceptance or complete resource costs.
"""
from decimal import Decimal, localcontext
from fractions import Fraction
from itertools import product
import json
from typing import Literal

from .budget import CostUnavailable
from .candidates import read_candidate
from .clock import phase_schedule
from .contracts import Contract, Hash, Name, ResolvedSpecification, TestPlan
from .costs import effective_budget
from .episode import ReplayProgram
from .records import TERMINAL
from .registration import record_identity
from .repository import Conflict
from .resolution import digest, verify_specification


class TaskComparisonConditions(Contract):
    """Pinned conditions; actual acceptance is attested separately by the host assessor."""
    version: Literal[1]
    data_corpus_hash: Hash
    data_corpus_generation: Name
    search_policy_hash: Hash
    actual_model_id: Name
    actual_reasoning_effort: Name
    executor_hash: Hash
    fee_schedule_hash: Hash
    price_table_hash: Hash
    budget_hash: Hash
    scoring_version: Literal["mean_terminal_net_return@1"]


def _runtime_conditions(records, definition, plan, supplied):
    tasks = {s["task_id"] for s in plan["tests"]}
    if not isinstance(supplied, dict) or set(supplied) != tasks:
        raise Conflict("runtime conditions must cover the exact predeclared tasks")
    conditions, artifacts = {}, set()
    for task_id, raw in supplied.items():
        item = TaskComparisonConditions.model_validate(raw).model_dump(mode="json")
        for name, content_hash in item.items():
            if name.endswith("_hash"):
                records.artifacts.read_bytes(content_hash)
                artifacts.add(content_hash)
        conditions[task_id] = item
    value = {"version": 1, "specification_hash": definition["value"]["specification"]["specification_hash"],
             "sample_plan_hash": digest(plan), "tasks": conditions}
    return value, artifacts


def assessment_matches_conditions(assessment, planned, specification_hash, conditions, package_hash):
    """Host acceptance fields shared by comparison and baseline-reuse consumers."""
    value = assessment["value"]
    nav = value.get("nav_diagnostic")
    return (value.get("formal_ready") is True and value.get("valid") is True and value.get("reasons") == []
            and nav is not None and nav.get("formal_score") is True and value.get("sample") == planned
            and value.get("specification_hash") == specification_hash
            and value.get("runtime_conditions_hash") == digest(conditions) and value.get("package_hash") == package_hash)


def qualified_comparison(records, frozen, samples):
    """Recompute from the exact sealed sample records, never trust summary flags.

    Formal assessments are a host evidence boundary. Current fixture assessment
    producers cannot attest valid/formal NAVs or actual runtime condition hashes.
    """
    value = frozen["value"]
    plan, definition = records.read(value["plan_id"]), records.read(value["definition_id"])
    if (plan["content_hash"] != value["plan_hash"] or plan["experiment_id"] != frozen["experiment_id"]
            or plan["value"]["definition_id"] != definition["record_id"]):
        raise Conflict("comparison plan binding mismatch")
    runtime = value.get("runtime_conditions")
    reused = plan["value"].get("reuse", {}).get("sources", {})
    if reused:
        from .reuse import validate_reused_plan
        validate_reused_plan(records, plan, value.get("selection_id"), runtime)
    expected = value["test_ids"]
    if [item["test_id"] for item in samples] != expected or len(samples) != len(plan["value"]["plan"]["tests"]):
        raise Conflict("comparison result changed its predeclared samples")
    qualified = runtime is not None and value.get("selection_id") is not None
    if runtime is not None:
        checked, _ = _runtime_conditions(records, definition, plan["value"]["plan"], runtime["tasks"])
        if checked != runtime or digest({"runtime": runtime, "packages": value["packages"]}) != value["runtime_fingerprint"]:
            raise Conflict("comparison runtime fingerprint mismatch")
    outcomes = {}
    for item, planned in zip(samples, plan["value"]["plan"]["tests"]):
        test_id = record_identity(frozen["experiment_id"], "test", planned["test_id"])
        test = records._test(test_id)
        if (item["test_id"] != test_id or test["value"].get("sample") != planned
                or test["value"].get("plan_id") != (reused[test_id]["source_plan_id"] if test_id in reused else plan["record_id"])
                or test["value"].get("rerun_of")):
            raise Conflict("comparison Test binding mismatch")
        if item["assessment_id"] is None:
            qualified = False
            continue
        assessment = records.read(item["assessment_id"])
        a = assessment["value"]
        if (assessment["kind"] != "evaluation" or assessment["content_hash"] != item["assessment_hash"]
                or assessment["experiment_id"] != frozen["experiment_id"] or a.get("evaluation_version") != 1
                or a.get("request", {}).get("test_id") != test_id
                or {"relation": "test", "target_id": test_id} not in records.related(assessment["record_id"])):
            raise Conflict("comparison assessment binding mismatch")
        first = records.db.execute("SELECT record_id FROM lagent_records WHERE kind='evaluation' "
            "AND json_extract(value_json,'$.evaluation_version')=1 AND json_extract(value_json,'$.request.test_id')=? "
            "ORDER BY record_sequence LIMIT 1", (test_id,)).fetchone()
        if first[0] != assessment["record_id"]:
            raise Conflict("comparison cannot substitute a later assessment")
        nav = a.get("nav_diagnostic")
        outcomes[planned["test_id"]] = {"status": item["status"], "valid": a.get("valid") is True,
            "initial_nav": nav["initial_nav"] if nav else None, "terminal_nav": nav["terminal_nav"] if nav else None}
        if (runtime is None or not assessment_matches_conditions(assessment, planned, runtime["specification_hash"],
                    runtime["tasks"][planned["task_id"]], value["packages"][planned["proposal_id"]])
                or item["status"] != "completed" or records.status(test_id) != "completed"):
            qualified = False
    result = compare_returns(definition["value"]["specification"], plan["value"]["plan"], outcomes,
                             baseline=value["baseline"], candidate=value["candidate"])
    if result["decision"] == "eligible" and not qualified:
        result.update(decision="inconclusive", reason="runtime_acceptance_incomplete")
    return result, qualified


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("evaluation numbers must be finite decimal strings or integers")
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError("evaluation numbers must be finite")
    return Fraction(number)


def _render(value):
    # This is display only. Decisions use the rational value, never this rounding.
    with localcontext() as context:
        context.prec = 50
        return str(Decimal(value.numerator) / Decimal(value.denominator))


def _exact(value):
    return {"numerator": str(value.numerator), "denominator": str(value.denominator)}


def terminal_return(initial_nav, terminal_nav):
    initial, terminal = _number(initial_nav), _number(terminal_nav)
    if initial <= 0:
        raise ValueError("initial NAV must be positive")
    return terminal / initial - 1


def compare_returns(specification, plan, outcomes, *, baseline, candidate):
    """Reduce one complete predeclared pair; this function does not promote.

    outcomes is keyed by the plan's local test IDs. Each value contains status,
    valid, initial_nav and terminal_nav; NAV fields are read only for valid,
    completed samples. Missing/invalid samples suppress all aggregate scores.
    """
    sealed = ResolvedSpecification.model_validate(specification)
    spec = verify_specification(sealed)
    if spec.evaluate.stability_policy_ref != "paired_repeat_threshold_v1":
        raise Conflict("unsupported evaluation stability policy")
    plan = TestPlan.model_validate(plan)
    if plan.specification_hash != sealed.specification_hash:
        raise Conflict("comparison plan specification mismatch")
    if baseline == candidate or {s.proposal_id for s in plan.tests} != {baseline, candidate}:
        raise Conflict("comparison requires exactly its baseline and candidate")
    tasks = {s.task_id for s in plan.tests}
    task_map = {t.task_id: t for t in spec.tasks}
    if not tasks <= task_map.keys():
        raise Conflict("comparison plan has unknown tasks")
    roles = {task_map[t].role for t in tasks}
    if len(roles) != 1:
        raise Conflict("comparison cannot mix task roles")
    role = next(iter(roles))
    if role == "selection_validation" and tasks != {t.task_id for t in spec.tasks if t.role == role}:
        raise Conflict("comparison omits a predeclared selection task")
    matrix = {(s.proposal_id, s.task_id, s.repeat_index) for s in plan.tests}
    if matrix != set(product((baseline, candidate), tasks, range(spec.evaluate.repeats))):
        raise Conflict("comparison requires the complete predeclared repeat matrix")
    for offset in range(0, len(plan.tests), 2):
        block = plan.tests[offset:offset + 2]
        if (len({(s.task_id, s.repeat_index) for s in block}) != 1
                or {s.proposal_id for s in block} != {baseline, candidate}):
            raise Conflict("comparison plan must interleave paired runs")
    if set(outcomes) - {s.test_id for s in plan.tests}:
        raise Conflict("unplanned samples cannot replace original comparison samples")
    invalid = []
    values = {}
    for sample in plan.tests:
        outcome = outcomes.get(sample.test_id)
        if outcome is None:
            invalid.append({"test_id": sample.test_id, "reason": "missing_sample"})
        elif outcome.get("status") != "completed":
            invalid.append({"test_id": sample.test_id, "reason": "sample_not_completed"})
        elif outcome.get("valid") is not True:
            invalid.append({"test_id": sample.test_id, "reason": "sample_invalid"})
        else:
            values[(sample.proposal_id, sample.task_id, sample.repeat_index)] = terminal_return(
                outcome["initial_nav"], outcome["terminal_nav"])
    result = {"role": role, "planned_repeats": spec.evaluate.repeats,
              "seed_support": plan.seed_support, "statistical_significance_claimed": False,
              "score": spec.evaluate.score, "annualized": False, "invalid_samples": invalid,
              "decision": "inconclusive", "groups": [], "summary": None}
    if invalid:
        return {**result, "reason": "required_samples_invalid"}
    durations = {t.task_id: len(t.trading_dates) for t in sealed.tasks}
    groups = [sorted(tasks)] if spec.evaluate.allow_mixed_durations else [
        sorted(t for t in tasks if durations[t] == count) for count in sorted({durations[t] for t in tasks})]
    summaries = []
    for group in groups:
        weights = {t: Fraction(task_map[t].weight) for t in group}
        total_weight = sum(weights.values())
        repeats = {name: [sum(weights[t] * values[name, t, index] for t in group) / total_weight
                          for index in range(spec.evaluate.repeats)] for name in (baseline, candidate)}
        differences = [c - b for b, c in zip(repeats[baseline], repeats[candidate])]
        means = {name: sum(repeats[name]) / spec.evaluate.repeats for name in (baseline, candidate)}
        improvement, worst = means[candidate] - means[baseline], min(differences)
        positives = sum(value > 0 for value in differences)
        threshold = spec.evaluate.positive_repeat_fraction
        if improvement <= 0 or improvement < Fraction(spec.evaluate.minimum_improvement):
            decision = "not_improved"
        elif (positives * threshold.denominator < spec.evaluate.repeats * threshold.numerator
              or worst < Fraction(spec.evaluate.worst_repeat_regression)):
            decision = "unstable"
        else:
            decision = "eligible"
        numbers = {"baseline_mean_return": means[baseline], "candidate_mean_return": means[candidate],
                   "mean_improvement": improvement, "worst_paired_difference": worst}
        summary = {**{k: _render(v) for k, v in numbers.items()}, "exact": {k: _exact(v) for k, v in numbers.items()},
                   "positive_repeats": positives, "planned_repeats": spec.evaluate.repeats, "decision": decision,
                   "paired_differences": [_render(v) for v in differences],
                   "paired_differences_exact": [_exact(v) for v in differences]}
        summaries.append(summary)
        result["groups"].append({"task_ids": group, "trading_day_counts": sorted({durations[t] for t in group}),
                                 "weights": {t: str(task_map[t].weight) for t in group}, **summary})
    if role != "selection_validation":
        return {**result, "decision": "tuning_only" if role == "tuning" else "holdout_only",
                "reason": "role_excluded_from_selection", "summary": summaries[0] if len(groups) == 1 else None}
    if len(groups) != 1:
        return {**result, "reason": "mixed_durations_require_explicit_weights"}
    return {**result, "decision": summaries[0]["decision"], "reason": summaries[0]["decision"], "summary": summaries[0]}


class EpisodeAssessments:
    """Snapshot validity and NAV evidence without mutating the Test or its costs."""
    def __init__(self, records):
        self.records = records

    def assess(self, test_id, *, submission_identity, rescore_of=None):
        if not isinstance(submission_identity, str) or not submission_identity.strip():
            raise ValueError("assessment submission identity required")
        records = self.records
        test = records._test(test_id)
        identity = record_identity(test["experiment_id"], "assessment", [test_id, submission_identity])
        request = {"test_id": test_id, "rescore_of": rescore_of}
        with records._transaction():
            if records.db.execute("SELECT 1 FROM lagent_records WHERE record_id=?", (identity,)).fetchone():
                previous = records.read(identity)
                if previous["value"]["request"] != request:
                    raise Conflict("assessment identity already has different inputs")
                return previous
            if test["value"].get("registration_version") != 1:
                raise Conflict("assessment requires a typed Test registration")
            prior = records.db.execute("SELECT record_id FROM lagent_records WHERE kind='evaluation' "
                "AND json_extract(value_json,'$.evaluation_version')=1 AND json_extract(value_json,'$.request.test_id')=?",
                (test_id,)).fetchall()
            if prior and rescore_of is None:
                raise Conflict("a new assessment must link its original assessment")
            links = [("test", test_id)]
            if rescore_of is not None:
                original = records.read(rescore_of)
                if (original["kind"] != "evaluation" or original["experiment_id"] != test["experiment_id"]
                        or original["value"].get("evaluation_version") != 1
                        or original["value"]["request"]["test_id"] != test_id):
                    raise Conflict("rescore must reference an assessment of the same Test")
                links.append(("rescore_of", rescore_of))
            status = records.status(test_id)
            if status not in TERMINAL | {"evaluating"}:
                raise Conflict("assessment requires stopped execution or evaluation readiness")
            definition = records.read(test["value"]["definition_id"])
            if definition["kind"] != "definition" or definition["experiment_id"] != test["experiment_id"]:
                raise Conflict("assessment definition binding mismatch")
            sealed = ResolvedSpecification.model_validate(definition["value"]["specification"])
            verify_specification(sealed)
            task = next(t for t in sealed.tasks if t.task_id == test["value"]["task_id"])
            reasons = []
            evidence = {}
            def projection(name):
                state = records.projection(test_id, name)
                if state:
                    evidence[name] = {"sequence": state["sequence"], "value_hash": digest(state["value"])}
                return state["value"] if state else None
            if status in {"blocked", "failed", "cancelled"}:
                reasons.append("test_" + status)
            episode, clock = projection("episode"), projection("phase_clock")
            schedule = phase_schedule(sealed, task.task_id)
            if (clock is None or clock["status"] != "finished" or clock["index"] != len(schedule)
                    or clock["schedule_hash"] != digest(schedule) or clock["blocked"] or clock["stop_required"]):
                reasons.append("phase_coverage_incomplete")
            for name in ("candidate_processes", "model_invocations"):
                calls = projection(name)
                if calls and any(not c["quiescent"] or c["status"] == "prepared" for c in calls["calls"].values()):
                    reasons.append("physical_cleanup_unknown")
            navs = []
            if episode is None:
                reasons.append("episode_missing")
            else:
                program_record = records.read(episode["program_id"])
                if (program_record["kind"] != "replay_program" or program_record["experiment_id"] != test["experiment_id"]
                        or records.related(program_record["record_id"]) != [{"relation": "test", "target_id": test_id}]):
                    raise Conflict("assessment replay program binding mismatch")
                program = ReplayProgram.model_validate(json.loads(records.artifacts.read_bytes(program_record["value"]["artifact_hash"])))
                if (digest(program) != episode["program_hash"] or digest(program) != program_record["value"]["program_hash"]
                        or program.test_id != test_id or program.specification_hash != sealed.specification_hash):
                    raise Conflict("assessment replay program integrity mismatch")
                links.append(("source", program_record["record_id"]))
                evidence["program_hash"] = digest(program)
                if (episode["status"] != "ready_for_evaluation" or episode["pending"] is not None
                        or episode["cursor"] != len(program.points) or episode["stop_reason"] is not None):
                    reasons.append("replay_incomplete")
                # Only the program's committed NAV IDs qualify; never choose a
                # highest/latest valuation or accept caller-supplied return values.
                for key, nav_id in sorted(episode["valuations"].items()):
                    nav = records.read(nav_id)
                    if (nav["kind"] != "valuation" or nav["experiment_id"] != test["experiment_id"]
                            or records.related(nav_id) != [{"relation": "test", "target_id": test_id}]
                            or key != nav["value"]["purpose"] + ":" + nav["value"]["trade_date"]):
                        raise Conflict("assessment valuation binding mismatch")
                    navs.append(nav)
                    links.append(("source", nav_id))
                reasons.append("formal_replay_acceptance_unavailable")
                if program.evidence_kind == "fixture":
                    reasons.append("fixture_market_evidence")
                if episode.get("resource_accounting_complete") is not True:
                    reasons.append("resource_accounting_incomplete")
            initial = [n for n in navs if n["value"]["purpose"] == "initial"]
            terminal = [n for n in navs if n["value"]["purpose"] == "terminal"]
            daily_dates = sorted(n["value"]["trade_date"] for n in navs if n["value"]["purpose"] == "daily")
            nav_diagnostic = None
            if (len(initial) != 1 or len(terminal) != 1 or daily_dates != [d.isoformat() for d in task.trading_dates]
                    or terminal[0]["value"]["trade_date"] != task.trading_dates[-1].isoformat()):
                reasons.append("nav_coverage_incomplete")
            else:
                start, end = initial[0]["value"]["nav"], terminal[0]["value"]["nav"]
                value = terminal_return(start, end)
                nav_diagnostic = {"initial_id": initial[0]["record_id"], "terminal_id": terminal[0]["record_id"],
                                  "initial_nav": start, "terminal_nav": end, "net_return": _render(value),
                                  "net_return_exact": _exact(value), "formal_score": False}
            try:
                budget = effective_budget(records, test_id)
            except CostUnavailable:
                reasons.append("costs_missing")
            else:
                evidence["cost_budget"] = {"sequence": budget["sequence"], "value_hash": budget["effective_hash"],
                                           "reconciliation_ids": budget["reconciliation_ids"]}
                links.extend(("source", r) for r in budget["reconciliation_ids"])
                calls = budget["value"]["invocations"].values()
                if budget["value"]["failures"]:
                    reasons.append("costs_invalid")
                if any(c["status"] not in {"settled", "released_unstarted"} for c in calls):
                    reasons.append("usage_unsettled")
                for bucket in ("environment", "evaluation"):
                    if not any(c["status"] == "settled" and c["bound"]["bucket"] == bucket for c in calls):
                        reasons.append(bucket + "_costs_missing")
            value = {"evaluation_version": 1, "request": request, "status_at_assessment": status,
                     "definition_hash": definition["content_hash"], "specification_hash": sealed.specification_hash,
                     "sample": test["value"]["sample"], "role": test["value"]["role"],
                     "valid": not reasons, "reasons": sorted(set(reasons)), "evidence": evidence,
                     "nav_diagnostic": nav_diagnostic, "formal_ready": False}
            prepared = records.prepare(experiment_id=test["experiment_id"], kind="evaluation", record_id=identity,
                submission_identity="assessment:" + digest([test_id, submission_identity]), value=value, links=links)
            return records._insert(prepared)


class ComparisonAssessments:
    """Freeze the pair before execution and retain one original result snapshot.

    Frozen runtime conditions require matching formal host assessments. The
    selection controller separately commits an eligible comparison with CAS.
    """
    def __init__(self, records):
        self.records = records

    def freeze(self, plan_id, *, baseline, candidate, submission_identity, previous_comparison=None,
               expected_selection_id=None, runtime_conditions=None):
        if not isinstance(submission_identity, str) or not submission_identity.strip():
            raise ValueError("comparison submission identity required")
        records = self.records
        plan = records.read(plan_id)
        if (plan["kind"] != "test_plan" or plan["value"].get("registration_version") != 1
                or plan["value"]["purpose"] not in {"tuning", "selection_validation"}):
            raise Conflict("comparison requires a registered tuning or selection plan")
        definition = records.read(plan["value"]["definition_id"])
        compare_returns(definition["value"]["specification"], plan["value"]["plan"], {}, baseline=baseline, candidate=candidate)
        identity = record_identity(plan["experiment_id"], "comparison-plan", submission_identity)
        tests = [record_identity(plan["experiment_id"], "test", sample["test_id"]) for sample in plan["value"]["plan"]["tests"]]
        packages = {}
        links = [("plan", plan_id), *(("test", test_id) for test_id in tests)]
        for name in (baseline, candidate):
            proposal = records.read(record_identity(plan["experiment_id"], "candidate", name))
            if proposal["kind"] != "candidate_proposal" or proposal["value"].get("registration_version") != 1:
                raise Conflict("comparison requires typed candidates")
            packages[name] = proposal["value"]["proposal"]["package_hash"]
            read_candidate(packages[name], artifacts=records.artifacts)
            links.append(("candidate", proposal["record_id"]))
        if previous_comparison is not None:
            previous = records.read(previous_comparison)
            if (previous["kind"] != "comparison" or previous["experiment_id"] != plan["experiment_id"]
                    or previous["value"].get("comparison_version") != 1
                    or previous["value"].get("record_type") != "result"
                    or previous["value"]["decision"] != "inconclusive"
                    or previous["value"]["plan_id"] == plan_id):
                raise Conflict("a repaired comparison requires a new plan linked to the inconclusive result")
            links.append(("comparison", previous_comparison))
        value = {"comparison_version": 1, "record_type": "plan", "plan_id": plan_id,
                 "baseline": baseline, "candidate": candidate, "test_ids": tests,
                 "definition_id": definition["record_id"], "specification_hash": plan["value"]["plan"]["specification_hash"],
                 "plan_hash": plan["content_hash"], "packages": packages, "previous_comparison": previous_comparison,
                 "runtime_fingerprint_complete": False}
        artifacts = set()
        if runtime_conditions is not None:
            if expected_selection_id is None:
                raise Conflict("qualified comparison requires the expected current selection")
            conditions, artifacts = _runtime_conditions(records, definition, plan["value"]["plan"], runtime_conditions)
            value.update(runtime_conditions=conditions, runtime_fingerprint=digest({"runtime": conditions, "packages": packages}),
                         runtime_fingerprint_complete=True)
        if expected_selection_id is not None:
            selected = records.read(expected_selection_id)
            baseline_id = record_identity(plan["experiment_id"], "candidate", baseline)
            if (selected["kind"] != "selection" or selected["experiment_id"] != plan["experiment_id"]
                    or selected["value"].get("selection_version") != 1 or selected["value"]["operation"] == "finalize"
                    or selected["value"]["current_baseline_id"] != baseline_id
                    or selected["value"]["definition_id"] != definition["record_id"]):
                raise Conflict("comparison must face the current baseline under its original definition")
            value.update(selection_id=expected_selection_id, selection_hash=selected["content_hash"])
            links.append(("selection", expected_selection_id))
        value["registration_fingerprint"] = digest(value)
        prepared = records.prepare(experiment_id=plan["experiment_id"], kind="comparison", record_id=identity,
            submission_identity="comparison-plan:" + submission_identity, value=value, links=links, artifact_hashes=artifacts)
        with records._transaction():
            reused = plan["value"].get("reuse", {}).get("sources", {})
            if reused:
                from .reuse import validate_reused_plan
                validate_reused_plan(records, plan, expected_selection_id, value.get("runtime_conditions"))
            if not records.db.execute("SELECT 1 FROM lagent_records WHERE record_id=?", (identity,)).fetchone():
                if expected_selection_id is not None:
                    from .selection import current_selection
                    current = current_selection(records, plan["experiment_id"])
                    if current is None or current["record_id"] != expected_selection_id:
                        raise Conflict("selection revision changed before comparison freeze")
                for test_id, sample in zip(tests, plan["value"]["plan"]["tests"]):
                    if test_id in reused:
                        continue
                    test = records._test(test_id)
                    if (test["value"].get("plan_id") != plan_id or test["value"].get("sample") != sample
                            or test["value"].get("rerun_of") or records.status(test_id) != "created"
                            or records.projection(test_id, "cost_budget") is not None):
                        raise Conflict("comparison pair and samples must freeze before execution")
            return records._insert(prepared)

    def complete(self, comparison_id, *, require_terminal=False):
        records = self.records
        frozen = records.read(comparison_id)
        if (frozen["kind"] != "comparison" or frozen["value"].get("comparison_version") != 1
                or frozen["value"].get("record_type") != "plan"):
            raise Conflict("comparison requires its frozen pair")
        identity = record_identity(frozen["experiment_id"], "comparison-result", comparison_id)
        with records._transaction():
            if records.db.execute("SELECT 1 FROM lagent_records WHERE record_id=?", (identity,)).fetchone():
                return records.read(identity)
            value = frozen["value"]
            plan = records.read(value["plan_id"])
            if require_terminal and any(records.status(test_id) not in TERMINAL for test_id in value["test_ids"]):
                raise Conflict("comparison result requires every predeclared Test to be terminal")
            samples, links = [], [("comparison", comparison_id), ("plan", plan["record_id"])]
            for test_id, sample in zip(value["test_ids"], plan["value"]["plan"]["tests"]):
                # Identity/sequence choice is independent of returns. A later
                # rescore can never silently replace this comparison's sample.
                row = records.db.execute("SELECT record_id FROM lagent_records WHERE kind='evaluation' "
                    "AND json_extract(value_json,'$.evaluation_version')=1 AND json_extract(value_json,'$.request.test_id')=? "
                    "ORDER BY record_sequence LIMIT 1", (test_id,)).fetchone()
                state = records.status(test_id)
                item = {"test_id": test_id, "status": state, "assessment_id": None, "assessment_hash": None}
                if row:
                    assessment = records.read(row[0])
                    if (assessment["experiment_id"] != frozen["experiment_id"]
                            or {"relation": "test", "target_id": test_id} not in records.related(row[0])):
                        raise Conflict("comparison assessment scope mismatch")
                    item.update(assessment_id=row[0], assessment_hash=assessment["content_hash"])
                    links.append(("evaluation", row[0]))
                samples.append(item)
            result, qualified = qualified_comparison(records, frozen, samples)
            summary = result["summary"] or {"baseline_mean_return": None, "candidate_mean_return": None,
                "mean_improvement": None, "worst_paired_difference": None, "positive_repeats": None,
                "planned_repeats": result["planned_repeats"]}
            payload = {"comparison_version": 1, "record_type": "result", "status": "completed",
                       "plan_id": plan["record_id"], "comparison_id": comparison_id,
                       "registration_fingerprint": value["registration_fingerprint"], "samples": samples,
                       "result": result, "decision": result["decision"], "formal_ready": qualified,
                       "promotion_authorized": qualified and result["decision"] == "eligible",
                       "selection_id": value.get("selection_id"), "runtime_fingerprint": value.get("runtime_fingerprint"),
                       "public_summary": {**summary, "decision": result["decision"]}}
            prepared = records.prepare(experiment_id=frozen["experiment_id"], kind="comparison", record_id=identity,
                submission_identity="comparison-result:" + comparison_id, value=payload, links=links)
            return records._insert(prepared)
