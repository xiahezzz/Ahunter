from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_REF_RE = re.compile(r"^(?P<id>[a-z][a-z0-9_]{1,63})@(?P<version>[1-9][0-9]*)$")
_CODE_RE = re.compile(r"^[0-9]{6}$")
# ``689xxx`` are CDRs, not the ordinary A-share universe used by Research
# and Market Daily.  Keep this definition aligned with the expected-universe
# contract rather than allowing a Subject that no whole-market provider can
# ever observe.
_A_SHARE_CODE_RE = re.compile(r"^(?:00[0-3]|30[0-1]|60[0135]|688)[0-9]{3}$")


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class VersionRef(ContractModel):
    id: str
    version: int = Field(ge=1)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not isinstance(value, str) or not _ID_RE.fullmatch(value):
            raise ValueError("id must be lowercase snake_case and 2-64 characters")
        return value

    @classmethod
    def parse(cls, value: str | dict[str, Any] | VersionRef) -> VersionRef:
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            return cls.model_validate(value)
        if not isinstance(value, str):
            raise TypeError("version reference must be a string")
        match = _REF_RE.fullmatch(value)
        if match is None:
            raise ValueError("version reference must look like id@1")
        return cls(id=match.group("id"), version=int(match.group("version")))

    def __str__(self) -> str:
        return f"{self.id}@{self.version}"


class ResearchScope(StrEnum):
    """The two supported research subjects share one lifecycle and catalog."""

    market = "market"
    security = "security"


class ResearchSubject(ContractModel):
    """A precisely bounded whole-market or one-security research subject.

    ``scope`` deliberately defaults to ``security`` for pre-RE-023 callers and
    persisted fixtures.  A Market Subject is never represented by a synthetic
    code; callers must state ``scope=ResearchScope.market`` instead.
    """

    scope: ResearchScope = ResearchScope.security
    code: str | None = None
    name: str | None = None

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not _CODE_RE.fullmatch(value) or not _A_SHARE_CODE_RE.fullmatch(value):
            raise ValueError("subject code must be a six-digit A-share code")
        return value

    @model_validator(mode="after")
    def validate_scope_shape(self) -> ResearchSubject:
        if self.scope == ResearchScope.market:
            if self.code is not None:
                raise ValueError("Market Subject must not carry a security code")
            return self
        if self.code is None:
            raise ValueError("Security Subject requires a six-digit A-share code")
        return self

    @property
    def is_market(self) -> bool:
        return self.scope == ResearchScope.market


class ResearchBoundary(ContractModel):
    as_of: datetime

    @field_validator("as_of")
    @classmethod
    def require_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return value


class EvidenceRef(ContractModel):
    evidence_id: str = Field(min_length=1, max_length=160)
    source: str = Field(min_length=1, max_length=160)
    locator: str | None = Field(default=None, max_length=500)
    observed_at: datetime | None = None
    excerpt: str | None = Field(default=None, max_length=2000)

    @field_validator("observed_at")
    @classmethod
    def evidence_time_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("evidence observed_at must be timezone-aware")
        return value


class FindingClaim(ContractModel):
    claim_id: str = Field(min_length=1, max_length=100)
    statement: str = Field(min_length=1, max_length=4000)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=20)


class FindingQuality(ContractModel):
    status: Literal["passed", "warning", "blocked"]
    checks: tuple[str, ...] = Field(default_factory=tuple, max_length=30)
    limitations: tuple[str, ...] = Field(default_factory=tuple, max_length=30)


class ResearchFinding(ContractModel):
    agent: VersionRef
    subject: ResearchSubject
    boundary: ResearchBoundary
    summary: str = Field(min_length=1, max_length=12000)
    claims: tuple[FindingClaim, ...] = Field(default_factory=tuple, max_length=50)
    evidence: tuple[EvidenceRef, ...] = Field(default_factory=tuple, max_length=200)
    risks: tuple[str, ...] = Field(default_factory=tuple, max_length=30)
    invalidation_conditions: tuple[str, ...] = Field(default_factory=tuple, max_length=30)
    quality: FindingQuality
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("agent", mode="before")
    @classmethod
    def parse_agent_ref(cls, value: object) -> VersionRef:
        return VersionRef.parse(value)  # type: ignore[arg-type]

    @model_validator(mode="after")
    def validate_claim_evidence(self) -> ResearchFinding:
        known = {item.evidence_id for item in self.evidence}
        if any(evidence_id not in known for claim in self.claims for evidence_id in claim.evidence_ids):
            raise ValueError("every claim evidence_id must refer to finding evidence")
        return self


class DataProductManifest(ContractModel):
    product: VersionRef
    title: str = Field(min_length=1, max_length=200)
    request_schema: dict[str, Any] = Field(default_factory=dict)
    result_schema: dict[str, Any] = Field(default_factory=dict)
    dependencies: tuple[VersionRef, ...] = Field(default_factory=tuple, max_length=20)
    required_fields: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    optional_fields: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    freshness_minutes: int | None = Field(default=None, ge=1, le=525600)
    minimum_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    conflict_tolerance: float | None = Field(default=None, ge=0.0)
    providers: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    # A declarative contract for the optional, product-specific scope carried
    # by an Agent's ProductAccess.  It is descriptive metadata for the
    # catalog/UI; ProductAccess itself contains the actual immutable grant.
    feed_scope: dict[str, Any] | None = None
    retention_class: str = Field(default="standard", min_length=1, max_length=80)
    derived: bool = False

    @field_validator("product", mode="before")
    @classmethod
    def parse_product_ref(cls, value: object) -> VersionRef:
        return VersionRef.parse(value)  # type: ignore[arg-type]

    @field_validator("dependencies", mode="before")
    @classmethod
    def parse_dependency_refs(cls, value: object) -> tuple[VersionRef, ...]:
        return tuple(VersionRef.parse(item) for item in (value or ()))  # type: ignore[arg-type]

    @model_validator(mode="after")
    def validate_fields(self) -> DataProductManifest:
        if set(self.required_fields) & set(self.optional_fields):
            raise ValueError("required and optional product fields must be disjoint")
        if self.derived and not self.dependencies:
            raise ValueError("derived product requires dependencies")
        if not self.derived and not self.providers:
            raise ValueError("source product requires at least one provider")
        return self


class MxRidFeedScope(ContractModel):
    """The only currently published feed scope: exact MX RID identities."""

    rids: tuple[int, ...] = Field(min_length=1, max_length=100)

    @field_validator("rids", mode="before")
    @classmethod
    def parse_rids(cls, value: object) -> tuple[int, ...]:
        if isinstance(value, (str, bytes)):
            raise ValueError("MX RID scope must be an array")
        try:
            values = tuple(value)  # type: ignore[arg-type]
        except TypeError as error:
            raise ValueError("MX RID scope must be an array") from error
        if any(type(item) is not int or item <= 0 or item > 2**53 - 1 for item in values):
            raise ValueError("MX RID scope contains an invalid RID")
        if len(set(values)) != len(values):
            raise ValueError("MX RID scope contains duplicate RIDs")
        return tuple(sorted(values))


class ProductAccess(ContractModel):
    """One exact Product grant, optionally narrowed to an MX RID feed set."""

    product: VersionRef
    feed_scope: MxRidFeedScope | None = None

    @field_validator("product", mode="before")
    @classmethod
    def parse_product_ref(cls, value: object) -> VersionRef:
        return VersionRef.parse(value)  # type: ignore[arg-type]


class AgentDataAccess(ContractModel):
    """A normalized, immutable collection of ProductAccess grants."""

    products: tuple[ProductAccess, ...] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_products(self) -> AgentDataAccess:
        keys = [str(item.product) for item in self.products]
        if len(keys) != len(set(keys)):
            raise ValueError("Agent Data Access products must be unique")
        return self


class AgentManifest(ContractModel):
    agent: VersionRef
    # Omitted only for a historical Manifest.  New publication APIs require
    # this field explicitly, while Catalog loading keeps old YAML immutable.
    scope: ResearchScope = ResearchScope.security
    title: str = Field(min_length=1, max_length=200)
    instructions: str = Field(min_length=1, max_length=20000)
    # required_products is intentionally retained only as a read-compatible
    # representation for published legacy manifests.  New manifests contain
    # data_access exclusively and are never rewritten into this shape.
    required_products: tuple[VersionRef, ...] | None = Field(default=None, max_length=50)
    data_access: tuple[ProductAccess, ...] | None = Field(default=None, max_length=50)
    details_schema: dict[str, Any] = Field(default_factory=dict)
    query_budget: int | None = Field(default=None, ge=0)
    max_result_rows: int | None = Field(default=None, ge=1)
    max_result_bytes: int | None = Field(default=None, ge=1)
    implementation: Literal["declarative", "code"] = "declarative"
    code_path: str | None = None

    @field_validator("query_budget", "max_result_rows", "max_result_bytes")
    @classmethod
    def retire_query_budgets(cls, value: int | None) -> None:
        # Published YAML remains immutable. Numeric budgets are legacy metadata;
        # the current engine exposes and executes all Agent queries without caps.
        return None

    @field_validator("agent", mode="before")
    @classmethod
    def parse_agent_ref(cls, value: object) -> VersionRef:
        return VersionRef.parse(value)  # type: ignore[arg-type]

    @field_validator("required_products", mode="before")
    @classmethod
    def parse_required_product_refs(cls, value: object) -> tuple[VersionRef, ...] | None:
        if value is None:
            return None
        return tuple(VersionRef.parse(item) for item in value)  # type: ignore[arg-type]

    @field_validator("data_access", mode="before")
    @classmethod
    def parse_data_access(cls, value: object) -> tuple[ProductAccess, ...] | None:
        if value is None:
            return None
        if isinstance(value, (str, bytes)):
            raise ValueError("Agent Data Access must be an array")
        try:
            return tuple(ProductAccess.model_validate(item) for item in value)  # type: ignore[arg-type]
        except TypeError as error:
            raise ValueError("Agent Data Access must be an array") from error

    @model_validator(mode="after")
    def validate_implementation(self) -> AgentManifest:
        if (self.required_products is None) == (self.data_access is None):
            raise ValueError("Agent Manifest must declare exactly one access representation")
        if self.required_products is not None:
            if not self.required_products:
                raise ValueError("Agent Manifest must declare at least one Product")
            keys = [str(product) for product in self.required_products]
            if len(keys) != len(set(keys)):
                raise ValueError("Agent Manifest Products must be unique")
        if self.data_access is not None:
            AgentDataAccess(products=self.data_access)
        if self.implementation == "code" and not self.code_path:
            raise ValueError("code-backed agent requires code_path")
        if self.implementation == "declarative" and self.code_path is not None:
            raise ValueError("declarative agent cannot declare code_path")
        return self

    @property
    def product_accesses(self) -> tuple[ProductAccess, ...]:
        """Normalized access grants without mutating a legacy Manifest."""
        if self.data_access is not None:
            return self.data_access
        return tuple(ProductAccess(product=product) for product in self.required_products or ())

    @property
    def product_refs(self) -> tuple[VersionRef, ...]:
        return tuple(access.product for access in self.product_accesses)

    @property
    def has_explicit_scope(self) -> bool:
        return "scope" in self.model_fields_set


class ResearchTeam(ContractModel):
    team: VersionRef
    # Legacy Team Manifests permanently mean Security Scope.  Publication is
    # responsible for requiring an explicit Scope on newly written versions.
    scope: ResearchScope = ResearchScope.security
    title: str = Field(min_length=1, max_length=200)
    agents: tuple[VersionRef, ...] = Field(min_length=1, max_length=100)

    @field_validator("team", mode="before")
    @classmethod
    def parse_team_ref(cls, value: object) -> VersionRef:
        return VersionRef.parse(value)  # type: ignore[arg-type]

    @field_validator("agents", mode="before")
    @classmethod
    def parse_agent_refs(cls, value: object) -> tuple[VersionRef, ...]:
        return tuple(VersionRef.parse(item) for item in (value or ()))  # type: ignore[arg-type]

    @model_validator(mode="after")
    def unique_agents(self) -> ResearchTeam:
        refs = [str(item) for item in self.agents]
        if len(refs) != len(set(refs)):
            raise ValueError("Team agents must be unique")
        return self

    @property
    def has_explicit_scope(self) -> bool:
        return "scope" in self.model_fields_set


class DecisionPipelineManifest(ContractModel):
    pipeline: VersionRef
    stages: tuple[str, ...] = Field(min_length=1, max_length=50)
    parallel_groups: tuple[tuple[str, ...], ...] = Field(default_factory=tuple, max_length=20)

    @field_validator("pipeline", mode="before")
    @classmethod
    def parse_pipeline_ref(cls, value: object) -> VersionRef:
        return VersionRef.parse(value)  # type: ignore[arg-type]

    @model_validator(mode="after")
    def unique_stages(self) -> DecisionPipelineManifest:
        if len(self.stages) != len(set(self.stages)):
            raise ValueError("pipeline stages must be unique")
        known = set(self.stages)
        if any(stage not in known for group in self.parallel_groups for stage in group):
            raise ValueError("parallel group references unknown stage")
        return self


class CodexProxyEnvironment(ContractModel):
    https_proxy: str = Field(min_length=1, max_length=500)
    http_proxy: str = Field(min_length=1, max_length=500)
    all_proxy: str = Field(min_length=1, max_length=500)


class ExecutionPolicy(ContractModel):
    policy: VersionRef
    model: str = Field(min_length=1, max_length=160)
    reasoning_effort: str = Field(min_length=1, max_length=40)
    timeout_seconds: int = Field(ge=10, le=3600)
    max_agent_concurrency: int = Field(ge=1, le=64)
    max_stage_concurrency: int = Field(ge=1, le=64)
    max_retries: int = Field(default=1, ge=0, le=1)
    codex_path: str | None = None
    proxy_environment: CodexProxyEnvironment | None = None

    @field_validator("policy", mode="before")
    @classmethod
    def parse_policy_ref(cls, value: object) -> VersionRef:
        return VersionRef.parse(value)  # type: ignore[arg-type]


class DecisionReview(ContractModel):
    stage: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=12000)
    claims: tuple[FindingClaim, ...] = Field(default_factory=tuple, max_length=50)
    evidence: tuple[EvidenceRef, ...] = Field(default_factory=tuple, max_length=200)
    risks: tuple[str, ...] = Field(default_factory=tuple, max_length=30)
    quality: FindingQuality


class TeamConclusion(ContractModel):
    team: VersionRef
    subject: ResearchSubject
    boundary: ResearchBoundary
    stance: Literal["watch_buy", "watch_add", "hold", "watch_reduce", "watch_exit"]
    conviction: Literal["low", "medium", "high"]
    evidence_quality: FindingQuality
    thesis: str = Field(min_length=1, max_length=16000)
    evidence: tuple[EvidenceRef, ...] = Field(default_factory=tuple, max_length=300)
    key_risks: tuple[str, ...] = Field(default_factory=tuple, max_length=50)
    invalidation_conditions: tuple[str, ...] = Field(default_factory=tuple, max_length=50)
    time_horizon: str = Field(min_length=1, max_length=160)
    price_range: dict[str, float] | None = None
    position_limit: str | None = Field(default=None, max_length=200)

    @field_validator("team", mode="before")
    @classmethod
    def parse_team_ref(cls, value: object) -> VersionRef:
        return VersionRef.parse(value)  # type: ignore[arg-type]

    @field_validator("price_range")
    @classmethod
    def valid_price_range(cls, value: dict[str, float] | None) -> dict[str, float] | None:
        if value is None:
            return None
        if set(value) - {"low", "high"} or not {"low", "high"}.issubset(value):
            raise ValueError("price_range must contain low and high")
        low, high = float(value["low"]), float(value["high"])
        if low <= 0 or high < low:
            raise ValueError("invalid price_range")
        return {"low": low, "high": high}


class RunStatus(StrEnum):
    pending = "pending"
    running = "running"
    passed = "passed"
    blocked = "blocked"
    failed = "failed"
    cancelled = "cancelled"


def canonical_json(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()
