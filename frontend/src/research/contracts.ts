export type Provider = { provider_id: string; display_name: string };

export type MxRidFeedScopeContract = {
  kind: "mx_rid_feeds";
  required: ["rids"];
  rids?: {
    type: "integer";
    minimum: number;
    maximum: number;
    min_items: number;
    max_items: number;
  };
};

export type Product = {
  product_ref: string;
  title: string;
  dependencies: string[];
  providers: Provider[];
  supports_feed_scope: boolean;
  feed_scope_contract: MxRidFeedScopeContract | null;
};

export type ProductGroup = { product_id: string; latest: Product; history: Product[] };

export type AccessRid = { rid: number; authorization: "current" | "revoked" };
export type DataAccess = { product_ref: string; feed_scope?: { rids: AccessRid[] } };
export type AgentAccess = {
  agent_ref: string;
  scope: "market" | "security";
  title: string;
  summary: string;
  instructions: string;
  data_access: DataAccess[];
  used_by_team_refs: string[];
  unassigned_rids: number[];
  blocked_reasons: string[];
  read_only: { query_budget: number | null; max_result_rows: number | null; implementation: "declarative" | "code" };
};
export type AgentAccessGroup = { agent_id: string; latest: AgentAccess; history: AgentAccess[] };
export type AccessDirectory = { rid_version: string; agents: AgentAccessGroup[] };
export type PublishedAccess = {
  created: boolean;
  agent: AgentAccess;
  impact: { fixed_team_refs: string[]; revoked_rids: number[] };
  message: string;
};
export type PublishedInstructions = {
  created: boolean;
  agent: AgentAccess;
  impact: { fixed_team_refs: string[] };
  message: string;
};

const REF = /^[a-z][a-z0-9_]{1,63}@[1-9][0-9]*$/;
const ID = /^[a-z][a-z0-9_]{1,63}$/;
const PROVIDER_ID = /^[a-z][a-z0-9-]{1,63}$/;
const HASH = /^[0-9a-f]{64}$/;
const MAX_SAFE_INTEGER = 2 ** 53 - 1;
const UNSAFE_TEXT = /(https?:\/\/|(?:^|[\\/])(?:Users|tmp|data)(?:[\\/])|endpoint|cookie|raw[_ -]?payload|source[_ -]?url|localhost|127\.0\.0\.1)/i;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isExactRecord(value: unknown, keys: readonly string[]): value is Record<string, unknown> {
  return isRecord(value) && Object.keys(value).length === keys.length && Object.keys(value).every((key) => keys.includes(key));
}

function safeText(value: unknown, maximum = 240): value is string {
  return typeof value === "string" && value.trim().length > 0 && value.length <= maximum && !UNSAFE_TEXT.test(value);
}

function boundedText(value: unknown, maximum: number): value is string {
  return typeof value === "string" && value.trim().length > 0 && value.length <= maximum;
}

function validInstructions(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0 && value.length <= 20_000;
}

function safeRef(value: unknown): value is string {
  return typeof value === "string" && REF.test(value);
}

function safeRid(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0 && value <= MAX_SAFE_INTEGER;
}

function safeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value);
}

function sameBase(reference: string, id: string): boolean {
  return reference.split("@", 1)[0] === id;
}

function parseProvider(value: unknown): Provider {
  if (!isExactRecord(value, ["provider_id", "display_name"]) || !PROVIDER_ID.test(String(value.provider_id)) || !safeText(value.display_name, 120)) {
    throw new Error("Data Product 目录格式无效");
  }
  return value as Provider;
}

function parseScopeContract(value: unknown): Product["feed_scope_contract"] {
  if (value === null) return null;
  if (
    (!isExactRecord(value, ["kind", "required"]) && !isExactRecord(value, ["kind", "required", "rids"])) ||
    value.kind !== "mx_rid_feeds" ||
    !Array.isArray(value.required) ||
    value.required.length !== 1 ||
    value.required[0] !== "rids"
  ) {
    throw new Error("Data Product 目录格式无效");
  }
  if (!("rids" in value)) return { kind: "mx_rid_feeds", required: ["rids"] };

  const rids = value.rids;
  if (
    !isExactRecord(rids, ["type", "minimum", "maximum", "min_items", "max_items"]) ||
    rids.type !== "integer" ||
    !safeInteger(rids.minimum) || rids.minimum < 1 ||
    !safeInteger(rids.maximum) || rids.maximum < rids.minimum || rids.maximum > MAX_SAFE_INTEGER ||
    !safeInteger(rids.min_items) || rids.min_items < 1 ||
    !safeInteger(rids.max_items) || rids.max_items < rids.min_items || rids.max_items > 100
  ) {
    throw new Error("Data Product 目录格式无效");
  }
  return {
    kind: "mx_rid_feeds",
    required: ["rids"],
    rids: {
      type: "integer",
      minimum: rids.minimum,
      maximum: rids.maximum,
      min_items: rids.min_items,
      max_items: rids.max_items,
    },
  };
}

function parseProduct(value: unknown): Product {
  if (
    !isExactRecord(value, ["product_ref", "title", "dependencies", "providers", "supports_feed_scope", "feed_scope_contract"]) ||
    !safeRef(value.product_ref) || !safeText(value.title, 200) ||
    !Array.isArray(value.dependencies) || value.dependencies.length > 20 || !value.dependencies.every(safeRef) ||
    !Array.isArray(value.providers) || value.providers.length > 20 ||
    typeof value.supports_feed_scope !== "boolean"
  ) {
    throw new Error("Data Product 目录格式无效");
  }
  const providers = value.providers.map(parseProvider);
  const feedScope = parseScopeContract(value.feed_scope_contract);
  if (value.supports_feed_scope !== (feedScope !== null)) throw new Error("Data Product 目录格式无效");
  return { product_ref: value.product_ref, title: value.title, dependencies: [...value.dependencies], providers, supports_feed_scope: value.supports_feed_scope, feed_scope_contract: feedScope };
}

function parseProductGroup(value: unknown): ProductGroup {
  if (!isExactRecord(value, ["product_id", "latest", "history"]) || typeof value.product_id !== "string" || !ID.test(value.product_id) || !Array.isArray(value.history) || value.history.length < 1) {
    throw new Error("Data Product 目录格式无效");
  }
  const latest = parseProduct(value.latest);
  const history = value.history.map(parseProduct);
  const productId = value.product_id as string;
  if (!sameBase(latest.product_ref, productId) || history.some((item) => !sameBase(item.product_ref, productId)) || history[history.length - 1]?.product_ref !== latest.product_ref) {
    throw new Error("Data Product 目录格式无效");
  }
  return { product_id: productId, latest, history };
}

export function parseProductCatalog(value: unknown): ProductGroup[] {
  if (!isExactRecord(value, ["products"]) || !Array.isArray(value.products) || value.products.length > 100) throw new Error("Data Product 目录格式无效");
  const products = value.products.map(parseProductGroup);
  if (new Set(products.map((item) => item.product_id)).size !== products.length) throw new Error("Data Product 目录格式无效");
  return products;
}

function parseAccessRid(value: unknown): AccessRid {
  if (!isExactRecord(value, ["rid", "authorization"]) || !safeRid(value.rid) || (value.authorization !== "current" && value.authorization !== "revoked")) {
    throw new Error("Agent 访问目录格式无效");
  }
  return value as AccessRid;
}

function parseDataAccess(value: unknown): DataAccess {
  if (!isRecord(value) || !safeRef(value.product_ref)) throw new Error("Agent 访问目录格式无效");
  const keys = Object.keys(value);
  if (!keys.every((key) => key === "product_ref" || key === "feed_scope")) throw new Error("Agent 访问目录格式无效");
  if (!("feed_scope" in value)) return { product_ref: value.product_ref };
  const scope = value.feed_scope;
  if (!isExactRecord(scope, ["rids"]) || !Array.isArray(scope.rids) || scope.rids.length < 1 || scope.rids.length > 100) throw new Error("Agent 访问目录格式无效");
  const rids = scope.rids.map(parseAccessRid);
  if (new Set(rids.map((item) => item.rid)).size !== rids.length) throw new Error("Agent 访问目录格式无效");
  return { product_ref: value.product_ref, feed_scope: { rids } };
}

export function parseAgentAccess(value: unknown): AgentAccess {
  if (
    !isExactRecord(value, ["agent_ref", "scope", "title", "summary", "instructions", "data_access", "used_by_team_refs", "unassigned_rids", "blocked_reasons", "read_only"]) ||
    !safeRef(value.agent_ref) || !safeText(value.title, 200) || !boundedText(value.summary, 240) || !validInstructions(value.instructions) ||
    (value.scope !== "market" && value.scope !== "security") ||
    !Array.isArray(value.data_access) || value.data_access.length < 1 || value.data_access.length > 50 ||
    !Array.isArray(value.used_by_team_refs) || !value.used_by_team_refs.every(safeRef) ||
    !Array.isArray(value.unassigned_rids) || !value.unassigned_rids.every(safeRid) ||
    !Array.isArray(value.blocked_reasons) || value.blocked_reasons.length > 100 || !value.blocked_reasons.every((item) => safeText(item, 160)) ||
    !isExactRecord(value.read_only, ["query_budget", "max_result_rows", "implementation"]) ||
    (value.read_only.query_budget !== null && (!Number.isSafeInteger(value.read_only.query_budget) || (value.read_only.query_budget as number) < 0)) ||
    (value.read_only.max_result_rows !== null && (!Number.isSafeInteger(value.read_only.max_result_rows) || (value.read_only.max_result_rows as number) < 1)) ||
    (value.read_only.implementation !== "declarative" && value.read_only.implementation !== "code")
  ) {
    throw new Error("Agent 访问目录格式无效");
  }
  const dataAccess = value.data_access.map(parseDataAccess);
  if (new Set(dataAccess.map((item) => item.product_ref)).size !== dataAccess.length || new Set(value.unassigned_rids).size !== value.unassigned_rids.length) {
    throw new Error("Agent 访问目录格式无效");
  }
  return {
    agent_ref: value.agent_ref,
    scope: value.scope,
    title: value.title,
    summary: value.summary,
    instructions: value.instructions,
    data_access: dataAccess,
    used_by_team_refs: [...value.used_by_team_refs],
    unassigned_rids: [...value.unassigned_rids],
    blocked_reasons: [...value.blocked_reasons],
    read_only: value.read_only as AgentAccess["read_only"],
  };
}

function parseAgentGroup(value: unknown): AgentAccessGroup {
  if (!isExactRecord(value, ["agent_id", "latest", "history"]) || typeof value.agent_id !== "string" || !ID.test(value.agent_id) || !Array.isArray(value.history) || value.history.length < 1) {
    throw new Error("Agent 访问目录格式无效");
  }
  const latest = parseAgentAccess(value.latest);
  const history = value.history.map(parseAgentAccess);
  const agentId = value.agent_id as string;
  if (!sameBase(latest.agent_ref, agentId) || history.some((item) => !sameBase(item.agent_ref, agentId)) || history[history.length - 1]?.agent_ref !== latest.agent_ref) {
    throw new Error("Agent 访问目录格式无效");
  }
  return { agent_id: agentId, latest, history };
}

export function parseAccessDirectory(value: unknown): AccessDirectory {
  if (!isExactRecord(value, ["rid_version", "agents"]) || typeof value.rid_version !== "string" || !HASH.test(value.rid_version) || !Array.isArray(value.agents) || value.agents.length > 100) {
    throw new Error("Agent 访问目录格式无效");
  }
  const agents = value.agents.map(parseAgentGroup);
  if (new Set(agents.map((item) => item.agent_id)).size !== agents.length) throw new Error("Agent 访问目录格式无效");
  return { rid_version: value.rid_version, agents };
}

export function parsePublishedAccess(value: unknown): PublishedAccess {
  if (!isExactRecord(value, ["created", "agent", "impact", "message"]) || typeof value.created !== "boolean" || !safeText(value.message, 240) || !isExactRecord(value.impact, ["fixed_team_refs", "revoked_rids"]) || !Array.isArray(value.impact.fixed_team_refs) || !value.impact.fixed_team_refs.every(safeRef) || !Array.isArray(value.impact.revoked_rids) || !value.impact.revoked_rids.every(safeRid)) {
    throw new Error("Agent 数据访问发布响应格式无效");
  }
  return { created: value.created, agent: parseAgentAccess(value.agent), impact: { fixed_team_refs: [...value.impact.fixed_team_refs], revoked_rids: [...value.impact.revoked_rids] }, message: value.message };
}

export function parsePublishedInstructions(value: unknown): PublishedInstructions {
  if (
    !isExactRecord(value, ["created", "agent", "impact", "message"]) ||
    typeof value.created !== "boolean" ||
    !safeText(value.message, 240) ||
    !isExactRecord(value.impact, ["fixed_team_refs"]) ||
    !Array.isArray(value.impact.fixed_team_refs) ||
    !value.impact.fixed_team_refs.every(safeRef)
  ) {
    throw new Error("Agent Instructions 发布响应格式无效");
  }
  return {
    created: value.created,
    agent: parseAgentAccess(value.agent),
    impact: { fixed_team_refs: [...value.impact.fixed_team_refs] },
    message: value.message,
  };
}

export async function researchApiError(response: Response, fallback: string): Promise<string> {
  try {
    const payload: unknown = await response.json();
    if (isRecord(payload) && safeText(payload.detail, 160)) return payload.detail;
  } catch {
    // Bounded generic feedback is safer than displaying transport details.
  }
  return fallback;
}

export type ResearchScope = "market" | "security";
export type ResearchTeamVersion = { team_ref: string; scope: ResearchScope; title: string; agents: string[] };
export type ResearchTeamGroup = { team_id: string; latest: ResearchTeamVersion; history: ResearchTeamVersion[]; daily_enabled_ref: string | null };
export type ResearchRequest = {
  mode?: "team" | "lagent";
  request_id: string;
  team_ref: string;
  scope: ResearchScope;
  subject: { scope: ResearchScope; code: string | null; name: string | null };
  origin: "web" | "cli" | "scheduled" | "legacy";
  requested_at: string;
  accepted_at: string;
  boundary_at: string | null;
  status: "queued" | "running" | "passed" | "partial" | "blocked" | "failed" | "cancelled";
  phase: "queued" | "preflight" | "snapshot" | "agents" | "decision" | "publishing" | "complete" | "cancelled";
  agents_completed: number;
  agents_total: number;
  decision_stage: string | null;
  reason_code: string | null;
  published_at: string | null;
  rerun_of: string | null;
  last_updated_at: string;
  can_cancel: boolean;
  can_rerun: boolean;
};
export type ResearchRecord = {
  mode?: "team" | "lagent";
  record_id: string;
  request_id: string;
  team_ref: string;
  scope: ResearchScope;
  subject: { scope: ResearchScope; code: string | null; name: string | null };
  origin: "web" | "cli" | "scheduled" | "legacy";
  requested_at: string;
  accepted_at: string;
  boundary_at: string | null;
  status: "passed" | "partial" | "blocked" | "failed" | "cancelled";
  phase: string;
  reason_code: string | null;
  published_at: string | null;
  rerun_of: string | null;
  has_report: boolean;
  quality_summary: {
    status: "passed" | "warning" | "blocked" | "unavailable" | "not_applicable";
    limitations_count: number;
    blocked_insights: number;
  };
};
export type CurrentResearch = {
  service: { state: "offline" | "starting" | "idle" | "running" | "stopping" | "degraded" | "failed"; heartbeat_at: string | null; active_request_id: string | null; queued_count: number; reason_code: string | null };
  requests: ResearchRequest[];
};
export type ResearchRecordDetail = ResearchRecord & { report?: { json: Record<string, unknown>; markdown: string } };

const A_SHARE = /^(?:00[0-3]|30[0-1]|60[0135]|688)\d{3}$/;
const REQUEST_ID = /^request-[0-9a-f]{32}$/;
const RECORD_ID = /^record-[0-9a-f]{32}$/;
const ORIGINS = new Set(["web", "cli", "scheduled", "legacy"]);
const REQUEST_STATUSES = new Set(["queued", "running", "passed", "partial", "blocked", "failed", "cancelled"]);
const RECORD_STATUSES = new Set(["passed", "partial", "blocked", "failed", "cancelled"]);
const PHASES = new Set(["queued", "preflight", "snapshot", "agents", "decision", "publishing", "complete", "cancelled"]);

function scope(value: unknown): ResearchScope {
  if (value === "market" || value === "security") return value;
  throw new Error("研究范围格式无效");
}

function timestamp(value: unknown): string {
  if (typeof value !== "string" || value.length > 64 || !Number.isFinite(Date.parse(value))) throw new Error("研究时间格式无效");
  return value;
}

function nullableTimestamp(value: unknown): string | null {
  return value === null ? null : timestamp(value);
}

function optionalRef(value: unknown): string | null {
  if (value === null) return null;
  if (!safeRef(value)) throw new Error("研究引用格式无效");
  return value;
}

function boundedReason(value: unknown): string | null {
  if (value === null) return null;
  if (typeof value !== "string" || !/^[a-z][a-z0-9_]{1,79}$/.test(value)) throw new Error("研究状态格式无效");
  return value;
}

function qualitySummary(value: unknown): ResearchRecord["quality_summary"] {
  if (!isExactRecord(value, ["status", "limitations_count", "blocked_insights"]) ||
    !["passed", "warning", "blocked", "unavailable", "not_applicable"].includes(String(value.status)) ||
    !Number.isSafeInteger(value.limitations_count) || (value.limitations_count as number) < 0 || (value.limitations_count as number) > 50 ||
    !Number.isSafeInteger(value.blocked_insights) || (value.blocked_insights as number) < 0 || (value.blocked_insights as number) > 20
  ) throw new Error("研究质量摘要格式无效");
  return {
    status: value.status as ResearchRecord["quality_summary"]["status"],
    limitations_count: value.limitations_count as number,
    blocked_insights: value.blocked_insights as number,
  };
}

function parseSubject(value: unknown, expectedScope?: ResearchScope): ResearchRequest["subject"] {
  if (!isExactRecord(value, ["scope", "code", "name"])) throw new Error("研究标的格式无效");
  const parsedScope = scope(value.scope);
  if (expectedScope && parsedScope !== expectedScope) throw new Error("研究范围格式无效");
  if (value.name !== null && !safeText(value.name, 200)) throw new Error("研究标的格式无效");
  if (parsedScope === "market") {
    if (value.code !== null) throw new Error("研究标的格式无效");
  } else if (typeof value.code !== "string" || !A_SHARE.test(value.code)) {
    throw new Error("研究标的格式无效");
  }
  return { scope: parsedScope, code: value.code as string | null, name: value.name as string | null };
}

function parseTeamVersion(value: unknown): ResearchTeamVersion {
  if (!isExactRecord(value, ["team_ref", "scope", "title", "agents"]) || !safeRef(value.team_ref) || !safeText(value.title, 200) || !Array.isArray(value.agents) || value.agents.length < 1 || value.agents.length > 100 || !value.agents.every(safeRef)) {
    throw new Error("研究团队目录格式无效");
  }
  return { team_ref: value.team_ref, scope: scope(value.scope), title: value.title, agents: [...value.agents] };
}

export function parseResearchTeamDirectory(value: unknown): ResearchTeamGroup[] {
  if (!isExactRecord(value, ["teams"]) || !Array.isArray(value.teams) || value.teams.length > 100) throw new Error("研究团队目录格式无效");
  const teams = value.teams.map((item): ResearchTeamGroup => {
    if (!isExactRecord(item, ["team_id", "latest", "history", "daily_enabled_ref"]) || typeof item.team_id !== "string" || !ID.test(item.team_id) || !Array.isArray(item.history) || item.history.length < 1 || (item.daily_enabled_ref !== null && !safeRef(item.daily_enabled_ref))) {
      throw new Error("研究团队目录格式无效");
    }
    const latest = parseTeamVersion(item.latest);
    const history = item.history.map(parseTeamVersion);
    const teamId = item.team_id as string;
    const dailyEnabledRef = item.daily_enabled_ref as string | null;
    if (!sameBase(latest.team_ref, teamId) || history.some((version) => !sameBase(version.team_ref, teamId)) || history[history.length - 1]?.team_ref !== latest.team_ref) throw new Error("研究团队目录格式无效");
    return { team_id: teamId, latest, history, daily_enabled_ref: dailyEnabledRef };
  });
  if (new Set(teams.map((team) => team.team_id)).size !== teams.length) throw new Error("研究团队目录格式无效");
  return teams;
}

function parseRequest(value: unknown): ResearchRequest {
  const keys = ["request_id", "team_ref", "scope", "subject", "origin", "requested_at", "accepted_at", "boundary_at", "status", "phase", "agents_completed", "agents_total", "decision_stage", "reason_code", "published_at", "rerun_of", "last_updated_at", "can_cancel", "can_rerun"];
  if (value && typeof value === "object" && "mode" in value) {
    if (value.mode !== "team" && value.mode !== "lagent") throw new Error("研究模式无效");
    keys.push("mode");
  }
  if (!isExactRecord(value, keys) || typeof value.request_id !== "string" || !REQUEST_ID.test(value.request_id) || !safeRef(value.team_ref) || typeof value.origin !== "string" || !ORIGINS.has(value.origin as ResearchRequest["origin"]) || typeof value.status !== "string" || !REQUEST_STATUSES.has(value.status as ResearchRequest["status"]) || typeof value.phase !== "string" || !PHASES.has(value.phase as ResearchRequest["phase"]) || !Number.isSafeInteger(value.agents_completed) || !Number.isSafeInteger(value.agents_total) || (value.agents_completed as number) < 0 || (value.agents_total as number) < 0 || (value.agents_completed as number) > (value.agents_total as number) || (value.decision_stage !== null && !safeText(value.decision_stage, 80)) || typeof value.can_cancel !== "boolean" || typeof value.can_rerun !== "boolean") {
    throw new Error("研究请求格式无效");
  }
  const parsedScope = scope(value.scope);
  const subject = parseSubject(value.subject, parsedScope);
  return {
    ...(value.mode ? { mode: value.mode as "team" | "lagent" } : {}),
    request_id: value.request_id as string, team_ref: value.team_ref as string, scope: parsedScope, subject,
    origin: value.origin as ResearchRequest["origin"], requested_at: timestamp(value.requested_at), accepted_at: timestamp(value.accepted_at), boundary_at: nullableTimestamp(value.boundary_at),
    status: value.status as ResearchRequest["status"], phase: value.phase as ResearchRequest["phase"], agents_completed: value.agents_completed as number, agents_total: value.agents_total as number,
    decision_stage: value.decision_stage as string | null, reason_code: boundedReason(value.reason_code), published_at: nullableTimestamp(value.published_at), rerun_of: value.rerun_of === null ? null : (typeof value.rerun_of === "string" && REQUEST_ID.test(value.rerun_of) ? value.rerun_of : (() => { throw new Error("研究请求格式无效"); })()),
    last_updated_at: timestamp(value.last_updated_at), can_cancel: value.can_cancel as boolean, can_rerun: value.can_rerun as boolean,
  };
}

export function parseResearchRequestResponse(value: unknown): ResearchRequest {
  if (!isExactRecord(value, ["request"])) throw new Error("研究请求响应格式无效");
  return parseRequest(value.request);
}

export function parseCurrentResearch(value: unknown): CurrentResearch {
  if (!isExactRecord(value, ["service", "requests"]) || !isExactRecord(value.service, ["state", "heartbeat_at", "active_request_id", "queued_count", "reason_code"]) || !["offline", "starting", "idle", "running", "stopping", "degraded", "failed"].includes(String(value.service.state)) || !Array.isArray(value.requests) || value.requests.length > 1000 || !Number.isSafeInteger(value.service.queued_count) || (value.service.queued_count as number) < 0 || (value.service.active_request_id !== null && (typeof value.service.active_request_id !== "string" || !REQUEST_ID.test(value.service.active_request_id)))) {
    throw new Error("当前研究格式无效");
  }
  return {
    service: { state: value.service.state as CurrentResearch["service"]["state"], heartbeat_at: nullableTimestamp(value.service.heartbeat_at), active_request_id: value.service.active_request_id as string | null, queued_count: value.service.queued_count as number, reason_code: boundedReason(value.service.reason_code) },
    requests: value.requests.map(parseRequest),
  };
}

function parseRecord(value: unknown): ResearchRecord {
  const keys = ["record_id", "request_id", "team_ref", "scope", "subject", "origin", "requested_at", "accepted_at", "boundary_at", "status", "phase", "reason_code", "published_at", "rerun_of", "has_report", "quality_summary"];
  if (value && typeof value === "object" && "mode" in value) {
    if (value.mode !== "team" && value.mode !== "lagent") throw new Error("研究模式无效");
    keys.push("mode");
  }
  if (!isExactRecord(value, keys) || typeof value.record_id !== "string" || !RECORD_ID.test(value.record_id) || typeof value.request_id !== "string" || !REQUEST_ID.test(value.request_id) || !safeRef(value.team_ref) || typeof value.origin !== "string" || !ORIGINS.has(value.origin as ResearchRecord["origin"]) || typeof value.status !== "string" || !RECORD_STATUSES.has(value.status as ResearchRecord["status"]) || typeof value.phase !== "string" || value.phase.length > 80 || typeof value.has_report !== "boolean") throw new Error("研究记录格式无效");
  const parsedScope = scope(value.scope);
  return {
    ...(value.mode ? { mode: value.mode as "team" | "lagent" } : {}),
    record_id: value.record_id as string, request_id: value.request_id as string, team_ref: value.team_ref as string, scope: parsedScope, subject: parseSubject(value.subject, parsedScope), origin: value.origin as ResearchRecord["origin"],
    requested_at: timestamp(value.requested_at), accepted_at: timestamp(value.accepted_at), boundary_at: nullableTimestamp(value.boundary_at), status: value.status as ResearchRecord["status"], phase: value.phase, reason_code: boundedReason(value.reason_code), published_at: nullableTimestamp(value.published_at), rerun_of: value.rerun_of === null ? null : (typeof value.rerun_of === "string" && REQUEST_ID.test(value.rerun_of) ? value.rerun_of : (() => { throw new Error("研究记录格式无效"); })()), has_report: value.has_report, quality_summary: qualitySummary(value.quality_summary),
  };
}

export function parseResearchRecords(value: unknown): { records: ResearchRecord[]; limit: number; offset: number } {
  if (!isExactRecord(value, ["records", "limit", "offset"]) || !Array.isArray(value.records) || value.records.length > 100 || !Number.isSafeInteger(value.limit) || !Number.isSafeInteger(value.offset)) throw new Error("研究记录列表格式无效");
  return { records: value.records.map(parseRecord), limit: value.limit as number, offset: value.offset as number };
}

export function parseResearchRecordDetail(value: unknown): ResearchRecordDetail {
  if (!isRecord(value)) throw new Error("研究记录详情格式无效");
  const withReport = "report" in value;
  const raw = { ...value };
  delete raw.report;
  const record = parseRecord(raw);
  if (!withReport) return record;
  const report = value.report;
  if (!isExactRecord(report, ["json", "markdown"]) || !isRecord(report.json) || typeof report.markdown !== "string" || report.markdown.length > 2_000_000) throw new Error("研究报告格式无效");
  return { ...record, report: { json: report.json, markdown: report.markdown } };
}

export function validResearchSecurityCode(value: string): boolean {
  return A_SHARE.test(value);
}

export function newSubmissionIdentity(prefix = "web"): string {
  const suffix = typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID().replace(/-/g, "")
    : `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${suffix}`.slice(0, 256);
}
