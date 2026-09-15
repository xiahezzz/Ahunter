"""Versioned experiment inputs. Defaults live in presets, never in the runner.

Required nullable caps distinguish an explicit unlimited value from a missing
input. A draft may be stored before validation; only resolve() yields a sealed
specification, and even that still requires capability preflight before running.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import json

from pydantic import BaseModel, ConfigDict, Field, model_validator

PositiveInt = Annotated[int, Field(strict=True, gt=0)]
NonnegativeInt = Annotated[int, Field(strict=True, ge=0)]
Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
PositiveDecimal = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Fraction = Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)]
Name = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:@-]*$")]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


def numeric_field(unit: str, *, null: str = "forbidden", **kwargs):
    return Field(json_schema_extra={"unit": unit, "null_semantics": null,
                                   "default_source": "explicit input or versioned preset"}, **kwargs)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Period(Contract):
    mode: Literal["range", "trading_days"]
    start: date
    end: date | None = None
    trading_days: PositiveInt | None = numeric_field("trading_day", default=None, null="inactive in range mode")

    @model_validator(mode="after")
    def coherent(self):
        if self.mode == "range":
            if self.end is None or self.end < self.start or self.trading_days is not None:
                raise ValueError("range requires end >= start and no trading_days")
        elif self.trading_days is None or self.end is not None:
            raise ValueError("trading_days mode requires a positive count and no end")
        return self


class Task(Contract):
    task_id: Name
    role: Literal["tuning", "selection_validation", "final_holdout"]
    research_start_date: date
    period: Period
    weight: PositiveDecimal = numeric_field("relative_weight")

    @model_validator(mode="after")
    def starts_before_market(self):
        if self.research_start_date > self.period.start:
            raise ValueError("research must start no later than the first trading date")
        return self


class ModelSettings(Contract):
    model: Name
    reasoning_effort: Name
    token_cap: PositiveInt | None = numeric_field("token", null="unlimited")


class ClockSettings(Contract):
    timezone: str
    mode: Literal["fixed_phase_snapshot"]
    auction_close: time
    decision_close: time
    postmarket_after: time
    initial_research_at: time
    auction_as_of: time
    auction_submit_at: time
    postauction_event_cutoff: time
    postauction_as_of: time
    postauction_submit_at: time
    postmarket_as_of: time

    @model_validator(mode="after")
    def ordered(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown clock timezone") from exc
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, time) and value.tzinfo is not None:
                raise ValueError("clock times are local; timezone is configured separately")
        if not (self.auction_as_of <= self.auction_submit_at < self.auction_close
                == self.postauction_event_cutoff <= self.postauction_as_of
                <= self.postauction_submit_at < self.decision_close < self.postmarket_after
                < self.postmarket_as_of):
            raise ValueError("invalid phase order or post-auction event cutoff")
        if self.initial_research_at <= self.postmarket_after:
            raise ValueError("initial research must follow postmarket boundary")
        return self


class Position(Contract):
    security: Name
    quantity: PositiveInt = numeric_field("share")
    acquired_on: date
    cost_basis: Money = numeric_field("CNY")


class AccountSettings(Contract):
    initial_cash: Money = numeric_field("CNY")
    initial_positions: tuple[Position, ...]
    currency: Literal["CNY"]
    policy_ref: Name
    available_credit: Money = numeric_field("CNY")
    valuation_policy_ref: Name
    entitlements: tuple[Literal["main", "star", "chinext", "st"], ...]
    cash_interest_rate: Money = numeric_field("annual_fraction")
    external_cashflows: Literal[False]
    voluntary_subscription: Literal[False]
    advance_unfilled_sale_proceeds: Literal[False]

    @model_validator(mode="after")
    def unique_holdings(self):
        if len({p.security for p in self.initial_positions}) != len(self.initial_positions):
            raise ValueError("initial positions must be unique by security")
        if len(set(self.entitlements)) != len(self.entitlements):
            raise ValueError("entitlements must be unique")
        return self


class SearchSettings(Contract):
    enabled: bool = Field(strict=True)
    provider: Name
    connection_ref: Name
    trust_policy: Literal["provider_time_filter"]
    date_only_policy: Literal["next_date"]
    topic: Literal["general"]
    depth: Literal["basic", "advanced"]
    max_results: PositiveInt = numeric_field("result")
    timeout_seconds: PositiveInt = numeric_field("second")
    retries: NonnegativeInt = numeric_field("retry")
    include_raw_content: bool = Field(strict=True)
    auto_parameters: Literal[False]
    include_answer: Literal[False]


class DataSettings(Contract):
    adapter: Literal["local_historical_bundle_v1"]
    bundle_ref: Name
    product_refs: tuple[Name, ...]
    historical_time_policy_ref: Name
    search: SearchSettings


class ExecutionSettings(Contract):
    market_impact: Literal[False]
    market_rule_ref: Name
    calendar_ref: Name
    fill_model_ref: Literal["fixed_path_conservative_v1"]
    minute_model_ref: Literal["conservative_minute_v1"]
    queue_model_ref: Literal["queue_replay_v1"]
    fees_ref: Name
    participation_rate: Fraction = numeric_field("fraction_of_historical_shares")
    minute_slippage_ticks: NonnegativeInt = numeric_field("tick")
    queue_slippage_ticks: NonnegativeInt = numeric_field("tick")
    order_latency_ms: NonnegativeInt = numeric_field("millisecond")
    cancel_latency_ms: NonnegativeInt = numeric_field("millisecond")
    receipt_latency_ms: NonnegativeInt = numeric_field("millisecond")
    commission_rate: Money = numeric_field("fraction_of_turnover")
    minimum_commission: Money = numeric_field("CNY_per_filled_order")
    commission_includes_exchange_fees: bool = Field(strict=True)
    fee_rounding: Literal["ROUND_HALF_UP"]
    fee_quantum: PositiveDecimal = numeric_field("CNY")
    evidence: Literal["minute_plus_auction_and_required_queue_evidence"]


class BudgetSettings(Contract):
    mode: Literal["explicit", "calibrated"]
    task_cost_limit: PositiveDecimal | Literal["calibration_derived"] = numeric_field("USD")
    environment_reserve: Money | Literal["unresolved"] = numeric_field("USD")
    evaluation_reserve: Money | Literal["unresolved"] = numeric_field("USD")
    cost_currency: Literal["USD"]
    price_table_ref: Name
    calibration_repeats: PositiveInt = numeric_field("episode")
    calibration_multiplier: PositiveDecimal = numeric_field("multiplier")
    rounding_unit: PositiveDecimal = numeric_field("USD")
    envelope_safety_factor: PositiveDecimal = numeric_field("multiplier")

    @model_validator(mode="after")
    def reserves_fit(self):
        if self.mode == "explicit" and self.task_cost_limit == "calibration_derived":
            raise ValueError("explicit budget requires task_cost_limit")
        if self.mode == "calibrated" and self.task_cost_limit != "calibration_derived":
            raise ValueError("calibrated budget derives task_cost_limit from calibration evidence")
        if isinstance(self.task_cost_limit, Decimal):
            reserves = [self.environment_reserve, self.evaluation_reserve]
            if all(isinstance(value, Decimal) for value in reserves) and sum(reserves) >= self.task_cost_limit:
                raise ValueError("cost reserves must leave a positive research allowance")
        return self


class RuntimeSettings(Contract):
    timeout_seconds: PositiveInt = numeric_field("second")
    phase_wall_timeout_seconds: PositiveInt = numeric_field("second")
    retries: NonnegativeInt = numeric_field("transport_retry_per_logical_call")
    candidate_recoveries: NonnegativeInt = numeric_field("recovery_per_phase")
    subagent_concurrency: PositiveInt = numeric_field("child_agent")
    cancel_wait_seconds: PositiveInt = numeric_field("second")
    lease_seconds: PositiveInt = numeric_field("second")
    heartbeat_seconds: PositiveInt = numeric_field("second")
    max_steps: PositiveInt | None = numeric_field("step", null="unlimited")
    total_subagents: PositiveInt | None = numeric_field("child_agent", null="unlimited")
    total_queries: PositiveInt | None = numeric_field("query", null="unlimited")
    recursive_delegation: Literal[False]

    @model_validator(mode="after")
    def lease_order(self):
        if self.heartbeat_seconds >= self.lease_seconds:
            raise ValueError("heartbeat must precede lease expiry")
        if self.timeout_seconds > self.phase_wall_timeout_seconds:
            raise ValueError("call timeout cannot exceed phase timeout")
        return self


class RepeatFraction(Contract):
    numerator: PositiveInt = numeric_field("ratio_numerator")
    denominator: PositiveInt = numeric_field("ratio_denominator")

    @model_validator(mode="after")
    def proper_fraction(self):
        if self.numerator > self.denominator:
            raise ValueError("repeat fraction must be at most one")
        return self


class EvaluationSettings(Contract):
    repeats: PositiveInt = numeric_field("episode_per_task_candidate")
    minimum_improvement: Money = numeric_field("absolute_return_fraction")
    positive_repeat_fraction: RepeatFraction
    worst_repeat_regression: Annotated[Decimal, Field(le=0, allow_inf_nan=False)] = numeric_field("absolute_return_fraction")
    stability_policy_ref: Name
    allow_mixed_durations: bool = Field(strict=True)
    score: Literal["mean_terminal_net_return"]
    reuse_baseline: Literal[True]
    auto_promote: Literal[True]
    retain_all_records: Literal[True]


class TraceSettings(Contract):
    retention: Literal["permanent"]
    mandatory_audit: Literal[True]
    default_page_size: PositiveInt = numeric_field("record")
    maximum_page_size: PositiveInt = numeric_field("record")

    @model_validator(mode="after")
    def page_sizes(self):
        if self.default_page_size > self.maximum_page_size:
            raise ValueError("default page size exceeds maximum")
        return self


class ExperimentDraft(Contract):
    schema_version: Literal[1]
    name: str = Field(min_length=1, max_length=240)
    tasks: tuple[Task, ...] = Field(min_length=1)
    model: ModelSettings
    clock: ClockSettings
    account: AccountSettings
    data: DataSettings
    execution: ExecutionSettings
    budget: BudgetSettings
    runtime: RuntimeSettings
    evaluate: EvaluationSettings
    trace: TraceSettings
    universe: Literal["SH_SZ_A_EXCLUDE_BJ"]

    @model_validator(mode="after")
    def unique_tasks(self):
        if len({t.task_id for t in self.tasks}) != len(self.tasks):
            raise ValueError("task IDs must be unique")
        if len(set(self.data.product_refs)) != len(self.data.product_refs):
            raise ValueError("product refs must be unique")
        return self


class ModuleError(Contract):
    code: Name
    module: Literal["experiment", "env", "runtime", "budget", "evaluate", "trace"]
    path: str
    message: str
    retryable: bool = False


class CalendarResolution(Contract):
    calendar_ref: Name
    content_hash: Hash
    coverage_start: date
    coverage_end: date
    sessions: tuple[date, ...]
    complete: bool = Field(strict=True)

    @model_validator(mode="after")
    def valid_sessions(self):
        if self.coverage_end < self.coverage_start:
            raise ValueError("invalid calendar coverage")
        if tuple(sorted(set(self.sessions))) != self.sessions:
            raise ValueError("calendar sessions must be ordered and unique")
        if any(d < self.coverage_start or d > self.coverage_end for d in self.sessions):
            raise ValueError("calendar session outside coverage")
        return self


class ResolvedTask(Contract):
    task_id: Name
    trading_dates: tuple[date, ...] = Field(min_length=1)
    initial_as_of: datetime
    calendar_hash: Hash


class ResolvedSpecification(Contract):
    schema_version: Literal[1] = 1
    specification_hash: Hash
    effective_json: str
    raw_json: str
    provenance_json: str
    field_schema_json: str
    tasks: tuple[ResolvedTask, ...]
    # Resolving configuration is not data/model/cost capability acceptance.
    capability_preflight_required: Literal[True] = True


class CandidateFile(Contract):
    path: str = Field(min_length=1)
    kind: Literal["source", "prompt", "dependency_lock", "contract"]
    content_hash: Hash
    byte_size: NonnegativeInt = numeric_field("byte")


class CandidatePackage(Contract):
    schema_version: Literal[1] = 1
    package_hash: Hash
    files: tuple[CandidateFile, ...] = Field(min_length=1)
    entrypoint: str
    allowed_config_json: str
    input_contract: Name
    output_contract: Name


class CandidateProposal(Contract):
    schema_version: Literal[1] = 1
    proposal_id: Name
    package_hash: Hash
    parent_proposal_id: Name | None
    hypothesis: str = Field(min_length=1)
    source: Literal["manual", "coding_task", "optimizer"]
    diff_artifact_hash: Hash | None

    @model_validator(mode="after")
    def no_self_parent(self):
        if self.parent_proposal_id == self.proposal_id:
            raise ValueError("proposal cannot parent itself")
        return self


class PlannedTest(Contract):
    test_id: Name
    proposal_id: Name
    task_id: Name
    repeat_id: Name
    repeat_index: NonnegativeInt = numeric_field("repeat_index")
    seed: NonnegativeInt | None = numeric_field("random_seed", null="provider unsupported")


class TestPlan(Contract):
    __test__ = False
    schema_version: Literal[1] = 1
    plan_id: Name
    specification_hash: Hash
    tests: tuple[PlannedTest, ...] = Field(min_length=1)
    seed_support: Literal["supported", "unsupported"]

    @model_validator(mode="after")
    def unique_samples(self):
        if len({t.test_id for t in self.tests}) != len(self.tests):
            raise ValueError("test IDs must be unique")
        identities = {(t.proposal_id, t.task_id, t.repeat_index) for t in self.tests}
        if len(identities) != len(self.tests):
            raise ValueError("duplicate candidate/task/repeat sample")
        repeats = {}
        for test in self.tests:
            signature = (test.repeat_id, test.seed)
            if test.repeat_index in repeats and repeats[test.repeat_index] != signature:
                raise ValueError("paired repeats must share repeat ID and seed plan")
            repeats[test.repeat_index] = signature
            if (test.seed is None) != (self.seed_support == "unsupported"):
                raise ValueError("seed must agree with provider seed support")
        if len({v[0] for v in repeats.values()}) != len(repeats):
            raise ValueError("repeat IDs must be unique across indices")
        return self


def date_search_boundary(as_of: datetime, *, source_timezone: str) -> dict:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    day = as_of.astimezone(ZoneInfo(source_timezone)).date()
    return {"published_before_date_exclusive": day.isoformat(),
            "last_permitted_date_inclusive": (day - timedelta(days=1)).isoformat(),
            "source_timezone": source_timezone, "trust_policy": "provider_time_filter"}


def original_case() -> dict:
    """Fresh editable preset including tuning, selection and final holdout tasks."""
    path = Path(__file__).resolve().parents[3] / "config/research/experiments/original-case.v1.json"
    return json.loads(path.read_text(encoding="utf-8"))
