"""The CLI contract: commands, wire names, and discoverable input fields.

No application services are imported here. API coverage and the generated skill
reference are checked offline by ``python -m advisor.cli.maintenance --check``.
"""

from dataclasses import asdict, dataclass
import re


@dataclass(frozen=True)
class Field:
    name: str
    kind: str = "str"
    required: bool = False
    repeated: bool = False
    flag: str | None = None
    help: str = ""

    @property
    def option(self) -> str:
        return "--" + (self.flag or self.name.replace("_", "-"))


@dataclass(frozen=True)
class Command:
    command: str
    method: str
    path: str
    help: str
    query: tuple[Field, ...] = ()
    body: tuple[Field, ...] = ()
    json_only: str | None = None
    download: bool = False

    @property
    def path_fields(self) -> tuple[str, ...]:
        return tuple(re.findall(r"\{(\w+)\}", self.path))

    def describe(self) -> dict:
        return {
            **asdict(self),
            "mutates": self.method != "GET",
            "path_fields": list(self.path_fields),
            "query": [{**asdict(f), "option": f.option} for f in self.query],
            "body": [{**asdict(f), "option": f.option} for f in self.body],
        }


LIMIT = Field("limit", "int", help="Page size; API default and bounds apply")
OFFSET = Field("offset", "int", help="Zero-based offset")
CURSOR = Field("cursor", help="Opaque cursor from the previous page; keep filters unchanged")
IDENTITY = Field("submission_identity", required=True, help="Caller-chosen stable ID; reuse only for the same submission")
AUDIT = Field("audit_identity", required=True, help="Stable identity for this exact read; hidden detail commits owner exposure before release")
SCOPE = Field("scope", required=True, help="security or market; must match the selected Team")

COMMANDS = (
    Command("research experiments presets original-case", "GET", "/api/research/experiments/presets/original-case", "Read the editable original-case draft; this does not resolve data or enable execution"),
    Command("research experiments create", "POST", "/api/research/experiments", "Store an immutable draft; incomplete configuration is retained with validation errors", json_only="object", body=(Field("config", "object", required=True), IDENTITY)),
    Command("research experiments list", "GET", "/api/research/experiments", "List experiment draft identities", query=(LIMIT, OFFSET)),
    Command("research experiments show", "GET", "/api/research/experiments/{experiment_id}", "Read original configuration and validation errors; draft status is not execution status"),
    Command("research experiments preflight", "POST", "/api/research/experiments/{experiment_id}/preflight", "Append configuration/local-inventory readiness evidence; HTTP success can contain blocked", body=(IDENTITY,)),
    Command("research experiments preflights list", "GET", "/api/research/experiments/{experiment_id}/preflights", "List immutable preflight results", query=(LIMIT, OFFSET)),
    Command("research experiments bundles import", "POST", "/api/research/experiments/{experiment_id}/bundles/import", "Import an explicitly selected local historical bundle; validation is not formal source acceptance", body=(Field("root", required=True, help="Absolute local directory containing manifest.json and declared source/data files"), IDENTITY)),
    Command("research experiments definitions resolve", "POST", "/api/research/experiments/{experiment_id}/definitions/resolve", "Resolve complete config against sealed calendar bytes at initial research time; retain blocked attempts", json_only="object", body=(Field("config", "object", required=True), Field("calendar_bundle_id", required=True), IDENTITY)),
    Command("research experiments plans register", "POST", "/api/research/experiments/{experiment_id}/plans", "Atomically register the complete predeclared sample matrix; Tests remain created and are not queued", json_only="object", body=(Field("definition_id", required=True), Field("plan", "object", required=True), Field("purpose", required=True), Field("selection_id", required=True, help="Explicit null except the frozen final-holdout selection ID"), IDENTITY)),
    Command("research experiments selection show", "GET", "/api/research/experiments/{experiment_id}/selection", "Read current initial/selected baseline IDs and freeze state without hidden samples or exposure reports"),
    Command("research experiments selection initialize", "POST", "/api/research/experiments/{experiment_id}/selection/initialize", "Fix the root baseline before any Test starts; one canonical initialization identity per experiment", body=(Field("definition_id", required=True), Field("baseline_id", required=True, help="Original candidate_proposal record ID, not its local proposal name"))),
    Command("research experiments selection apply", "POST", "/api/research/experiments/{experiment_id}/selection/apply", "Commit a completed comparison decision against its original selection revision; eligibility alone never moves the baseline", body=(Field("comparison_id", required=True), Field("expected_selection_id", required=True), IDENTITY)),
    Command("research experiments selection finalize-holdout", "POST", "/api/research/experiments/{experiment_id}/selection/finalize-holdout", "Permanently freeze selection after terminal Tests, known cleanup and unseen holdout checks; does not run holdout", body=(Field("expected_selection_id", required=True), IDENTITY)),
    Command("research experiments candidates register", "POST", "/api/research/experiments/{experiment_id}/candidates", "Seal uploaded original bytes and register a distinct proposal; never executes code or creates Tests", json_only="object", body=(
        Field("manifest", "object", required=True, help="CandidateInput: declared relative file paths, entrypoint, contracts and allowed research config"),
        Field("files_base64", "object", required=True, help="Exact declared path to canonical base64 bytes map; API never reads host paths"),
        Field("proposal", "object", required=True, help="proposal_id, parent_proposal_id (nullable), hypothesis, source"),
        Field("diff_base64", required=True, help="Canonical base64 diff bytes or explicit null"), IDENTITY,
    )),
    Command("research experiments tree", "GET", "/api/research/experiments/{experiment_id}/tree", "Page proposal lineage, original Test status counts and baseline markers at fixed record/event ceilings", query=(LIMIT, CURSOR)),
    Command("research experiments candidates tests", "GET", "/api/research/experiments/{experiment_id}/candidates/{proposal_id}/tests", "Page every original and linked rerun Test for a proposal; outcomes and traces remain audited", query=(LIMIT, CURSOR)),
    Command("research experiments candidates comparisons", "GET", "/api/research/experiments/{experiment_id}/candidates/{proposal_id}/comparisons", "Page original comparison opponents and permitted aggregate feedback separately from proposal parentage", query=(LIMIT, CURSOR)),
    Command("research experiments selection history", "GET", "/api/research/experiments/{experiment_id}/selection/history", "Page original selection decisions and baseline changes without hidden exposure reports or samples", query=(LIMIT, CURSOR)),
    Command("research experiments candidates list", "GET", "/api/research/experiments/{experiment_id}/candidates", "List typed original proposal metadata with a stable cursor; no Test outcomes", query=(LIMIT, CURSOR)),
    Command("research experiments candidates show", "GET", "/api/research/experiments/{experiment_id}/candidates/{proposal_id}", "Read a local proposal ID, verified package manifest and original parent links; no execution permission"),
    Command("research experiments records list", "GET", "/api/research/experiments/{experiment_id}/records", "List record metadata and Test status without exposing hidden detail", query=(Field("kind"), LIMIT, CURSOR)),
    Command("research experiments records show", "POST", "/api/research/experiments/{experiment_id}/records/{record_id}/detail", "Read an original record with durable hidden-scope exposure auditing", body=(AUDIT,)),
    Command("research experiments tests events", "POST", "/api/research/experiments/{experiment_id}/tests/{test_id}/events", "Read original attempts, phase/account/cost observations and lifecycle events; hidden reads are audited", query=(Field("after", "int"), LIMIT), body=(AUDIT,)),
    Command("research experiments tests cancel", "POST", "/api/research/experiments/{experiment_id}/tests/{test_id}/cancel", "Cancel an unstarted Test or durably request shared-service cleanup; 202 remains pending", body=(IDENTITY, Field("reason", required=True))),
    Command("research experiments tests rerun", "POST", "/api/research/experiments/{experiment_id}/tests/{test_id}/rerun", "Register a new linked Test from a terminal original; remains created and cannot replace original comparison samples", body=(IDENTITY, Field("reason", required=True))),
    Command("research experiments records export", "POST", "/api/research/experiments/{experiment_id}/records/{record_id}/export", "Download an audited record manifest with original links, artifact hashes and retention availability", body=(AUDIT,), download=True),
    Command("research experiments records artifact", "POST", "/api/research/experiments/{experiment_id}/records/{record_id}/artifacts/{artifact_hash}", "Download linked original bytes with audit and retention checks", body=(AUDIT,), download=True),
    Command("research experiments comparisons feedback", "GET", "/api/research/experiments/{experiment_id}/comparisons/{record_id}/feedback", "Read permitted aggregate selection feedback; final-holdout and unknown scopes are rejected"),
    Command("research experiments comparisons freeze", "POST", "/api/research/experiments/{experiment_id}/comparisons", "Freeze the original sample pair and declared conditions before execution; uploaded evidence does not confer runtime acceptance", json_only="object", body=(Field("plan_id", required=True), Field("baseline", required=True), Field("candidate", required=True), IDENTITY,
        Field("previous_comparison", required=True, help="Prior inconclusive result ID for a new plan, or explicit null"), Field("expected_selection_id", required=True),
        Field("runtime_conditions", "object", required=True, help="Exact task-to-condition mapping, or explicit null for an unqualified comparison"),
        Field("runtime_artifacts_base64", "object", required=True, help="Exact referenced hash-to-canonical-base64 original bytes map; empty when conditions are null"))),
    Command("research experiments comparisons complete", "POST", "/api/research/experiments/{experiment_id}/comparisons/{record_id}/complete", "Derive one immutable result after all original Tests are terminal; returns aggregate feedback, not audited sample detail"),
    Command("research experiments calibrations freeze", "POST", "/api/research/experiments/{experiment_id}/calibrations", "Freeze original baseline calibration conditions before execution; one canonical campaign per definition", json_only="object", body=(
        Field("plan_id", required=True), Field("expected_selection_id", required=True),
        Field("table", "object", required=True, help="Versioned comparison-equivalent USD PriceTable declaration"),
        Field("envelopes", "array", required=True, help="ResourceEnvelope for every predeclared task"),
        Field("artifacts_base64", "object", required=True, help="Every referenced evidence hash and its exact canonical-base64 original bytes"))),
    Command("research experiments calibrations complete", "POST", "/api/research/experiments/{experiment_id}/calibrations/{record_id}/complete", "Derive immutable allocations from all original completed and metered calibration repeats; detail requires audited record reads"),
    Command("research lagent settings get", "GET", "/api/research/lagent/settings", "Read versioned LAgent configuration"),
    Command("research lagent settings update", "PUT", "/api/research/lagent/settings", "Publish complete LAgent configuration with optimistic concurrency", json_only="object", body=(
        Field("expected_version", "int", required=True), Field("config", "object", required=True),
    )),
    Command("research lagent requests create", "POST", "/api/research/lagent/requests", "Queue free LAgent research with one main investigator and dynamic subagents", body=(
        SCOPE, Field("code"), Field("task", required=True), IDENTITY, Field("expected_version", "int", required=True),
    )),
    Command("research lagent requests trace", "GET", "/api/research/lagent/requests/{request_id}/trace", "Read pinned configuration, task tree and paginated invocation/data/query events", query=(
        Field("after", "int"), LIMIT,
    )),
    Command("research lagent requests artifact", "GET", "/api/research/lagent/requests/{request_id}/artifacts/{artifact_hash}", "Download an integrity-checked input or result linked to this request's trace", download=True),
    Command("health", "GET", "/api/health", "Read collector health"),
    Command("current-state", "GET", "/api/current-state", "Read dashboard state and quality"),
    Command("services status", "GET", "/api/services", "Read service status without starting services"),
    Command("mx listener status", "GET", "/api/mx/listener/status", "Read passive Listener status"),
    Command("mx listener start", "POST", "/api/mx/listener/start", "Request Listener start; never starts Chrome"),
    Command("mx listener stop", "POST", "/api/mx/listener/stop", "Request Listener stop"),
    Command("mx chrome start", "POST", "/api/mx/chrome/start", "Explicitly start the fixed dedicated Chrome; no login or navigation"),
    Command("mx rids get", "GET", "/api/mx/rids", "Read authorized RIDs and configuration version"),
    Command("mx rids replace", "PUT", "/api/mx/rids", "Replace the full RID set with user-supplied values; JSON rids: [] explicitly clears it", body=(
        Field("version", required=True, help="Current version from mx rids get"),
        Field("rids", "int", required=True, repeated=True, flag="rid", help="Authorized RID; repeat for every RID to retain"),
    )),
    Command("mx events list", "GET", "/api/mx/events", "Search and paginate accepted MX events", query=(
        LIMIT, CURSOR, Field("rid", "int", repeated=True), Field("authorization"),
        Field("start_at", help="Epoch milliseconds or ISO datetime"), Field("end_at", help="Epoch milliseconds or ISO datetime"),
        Field("has_media", help="true or false"), Field("q", help="Search text"),
    )),
    Command("mx events get", "GET", "/api/mx/events/{event_id}", "Read one stored event"),
    Command("mx media download", "GET", "/api/mx/events/{event_id}/media/{media_id}", "Save original media bytes to a new file", download=True),
    Command("market-daily status", "GET", "/api/market-daily/status", "Read Market Daily status"),
    Command("market-daily cold-start", "POST", "/api/market-daily/cold-start", "Queue the idempotent five-year cold start for execution after 21:00"),
    Command("market-daily runs list", "GET", "/api/market-daily/runs", "List market ingestion runs", query=(LIMIT,)),
    Command("market-daily runs get", "GET", "/api/market-daily/runs/{run_id}", "Read one market ingestion run"),
    Command("market-daily runs failures", "GET", "/api/market-daily/runs/{run_id}/failures", "Read paginated source failures", query=(LIMIT, OFFSET)),
    Command("reports list", "GET", "/api/reports", "List verified report archives", query=(LIMIT, CURSOR, Field("start_date"), Field("end_date"))),
    Command("reports get", "GET", "/api/reports/{report_date}/{report_type}", "Read a verified premarket or review archive", query=(Field("run_id", help="Defaults to initial; use the run_id from the listing"),)),
    Command("research cycles list", "GET", "/api/research/cycles", "List legacy cycle artifacts", query=(LIMIT,)),
    Command("research cycles get", "GET", "/api/research/cycles/{report_date}/{cycle_id}", "Read a legacy cycle artifact"),
    Command("research execution-settings get", "GET", "/api/research/execution-settings", "Read execution policy and available models"),
    Command("research execution-settings update", "PUT", "/api/research/execution-settings", "Update model settings using the current policy reference", body=(
        Field("expected_policy_ref", required=True), Field("model", required=True), Field("reasoning_effort", required=True),
    )),
    Command("research agents list", "GET", "/api/research/agents", "List published Research Agents"),
    Command("research data-catalog", "GET", "/api/research/data-catalog", "List published Data Products"),
    Command("research agent-access", "GET", "/api/research/agent-access", "Read Agent data access and RID version"),
    Command("research agents revise-access", "POST", "/api/research/agents/{agent_ref}/access-revisions", "Publish an access revision; existing Teams remain pinned", json_only="object", body=(
        Field("rid_version", required=True), Field("data_access", "array", required=True, help="[{product, feed_scope?: {rids: [...]}}]"),
    )),
    Command("research agents revise-instructions", "POST", "/api/research/agents/{agent_ref}/instruction-revisions", "Publish instructions from a UTF-8 file; existing Teams remain pinned", body=(
        Field("instructions", "text-file", required=True, flag="instructions-file", help="UTF-8 file, or - for stdin; JSON input uses the instructions string"),
    )),
    Command("research teams list", "GET", "/api/research/teams", "List Teams and daily enablement"),
    Command("research teams create", "POST", "/api/research/teams", "Publish a Team with an explicit non-empty Agent selection", body=(
        Field("team_id", required=True), Field("title", required=True), SCOPE,
        Field("agent_ids", required=True, repeated=True, flag="agent-id", help="Published Agent ID; repeat for each member"),
    )),
    Command("research requests create", "POST", "/api/research/requests", "Queue research; HTTP 202 means accepted, not completed", body=(
        Field("team_ref", required=True), SCOPE, Field("code", help="Six-digit stock code; omitted for market scope (sends null)"), IDENTITY,
    )),
    Command("research requests current", "GET", "/api/research/requests/current", "Read queued/running requests and worker state"),
    Command("research requests get", "GET", "/api/research/requests/{request_id}", "Read request status, including terminal no-report states"),
    Command("research requests cancel", "POST", "/api/research/requests/{request_id}/cancel", "Request cancellation of unfinished research"),
    Command("research requests rerun", "POST", "/api/research/requests/{request_id}/rerun", "Queue a new run of a terminal request", body=(IDENTITY,)),
    Command("research records list", "GET", "/api/research/records", "List research records; default statuses are passed and partial", query=(
        Field("team_id"), Field("team_ref"), Field("status", repeated=True), LIMIT, OFFSET,
    )),
    Command("research records get", "GET", "/api/research/records/{record_id}", "Read a record and integrity-checked report when available"),
    Command("research daily-teams enable", "PUT", "/api/research/daily-teams/{team_ref}", "Enable this exact Team version for daily research"),
    Command("research daily-teams disable", "DELETE", "/api/research/daily-teams/{team_ref}", "Disable this exact Team version for daily research"),
    Command("profiles list", "GET", "/api/profiles", "List available stock profiles", query=(LIMIT,)),
    Command("profiles get", "GET", "/api/profiles/{code}", "Read a six-digit stock profile"),
    Command("charts list", "GET", "/api/charts", "List available chart assets", query=(LIMIT,)),
    Command("charts download", "GET", "/api/charts/{asset_id}", "Save a chart image to a new file", download=True),
    Command("ledger transactions list", "GET", "/api/ledger/transactions", "Read recorded transactions and ledger state", query=(LIMIT, OFFSET)),
    Command("ledger transactions add", "POST", "/api/ledger/transactions", "Record an existing transaction; never submits an order", body=(
        Field("transaction_id", required=True), Field("account_id"), Field("trade_date", required=True),
        Field("transaction_type", required=True, help="cash_deposit, cash_withdrawal, buy, sell, fee, or tax"),
        Field("code"), Field("quantity", "int", required=True), Field("price", "float", required=True),
        Field("amount", "float", required=True), Field("fees", "float", required=True),
    )),
    Command("ledger import", "POST", "/api/ledger/import", "Import a JSON array of existing transactions (not CSV or orders)", json_only="array"),
)
