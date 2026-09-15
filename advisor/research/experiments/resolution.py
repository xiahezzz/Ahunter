"""Pure specification resolution with an injected, versioned calendar boundary."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
import hashlib
import json
from typing import Callable
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

from .contracts import (CalendarResolution, ExperimentDraft, ModuleError, Period,
                        ResolvedSpecification, ResolvedTask)


def canonical_value(value):
    if isinstance(value, BaseModel):
        return canonical_value(value.model_dump(mode="python"))
    if isinstance(value, Decimal):
        # Avoid Decimal.normalize(), which can round through the active context.
        text = format(value, "f")
        return (text.rstrip("0").rstrip(".") if "." in text else text) if value else "0"
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: canonical_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [canonical_value(item) for item in value]
    return value


def encode(value):
    return json.dumps(canonical_value(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Resolution:
    raw_json: str
    errors: tuple[ModuleError, ...]
    specification: ResolvedSpecification | None

    @property
    def status(self):
        return "resolved" if self.specification is not None else "unresolved"


def validation_errors(raw) -> tuple[ModuleError, ...]:
    try:
        ExperimentDraft.model_validate(raw)
        return ()
    except ValidationError as exc:
        return tuple(ModuleError(code="missing_configuration" if e["type"] == "missing" else "invalid_configuration",
                                 module="experiment", path=".".join(map(str, e["loc"])), message=e["msg"])
                     for e in exc.errors(include_input=False, include_url=False))


def _merge(raw, preset, path=""):
    """Merge mappings only; ordered lists are replaced as a whole by the caller."""
    merged = {}
    provenance = {}
    for key in sorted(set(preset) | set(raw)):
        field = f"{path}.{key}" if path else key
        chosen = raw[key] if key in raw else preset[key]
        if isinstance(chosen, dict):
            base = preset.get(key, {})
            merged[key], children = _merge(raw.get(key, {}), base if isinstance(base, dict) else {}, field)
            provenance.update(children)
        else:
            merged[key] = chosen
            provenance[field] = "input" if key in raw else "preset"
    return merged, provenance


def _leaves(value, path=""):
    if isinstance(value, dict) and value:
        for key, item in value.items():
            yield from _leaves(item, f"{path}.{key}" if path else key)
    elif isinstance(value, list) and value:
        for index, item in enumerate(value):
            yield from _leaves(item, f"{path}.{index}")
    else:
        yield path, value


def _value_at(value, path):
    try:
        for part in path.split("."):
            value = value[int(part)] if isinstance(value, list) else value[part]
        return True, value
    except (KeyError, IndexError, TypeError, ValueError):
        return False, None


def resolve(raw: dict, *, calendar: Callable[[str, Period], CalendarResolution],
            preset: dict | None = None, preset_ref: str | None = None) -> Resolution:
    # Capture a detached JSON value before processing; no mutable caller objects survive.
    raw_json = encode(raw)
    raw = json.loads(raw_json)
    if preset is not None and not preset_ref:
        raise ValueError("a versioned preset_ref is required when applying defaults")
    merged, origins = _merge(raw, json.loads(encode(preset or {})))
    errors = list(validation_errors(merged))
    if errors:
        return Resolution(raw_json, tuple(errors), None)
    spec = ExperimentDraft.model_validate(merged)
    if spec.account.policy_ref != "cash_equity_v1" or spec.account.available_credit != 0:
        errors.append(ModuleError(code="account_policy_unsupported", module="env", path="account",
                                  message="cash_equity_v1 is the only implemented policy and does not provide credit"))
    if spec.account.initial_cash == 0 and not spec.account.initial_positions:
        errors.append(ModuleError(code="invalid_initial_nav", module="env", path="account",
                                  message="initial NAV must be positive"))
    resolved_tasks = []
    for task in spec.tasks:
        path = f"tasks.{task.task_id}.period"
        try:
            evidence = CalendarResolution.model_validate(calendar(spec.execution.calendar_ref, task.period))
        except (ValueError, LookupError) as exc:
            errors.append(ModuleError(code="calendar_unavailable", module="env", path=path,
                                      message=str(exc)))
            continue
        period = task.period
        sessions = tuple(d for d in evidence.sessions if d >= period.start and
                         (period.end is None or d <= period.end))
        if period.mode == "trading_days":
            sessions = sessions[:period.trading_days]
        enough = (evidence.complete and evidence.calendar_ref == spec.execution.calendar_ref
                  and evidence.coverage_start <= period.start and bool(sessions)
                  and sessions[0] == period.start)
        if period.mode == "trading_days":
            enough = enough and len(sessions) == period.trading_days
        else:
            enough = enough and evidence.coverage_end >= period.end and sessions[-1] == period.end
        if not enough:
            errors.append(ModuleError(code="calendar_coverage_missing", module="env", path=path,
                                      message="complete version-matched calendar coverage required; period was not shortened"))
            continue
        initial = datetime.combine(task.research_start_date, spec.clock.initial_research_at, ZoneInfo(spec.clock.timezone))
        first = datetime.combine(sessions[0], spec.clock.auction_as_of, ZoneInfo(spec.clock.timezone))
        if initial >= first:
            errors.append(ModuleError(code="invalid_phase_order", module="env", path=path,
                                      message="initial research must precede the first trading phase"))
        resolved_tasks.append(ResolvedTask(task_id=task.task_id, trading_dates=sessions,
                                           initial_as_of=initial, calendar_hash=evidence.content_hash))
    # Different durations are valid tasks. Evaluation groups them unless the
    # sealed specification explicitly opts into a weighted mixed-period score.
    if errors:
        return Resolution(raw_json, tuple(errors), None)
    effective = canonical_value(spec)
    # Include the exported units/ranges and complete calendar binding in identity.
    schema = ExperimentDraft.model_json_schema()
    identity = {"effective": effective, "tasks": resolved_tasks, "field_schema": schema}
    provenance = {}
    for path, value in _leaves(effective):
        origin = next((origins[key] for key in sorted(origins, key=len, reverse=True)
                       if path == key or path.startswith(key + ".")), "schema")
        present, raw_value = _value_at(raw, path)
        _, original_value = _value_at(merged, path)
        provenance[path] = {"source": preset_ref if origin == "preset" else origin,
                            "raw_present": present, "raw_value": raw_value,
                            "selected_value": original_value, "effective_value": value}
    provenance["$schema"] = {"source": "experiment-contracts@1"}
    if preset is not None:
        provenance["$preset"] = {"source": preset_ref, "content_hash": digest(preset)}
    sealed = ResolvedSpecification(specification_hash=digest(identity), effective_json=encode(effective),
                                   raw_json=raw_json, provenance_json=encode(provenance),
                                   field_schema_json=encode(schema), tasks=tuple(resolved_tasks))
    return Resolution(raw_json, (), sealed)


def verify_specification(sealed: ResolvedSpecification) -> ExperimentDraft:
    """Reject altered exports before a later module uses the sealed conditions."""
    effective = json.loads(sealed.effective_json)
    identity = {"effective": effective, "tasks": sealed.tasks,
                "field_schema": json.loads(sealed.field_schema_json)}
    if digest(identity) != sealed.specification_hash:
        raise ValueError("resolved specification content hash mismatch")
    return ExperimentDraft.model_validate(effective)
