# A Hunter CLI workflows

All examples use `rtk ahunter`. Read the project `agents.md` and use its runtime
entry point when `ahunter` is not on PATH. Substitute IDs and versions returned
by the API; example values are not authorization to run research or modify data.

## Read status and events

```bash
rtk ahunter services status
rtk ahunter current-state
rtk ahunter mx listener status
rtk ahunter mx events list --limit 20 --q '检索词' --has-media true
rtk ahunter mx events get EVENT_ID
rtk ahunter mx media download EVENT_ID MEDIA_ID --output /absolute/new-image.png
```

Use repeated `--rid` values to filter known RIDs, `--start-at`/`--end-at` for
timestamps, and `--cursor` with exactly the same filters for the next page.
Media download preserves original bytes and returns path, size, SHA-256, and
content type. The parent directory must exist; choose a new destination if one
already exists. `charts download ASSET_ID --output PATH` has the same behavior.

## Submit and inspect research

Read `research teams list` for an available exact `team_ref` and its scope.
For a user-selected security and matching Team:

```bash
rtk ahunter research requests create --team-ref TEAM@VERSION --scope security --code 000001 --submission-identity USER_TASK_ID
rtk ahunter research requests get REQUEST_ID
rtk ahunter research requests current
rtk ahunter research records list --team-ref TEAM@VERSION --status passed --status partial
rtk ahunter research records get RECORD_ID
```

For market research, use `--scope market` and omit `--code`; the CLI sends null.
The identity must be stable across retries of the same intended submission.
Persist it in the task context before submitting. The CLI does not choose a
security, Team, identity, or model for the user. Select those within the task's
scope using the returned catalog; do not invent identifiers.

HTTP 202 queues a request. Inspect its status and phase, and inspect the worker
state if it remains queued. The CLI does not start the worker automatically.
Poll `requests get` at a reasonable interval when the task requires waiting;
stop on a terminal state or the task's time limit. Failed, blocked, or cancelled
requests may have no report. `records list` defaults to passed and partial;
explicitly filter other statuses when investigating failures.

`research requests cancel REQUEST_ID` requests cancellation. For an intentional
rerun of a terminal request, use `research requests rerun REQUEST_ID
--submission-identity NEW_TASK_ID`. Read the newly returned request ID.

`market-daily cold-start` queues the one idempotent five-year backfill for the
service to execute after 21:00. Use `market-daily status`, `market-daily runs list`,
`market-daily runs get RUN_ID`, and `market-daily runs failures RUN_ID` to inspect
progress and source failures. Acceptance does not mean market data is ready.
Run completion/failure counts update with each committed security outcome;
resuming a partial run retains completed counts and unresolved conflicts while
requeuing source-missing items. A 100% progress value includes failed outcomes
and does not imply `run_status=complete` or research readiness.

## Change configuration

Read first, preserve version tokens, and submit the complete intended change.
No command automatically rereads and overwrites state after a conflict.

- RIDs: `mx rids get` returns `version`. `mx rids replace --version VERSION
  --rid AUTHORIZED_RID --rid ANOTHER_AUTHORIZED_RID` replaces the entire set.
  Explicit clearing requires JSON `{"version":"CURRENT_VERSION","rids":[]}`.
- Model settings: read `research execution-settings get`, then use
  `research execution-settings update --expected-policy-ref CURRENT_REF
  --model MODEL --reasoning-effort EFFORT`. Use available values from the read.
- Team: `research teams create --team-id TEAM_ID --title TITLE --scope security
  --agent-id AGENT_ID --agent-id ANOTHER_AGENT_ID`. Use `market` for market Teams.
  Any non-empty selection of compatible published Agents is valid; do not require
  a universal Agent. Publication does not enable daily execution.
- Daily enablement: `research daily-teams enable TEAM@VERSION` or
  `research daily-teams disable TEAM@VERSION` affects that exact version.
- Instructions: `research agents revise-instructions AGENT@VERSION
  --instructions-file /path/instructions.md`, or `--instructions-file -` for
  stdin. With `--json`, use `{"instructions":"..."}` instead of a filename.
- Data access: read `research data-catalog` and `research agent-access`. Submit
  `research agents revise-access AGENT@VERSION --json /path/access.json`:

```json
{
  "rid_version": "CURRENT_64_CHARACTER_RID_VERSION",
  "data_access": [
    {"product": "PUBLISHED_PRODUCT@VERSION"},
    {"product": "PUBLISHED_MX_PRODUCT@VERSION", "feed_scope": {"rids": [123]}}
  ]
}
```

Use actual product references and only user-authorized feed RIDs. Agent revisions
publish new versions; existing Teams remain pinned and daily enablement stays
unchanged. Inspect the returned impact and update Teams only within the request.

## Record existing ledger transactions

These commands update the local ledger; they never submit broker orders.
`ledger transactions add` accepts named flags or a complete JSON object.
`ledger import --json /path/transactions.json` accepts a JSON array, not CSV.

Each row requires `transaction_id`, `trade_date` (YYYY-MM-DD),
`transaction_type` (`cash_deposit`, `cash_withdrawal`, `buy`, `sell`, `fee`, `tax`),
integer `quantity`, and numeric `price`, `amount`, `fees`. Optional `account_id` defaults
to `default`; `code` is a string for security transactions. Supply the actual
recorded values, retain stable transaction IDs, and inspect conflicts instead
of issuing a duplicate with a different ID. Buy amounts are negative and sell
amounts positive, with `abs(amount) = quantity * price`; fees are separate.
Cash deposits are positive; withdrawals, fees, and taxes are negative. Cash-only
rows have no code and use zero quantity, price, and fees.

## Reports and failures

`reports list` returns archive dates, types, run IDs, and pagination. Use
`reports get DATE TYPE --run-id RUN_ID` for the selected entry; omitting run ID
selects `initial`. Legacy `research cycles` and newer `research records` are
distinct existing API surfaces. Prefer records for current research tasks.

CLI exit 0 means the HTTP request succeeded; always inspect business status and
quality inside `data`. HTTP 409 usually requires refreshed configuration,
cursor, or task state. HTTP 503 can indicate an unavailable source or a quality
gate, not merely a stopped process. Diagnose the returned detail; do not bypass
the gate by reading an unchecked report or creating a substitute conclusion.

Chinese publication checks evaluate prose separately from versioned product
references, structured evidence IDs and HTTP(S) source URLs. A reference alone is not Chinese prose;
English paragraphs remain blocked. Language-policy fixes apply to new runs,
and do not rewrite prior failed attempts or published artifacts.

For Agents using host-mediated historical queries, final Findings may combine
the audited query results with other declared products supplied directly in
the same sealed Capsule (such as intraday quotes and industry taxonomy).
Historical backing rows remain accessible only through audited host queries;
the query phase does not revoke access to other declared products.

Research Agent queries no longer enforce a query-count, result-row, result-byte,
security-count, rank-size, or session-window budget. API budget fields return
`null` for unlimited. Published YAML and old artifacts remain unchanged; legacy
numeric Agent budget fields do not limit new runs. The host still executes only
declared Products in a sealed Snapshot and records every query and its full
result totals. User-selected query predicates and rank sizes remain meaningful;
no hidden budget trims their results. Control-only Capsules still disable query
execution, and access/data-quality failures still block the affected Agent.


Market publication checks treat structured evidence provenance separately
from research prose: a listed data provider's source name does not become the
target of an action in an unrelated paragraph. Every source/excerpt field is
still checked for explicit advice, and unsafe evidence blocks its dependent
Insight. Do not bypass these checks or rewrite a terminal report after a fix;
submit a new run.

Market output checks recognize full timestamps, so fractional seconds are not
misclassified as six-digit security codes. Real security names/codes elsewhere
in the same output remain subject to the existing Market-scope checks.
## LAgent free research

1. Read `research lagent settings get`. Defaults permit every published Data Product
   and arbitrary task-specific subagent roles; only the main investigator delegates.
   Delegations run sequentially and return to the same logical main conversation.
2. To change defaults, send `research lagent settings update --json FILE` with
   `{"expected_version":CURRENT_VERSION,"config":COMPLETE_CONFIG}`. Configure main
   and subagent instructions, model, reasoning effort, invocation timeout/retry,
   allowed product versions, MX RID selection, intraday prefetch, step/query/subagent
   counts, per-query result size, and total runtime. Readable version conflicts
   return HTTP 409. `allowed_products:null` selects the whole catalog; `[]` selects
   none. `mx_rids:null` pins the existing owner-authorized set at submission; `[]`
   disables scoped MX access. Explicit RIDs must already be owner-authorized.
3. Submit `research lagent requests create --scope market --task '研究问题'
   --expected-version CURRENT_VERSION --submission-identity UNIQUE_ID`.
   For a security subject use `--scope security --code 600519`. No Team is required.
   HTTP 202 only confirms durable queue acceptance. Request/Record `mode` is `lagent`;
   the legacy `team_ref` field carries a compatibility execution identity, not a Team.
4. Read `research requests get REQUEST_ID` and
   `research lagent requests trace REQUEST_ID --after 0 --limit 100`.
   Continue with `next_after` until drained; poll while queued/running. Observe
   parent/child IDs, public task/action descriptions, product and result hashes,
   queries, model/reasoning settings, elapsed time, CLI token usage (empty when
   unavailable), retries and errors. Traces never contain private model reasoning.
   Download a trace-linked `artifact_hash` or `capsule_hash` with `research lagent
   requests artifact REQUEST_ID HASH --output NEW_FILE.json`; only artifacts linked
   to that exact request are available, and hashes are verified before download.
5. Cancel with `research requests cancel REQUEST_ID`. Rerun a terminal request
   with `research requests rerun REQUEST_ID --submission-identity NEW_ID`; its
   LAgent configuration and already-pinned model policy are retained, with a new
   evidence boundary and fresh data. Read successful reports through
   `research records get RECORD_ID`; list history with `research records list
   --team-id lagent --status passed --status blocked --status failed --status cancelled`.

Configuration and accessible product versions are pinned at acceptance. Inherited
model defaults resolve and pin when execution begins. Every completed action and
sealed data result is durable and reused on interrupted-request recovery. A process
crash before an action result is recorded can repeat that unfinished read/model call.
No data-quality failure produces a replacement conclusion. Optional intraday prefetch
seals the live snapshot before model latency; if capture fails, selecting that product
blocks the request instead of fetching newer evidence under the old boundary.


## Historical LAgent experiments (development status)

The historical experiment platform is being implemented separately from business
LAgent research. LE-001 supplies the versioned `original-case.v1.json` preset,
configuration resolution and candidate package sealing. Draft and evidence APIs
and candidate registration, bundle import, definition resolution and plan registration
are now public; execution remains an internal interface.
Do not submit an ordinary business LAgent request as a historical Episode.

The `research experiments` command group currently provides:

- `presets original-case` returns `{config, execution_available:false}`. Edit the
  config and pass `{config, submission_identity}` via `create --json FILE`.
  `list`/`show EXPERIMENT_ID` retain original input, validation errors and draft status.
- `preflight EXPERIMENT_ID --submission-identity ID` appends configuration/local
  inventory observations; `preflights list` pages original reports. Current reports
  are blocked, with zero model calls/orders and null return. Repeating the same ID
  returns the original report without re-inspecting changed inventory. A fresh
  preflight uses a new ID. This is not a bundle coverage or execution acceptance test.
- `records list EXPERIMENT_ID --kind KIND` returns bounded metadata/Test statuses.
  Follow `next_cursor` with unchanged kind/experiment; the cursor keeps the original
  sequence ceiling and does not include later registrations. No hidden detail is
  released or exposure recorded by this overview.
- `records show EXPERIMENT_ID RECORD_ID --audit-identity ID` reads exact original
  records. `tests events EXPERIMENT_ID TEST_ID --after N --limit N --audit-identity ID`
  reads original lifecycle, attempt and financial/cost events using `next_after`.
  These are POST operations: hidden-scope exposure commits before release. Use a
  fresh audit identity for each distinct action/target/page; retries of the same
  exact read can reuse their identity. GET cannot bypass the audit.
- `records export EXPERIMENT_ID RECORD_ID --audit-identity ID --output FILE`
  downloads a JSON manifest with original record/links, hashes and source retention
  availability. `records artifact EXPERIMENT_ID RECORD_ID HASH --audit-identity ID
  --output FILE` downloads linked exact bytes. Existing files are never overwritten;
  expired originals return HTTP 410, unlinked/cross-experiment records 404, integrity
  or identity conflicts 409, and unavailable audit storage 503 without hidden bytes.
- `comparisons feedback EXPERIMENT_ID RECORD_ID` returns only the existing numeric/
  decision allowlist. Final-holdout and unknown scopes are denied. Eligible does not
  mean promoted; this endpoint does not change selection.

`candidates register EXPERIMENT_ID --json FILE` accepts exactly `manifest`,
`files_base64`, `proposal`, `diff_base64`, and `submission_identity`. The manifest
contains source_paths, prompt_paths, dependency_lock_paths, contract_paths,
entrypoint, input_contract, output_contract and allowed_config. Every declared path
must occur exactly once in the files_base64 map, with canonical base64 encoding of
the original bytes. All four file kinds are required. Paths must be normalized
relative names, with no traversal, backslash, NUL or file/directory collision;
the API never reads host paths. Request size remains bounded to 1,000,000 bytes.
The proposal object contains proposal_id, nullable parent_proposal_id, hypothesis
and source (manual/coding_task/optimizer provenance only). It cannot supply package
or diff hashes; the host derives these. diff_base64 must be canonical bytes or null.
No source code, dependency install, model call or Test runs during registration.

Package and proposal records/links commit atomically using the existing registry.
Identical package bytes preserve one package identity while different proposals
retain separate lineage. Missing parents, self-parenting, changed same-identity
input and new proposals after selection freeze are rejected; a retry of an already
registered proposal still returns its original records after freeze. Files sealed
before a database failure may remain unreferenced, but no partial proposal history
is published. Never create a new identity blindly after a failed write.
`candidates list EXPERIMENT_ID` pages typed proposal metadata with the original
cursor ceiling; `candidates show EXPERIMENT_ID PROPOSAL_ID` takes the local name,
verifies package bytes and original links, and returns record IDs for existing
manifest/diff/source downloads. These reads expose no hidden Test outcomes.

`bundles import EXPERIMENT_ID --root ABSOLUTE_DIRECTORY --submission-identity ID`
reads the explicitly selected local directory's manifest.json and only declared
source/data files. Files are sealed and indexed atomically; invalid manifests/files/
rows return HTTP 400 with blocked issues and a failure_record_id when original
evidence could be captured. Inspect that ID with records show/export. Missing input
directories or unreadable manifests cannot supply a sealed failure record. Sealing
runs in a worker thread so other API requests remain responsive; the import request
waits for its result, and is not a queued Test. After a timeout inspect records
list/show before resubmitting. Imports
do not prove real source acceptance; fixture_only/pending_independent_review remain.

`definitions resolve EXPERIMENT_ID --json FILE` requires exactly config,
calendar_bundle_id and submission_identity. Submit the complete intended config;
the original draft is unchanged. The host checks sealed calendar bytes against the
index and derives cutoffs from each task's initial research time. It rejects late,
missing or mismatched calendars without shortening the requested period. HTTP 200
contains status resolved or blocked, structured errors and an immutable resolution
record; only resolved includes a definition. The calendar source, cutoff and original
config remain recorded. Same-identity retries return the original decision; changed
inputs conflict. Use a new identity for a new inspection after fixing evidence.

`plans register EXPERIMENT_ID --json FILE` requires definition_id, plan, purpose,
selection_id and submission_identity. The plan includes plan_id, specification_hash,
seed_support and every predeclared test_id/proposal_id/task_id/repeat_id/repeat_index/
seed. Preserve the complete candidate × task × repeat matrix and interleaved order.
Purpose is tuning, selection_validation, calibration or final_holdout; selection_id
is explicit null except for an existing finalized holdout selection. The original
registry checks role/repetition/seed rules, candidate bytes and freeze eligibility.
Plan and all Tests commit together. HTTP 201 returns original IDs and
execution_available:false; Tests remain created, with no shared queue work. Viewing
the returned Tests uses records list/show and tests events. New optimization
definitions/plans are rejected after freeze; identical registered retries remain.

`tests cancel EXPERIMENT_ID TEST_ID --submission-identity ID --reason TEXT` preserves
an immutable owner request. For a never-admitted Test, cancellation and its receipt
commit atomically. A missing scheduling index for an admitted Test or previous
worker ownership requires reconciliation; the endpoint cannot declare physical
cleanup from missing metadata. Shared queued/running/evaluating Tests receive a
durable queue cancellation intent and HTTP 202. Waiting evaluation is returned to
the original service, which closes it without replaying the Episode or scoring it.
Unknown process cleanup remains nonterminal. HTTP 200 returns the existing terminal
status, including completed/failed/blocked; cancellation does not rewrite outcomes.
The response includes request, test_id, status, terminal, cancel_pending and
queue_state. Retrying preserves the request but observes current lifecycle state;
changed inputs under the same identity conflict. Inspect events with a fresh audit
identity when reading another page; cancellation itself does not expose hidden
market results, costs or attempts. It does not start a service or new executor.
Reading/exporting the exact cancellation receipt retains an audit but counts as
metadata for holdout exposure. This requires the original control fields, exactly
one typed Test link and no attached artifacts; added result fields, other links or
artifacts are not exempt from exposure tracking.

`tests rerun EXPERIMENT_ID TEST_ID --submission-identity ID --reason TEXT` returns
HTTP 201 with a new linked Test and its current status. The original must be terminal.
Original task/candidate/sample/plan links remain, and eligible_for_original_comparison
is false: it cannot fill or replace a missing predeclared sample. The new Test starts
created and is not queued. New reruns are forbidden after freeze, and final-holdout
Tests cannot add unplanned reruns. Identical registered reruns still return their
original new ID after freeze or after they themselves have finished. Cancellation
remains available after freeze. Use records list/show and tests events for inspection.

`selection initialize EXPERIMENT_ID --definition-id ID --baseline-id RECORD_ID`
fixes the root baseline before any Test has started or created a cost budget. The
baseline_id is the candidate_proposal record ID returned by candidate registration,
not a local proposal name. Input is exactly definition_id and baseline_id. There is
one canonical initialization identity per experiment, so no submission_identity
flag is accepted. Repeating the same inputs returns the original record, including
after freeze; original candidate bytes are still checked. Changed inputs, child
candidates, late initialization or cross-experiment IDs are rejected.

`selection show EXPERIMENT_ID` returns selection:null before initialization, or the
current selection ID/hash/sequence/time, definition, initial/current candidate and
package IDs, previous selection ID, operation, frozen and formal_ready. It exposes
no sample results, comparison detail or exposure report and writes no audit. The
original full selection record is available through the existing audited record
commands. formal_ready is the stored selection field, not global run admission;
execution_available remains false.

`selection apply EXPERIMENT_ID --comparison-id ID --expected-selection-id ID
--submission-identity ID` accepts a completed comparison record ID. The API permits
only existing tuning/selection feedback scopes, and the controller recomputes the
original evidence qualification before committing. Eligibility alone is not a
promotion. A stale baseline, incomplete runtime acceptance, poor or unstable results
remain explicit decisions with an unchanged pointer. HTTP 200 returns decision and
execution_available:false; inspect decision.value.decision and then selection show.
Every comparison has one immutable selection decision. Identical retries return it;
changing its request or rebinding the comparison to another selection conflicts.
Concurrent qualifying comparisons can advance a given baseline only once.

`selection finalize-holdout EXPERIMENT_ID --expected-selection-id ID
--submission-identity ID` checks the exact current revision, terminal planned Tests,
known process cleanup, original candidate bytes and unseen dated holdout provenance.
Success freezes selection permanently and enables one complete holdout plan for the
initial/current candidates under the same definition, via plans register. It neither
creates nor queues the holdout Tests itself. Exposure occurring before plan admission
is checked again. After freeze, new optimization candidates/definitions/plans/reruns
are rejected; old immutable retries and cancellation remain available. Lost replies
must be reconciled with the same identity and selection show, without inventing a new
freeze request. Selection writes run their verification in worker threads; they
still wait for committed results and do not represent queued research jobs.

The API never accepts supplied assessment results or formal acceptance flags.
Positive promotion tests use synthetic accepted-assessor contracts solely to verify
the consumer and CAS boundary. Actual fixture Episode assessments still cannot
qualify for production promotion or formal historical execution.

`comparisons freeze EXPERIMENT_ID --json FILE` freezes a registered tuning or
selection-validation pair before any original sample starts or creates a cost
budget. Input is exactly plan_id (record ID), baseline and candidate (local proposal
names), submission_identity, previous_comparison (result ID or null),
expected_selection_id, runtime_conditions and runtime_artifacts_base64. The expected
selection is required and must still be the current baseline at commit. A repeated
original freeze retains its identity even after that baseline later changes.

runtime_conditions is either null or an exact task map of TaskComparisonConditions:
version, data_corpus_hash/generation, search_policy_hash, actual_model_id,
actual_reasoning_effort, executor_hash, fee_schedule_hash, price_table_hash,
budget_hash and scoring_version. These are declarations for later matching against
host measurements. Every *_hash needs its exact original bytes in
runtime_artifacts_base64, keyed by SHA-256 and encoded as canonical base64. Existing
stored hashes still require bytes; knowing another record's hash never creates a
new permanent download link. Unknown/extra/missing hashes, invalid bytes and hash
mismatches fail before upload writes. Null conditions require an empty map and do
not support formal promotion. Uploads remain bounded to the API's 1,000,000-byte
request limit. Original bytes are not re-encoded or treated as host paths.

`comparisons complete EXPERIMENT_ID RECORD_ID` uses the frozen comparison's canonical
result identity, sends an empty JSON object and accepts no result/score/acceptance
inputs. A new result requires all predeclared Tests terminal under the same writer
lock; unfinished samples return 409 without a result. Terminal failures, missing
assessments or unqualified runtime evidence produce an immutable inconclusive
result where applicable, not invented zero returns. Each Test contributes only its
first original assessment; later rescores do not replace it. Identical calls return
the original result, including a previously saved internal snapshot.

The response contains comparison identity/hash/status/qualification flags,
execution_available:false, and only the existing aggregate feedback allowlist.
There are no sample arrays, invalid-sample details, task scores, traces or assessment
payloads in this response. Use the returned result ID with audited records show/
export for those details. A completed comparison does not complete Tests or promote
the baseline; selection apply is the separate pointer mutation. Unknown/holdout
scopes are denied both for completion and for previous_comparison repair links.
Repair requires a new plan linked to an original inconclusive result. Conditions,
identity and sample sets cannot be changed in place. These writes run in worker
threads and wait for a committed result; they never enqueue or execute an Episode.

Calibration conditions and results use two public commands:

`calibrations freeze EXPERIMENT_ID --json FILE` posts plan_id (original typed
calibration TestPlan), expected_selection_id (original initialized selection),
table (PriceTable), envelopes (ResourceEnvelope list for every task) and
artifacts_base64 (exact evidence hash-to-canonical-base64 original bytes map).
The selected definition and root baseline must match the original sample plan.
New campaigns require the current open initial selection and all definition Tests
still created without a budget. Candidate bytes are reverified. The campaign is
canonical per definition; do not add submission_identity. Exact retries preserve
the original campaign after execution or selection freeze; changed conditions
cannot reprice the same campaign.

All table source, tariff usage-semantics and envelope replay evidence hashes must
have uploaded originals, including hashes already in the store. Extra/missing
entries and mismatched bytes are rejected before sealing any upload bytes.
The host validates declared prices, all task scales, full no-model replay/evaluation
claims, evidence JSON equality and reserves against the sealed specification.
The existing 1,000,000-byte JSON limit applies. These declarations do not establish
actual historical, provider or hard-budget acceptance.

`calibrations complete EXPERIMENT_ID RECORD_ID` sends empty JSON automatically
and takes the campaign record ID. It accepts no costs, allocations or success flags.
Only the original complete repeat set with completed Tests, all phase closures and
settled research/environment/evaluation costs can derive a result. HTTP 409 with
detail.status=blocked, detail.reason and execution_available=false preserves missing
or unsettled cost reasons; inspect original evidence before retrying. Failed samples
are not dropped or replaced by linked reruns. Complete uses the maximum original
research cost and the sealed multiplier, phase counts, reserves and final upward
rounding; result retries retain the original allocations and measurement identities,
even when later reconciliation adds facts.

Both mutations run in worker threads and return calibration record identity, hash,
sequence, creation time, cost_record_type and formal_ready=false. They return no
measurements, per-task allocations or source detail and create no exposure record.
Use audited records show/export for that detail; whole-definition resource evidence
includes hidden scopes and may affect holdout exposure. No model, provider, Test or
queue work starts. A successful fixture derivation does not enable production runs.

Evolution history is now available through four bounded GET commands:

- `tree EXPERIMENT_ID --limit 50` returns proposal nodes with local/record parent IDs,
  source, hypothesis, diff hash and package hash, original Test counts by state/role,
  and separate initial/current baseline markers. Same package bytes do not merge
  proposals. A failed parent's descendants remain visible.
- `candidates tests EXPERIMENT_ID PROPOSAL_ID` lists every original Test and linked
  rerun once with its original plan/task/repeat/purpose, status, rerun_of and
  eligible_for_original_comparison. Reused baseline references do not add Tests.
- `candidates comparisons EXPERIMENT_ID PROPOSAL_ID` lists frozen original pairs,
  opponent IDs, plan and selection IDs, nullable canonical result ID and only the
  existing permitted aggregate feedback. Parentage never follows the opponent.
  Awaiting results have feedback:null; inconclusive numbers remain null.
- `selection history EXPERIMENT_ID` pages initialization, promotion, retained
  decisions and final freeze with original comparison links and allowlisted reason
  codes. A retained decision does not move the current baseline marker.

Each response has items, total, next_cursor, the snapshot's current selection,
snapshot.records_through/events_through and execution_available=false. Each list
captures one read transaction and pins both ceilings across its pages. Later Tests,
events, comparison results or baseline changes only appear when starting a new
query without a cursor. Each node list starts its own snapshot; inspect that
snapshot when comparing it with an earlier tree count. Limits are 1–200, default
50; cursors cannot switch experiment, node or view. Unknown scope is 404; invalid
cursor is 400, query bounds 422, corrupt lineage/events or unsupported history 409.
Original status events are checked rather than trusting a mutable projection.

All four reads are metadata/approved comparison feedback and create no exposure
records. They return no per-repeat returns, costs, phase traces, free-form selection
reasons or finalization exposure reports. Use records show/export/artifact and
tests events with audit_identity for those original details; hidden detail is
still gated and audited by the host. Lineage history remains available if original
package bytes are unavailable; candidates show and exports
perform the actual byte checks. No missing result becomes a zero return.

These queries form the experiment UI's data foundation; the evolution page and
interactive Episode details are not yet implemented.
There are no public Test submit or calibration execution operations yet. These commands
start no services, providers, models or fake executions. Remaining internal module
notes below describe implementation boundaries, not additional CLI capabilities.

The preset reserves tuning, selection-validation and final-holdout windows.
Configured historical bundle, fee, calendar, search connection and price-table
references are requested identities, not verified available capabilities. Calendar
resolution requires an injected complete versioned calendar. A resolved specification
still requires historical data, search, runtime and cost capability preflight.
`token_cap` and runtime count caps accept explicit null for unlimited counts;
`budget.task_cost_limit: calibration_derived` selects the calibration formula and
`environment_reserve`/`evaluation_reserve: unresolved` preserve missing measurements.
None of these values authorizes a model call or supplies a zero-cost estimate.

Candidate package hashes identify sealed source/prompt/lock/contract bytes and
allowlisted research configuration. Proposal IDs retain separate parent/hypothesis/
submission provenance even for identical packages. Experiment drafts with invalid
or missing fields retain validation errors and remain non-runnable. Internal storage
retains immutable facts/events, fenced worker attempts, transactional outbox entries
and linked rerun/rescore records. Typed definitions and candidate proposals are now
registered durably; complete TestPlan/sample sets commit atomically and preserve
predeclared order, repeat identities and roles. Registration does not enqueue or
execute an Episode. Final-holdout plan registration requires selection freeze.

Host query views deny optimizer access to selection/holdout detail, trace and
artifacts. Owner reads of hidden details require an explicit audit identity and
commit exposure before returning content. Selection feedback has a numeric/status
allowlist; external known exposure is recorded by date intervals, so task renaming
does not erase it. Expired source originals return metadata/hash-only availability;
source expiry never deletes shared permanent internal artifacts. Exports identify
whether linked bytes are available, source-expired or corrupted. The owner draft/
evidence commands above expose these queries; optimizer views remain host-only.

LE-003 provides `HistoricalBundles.import_bundle` and internal coverage `preflight`
interfaces for `local_historical_bundle_v1`. Imports seal source proof files and
JSONL rows, validate hashes/duplicates/timezones/raw bars, and preserve immutable
failure records. Preflight seals a task/security/date/scenario coverage report;
missing minutes, auctions, reconstructable queues, rules, fees or historical
universe/calendar evidence stay blocked. An unknown historical version uses its
collection time; existing Sina `source_at=fetched_at` and MX `received_at` cannot
be backdated. Fixture coverage passing never means real-data readiness, and setting
origin to real does not bypass independent source/rule review. That acceptance path
and real bundles are still pending. Market Daily remains Sina-only. No historical
coverage-preflight command is public yet; bundle import is available above. The
draft preflight does not call this bundle capability verifier.

LE-004 adds host-only phase-bound data access and Tavily search components.
Main/child sessions share event cutoff and snapshot time; source publication,
collection, date-only, dependency and receipt availability are distinct. Results,
catalogs and sanitized errors expose only permitted products and visible rows.
Candidate tool arguments cannot choose a cutoff, test, connection or raw URL.
Search accepts text with host-injected dates, disables automatic parameters and
generated answers, rejects explicitly future metadata, and labels retained results
`date_filter_trusted`, never timestamp-verified merely because the service filtered
them. Unverified provider timezone/end-date semantics tighten the provider bound.
The adapter requires an injected controlled connection and call-authorization hook;
no live connection or paid request has been accepted. Missing connections return
`search_provider_unavailable` without switching providers. Existing business LAgent
search is unchanged.

Request responses are immutable within a collection generation; boundary, provider,
policy or generation changes create distinct keys. Search originals use source
retention, while permanent query receipts store hashes rather than text copies.
Expired/missing responses cannot be silently refreshed for exact replay; unresolved
external calls remain explicit. The host HTTP transport uses only the fixed Tavily
endpoint and rejects redirects. Complete runtime filesystem/network/shell containment and real provider acceptance
still await LE-008/015. LE-005 now supplies the durable phase lifecycle described below.
These internal interfaces are not a runnable Episode or a public CLI operation.

LE-005 supplies a host-only `PhaseClock` over the existing event/projection store.
It derives the full phase sequence from the sealed task calendar, freezes internal
snapshot evidence, and binds `PhaseSession` permissions to the active durable phase
and current worker generation. `closing` immediately fences main/child permissions;
`closed` precedes advancing to the next scheduled phase. Recovery preserves the
same snapshot and wall deadline. Late results are discarded with hash-only audit.
Required evidence missing or later than the fixed snapshot blocks progression;
optional future evidence is omitted. Search source-policy bodies cannot be made
permanent through the internal snapshot path. Observe distinguishes fixed simulated
cutoffs from actual activation, elapsed time and deadline.

Historical ingress timing separately records platform reception, market reception/
acceptance and receipt availability; market-closed periods wait for the next
historical acceptance interval and configured delay. Post-auction plans cannot
participate in the completed opening auction; forbidden cancellation is rejected.
This timing helper neither creates orders nor fills trades. Budget-stop state skips
later research permissions while allowing scheduled replay; cancellation/platform
failure prevents further advance. Process termination, budget decisions, account
valuation and full Episode orchestration still belong to LE-008/009/006/010.
No runnable historical Episode or execution command has been published.

LE-006 now supplies internal `SimulatedAccount`, `OrderFees` and `AccountValuations`
components. They write only the experiment ledger/projection, not recorded real
ledger transactions or production positions. Batch reservation uses confirmed
cash/shares; pending sales and unconfirmed cancellations cannot fund new orders.
Partial fills settle cumulative per-order fees, reserve remaining maximum fees and
hold price-improvement cash until its receipt. Bought-share resale dates, cash reuse
and withdrawal dates follow the supplied historical rule/calendar. These are
simulation assumptions, not a statement of the owner's broker fees or withdrawal
rules. Tiny-sale commission shortfalls become explicit payables, never cash credit.

Initial/daily/terminal NAV uses qualified raw close values and a sealed account
replay cutoff. A finalized interval cannot accept late earlier fills. Qualified
suspension carry-forward is marked stale; no end liquidation or research-cost debit
is inserted. Mathematical return output remains non-formal until Episode acceptance.
`CorporateActions` now records record-date entitlements, ex-date receivables and
cash payments; attached dividends do not double count raw share prices in NAV.
Explicit historical tax policies drive calendar holding periods, FIFO disposal,
maximum outstanding reserves and sale/payment withholding. Sell order reservations
are reassigned when execution order changes so tax FIFO is preserved. Integer
non-taxable bonus/split shares have explicit trading dates; rights default to a
recorded nonparticipation. Stale raw prices are adjusted through proven intervening
actions; current raw closes are not adjusted twice. Unknown/missing action coverage,
taxable share allocation and unproven fractional/composite-action rules remain unsupported.
Policy fixtures are not accepted real historical rule packages. LE-006 remains in progress.
`SpecialSettlements` additionally executes source-bound, complete-lot retirement,
fixed unconditional cash claims and explicit final replacement-share allocations.
It rejects partial ownership matches, pending orders/entitlements and unavailable
terms. Cash becomes usable only on the payment event; replacement shares require
their own qualified prices and trading times. Retired securities cannot receive new
reservations. Explicit dividend-tax disposal or pure-share tax-lot carry cannot
silently forgive outstanding liabilities. Contingent consideration and mixed tax
allocation are not supported; real source/rule acceptance is still outstanding.
No account API or simulated-order CLI has been published; formal market rules/fees
still require LE-003 acceptance, and execution integration belongs to LE-007/010.

LE-007 supplies internal host-only `ExecutionEngine.accept_batch` and
`advance_minute`: explicit historical acceptance bounds/quantities, whole-batch
resources, full continuous minutes, adverse high/low plus sealed tick slippage,
and stable shared per-security minute capacity. Account staging publishes every
fill/fee and capacity change in one fenced event; retries cannot allocate twice.
Results distinguish model_no_fill from execution_evidence_missing. Auction,
limit-queue, halt/resume and cancel/match competition never fall back to a minute
no-fill result. Phase-plan ingress and cancellation/replacement now use StagePlans as described
below; no simulated-order command is published.
`QueueReplay.advance` now consumes complete source-proven historical books,
contiguous events and fixed auction results, with pinned insertion ordering.
Proven ahead consumption/cancellation can create opportunities; unknown queue
changes fail closed. Simulated fills never rewrite the historical book and share
the same minute capacity as the minute executor. Explicit time/sequence prefixes
persist their cursor and atomic account effects; whole capture artifacts stay in
host evidence storage, while prefix events do not inline future market events.
The future Episode runner must cut replay at every observable/action boundary and
keep raw full-capture artifacts outside candidate access. Pending simulated cancellations are executable only through the fixed effective
time with a proven sequence cut; StagePlans applies the corresponding action.
`StagePlans` permits only the designated root main session to accept one successful
plan per active phase, rechecking scope/deadline inside the commit transaction.
Plan identity is idempotent across transport retries. Whole-plan validation does
not anticipate sale/cancel proceeds; replacement identities are reserved without
reserving future buying power. Orders wait for fixed exchange acceptance and
source-proven matching cursors. Cancellation stops matching at its effective time
but frees resources only at receipt; remaining replacement targets are revalidated
without resizing/repricing and receive fresh priority. Illegal/underfunded targets
preserve prior fills and confirmed cancellation. DAY expiry needs complete closing
queue evidence and cannot skip overdue workflow actions or renew orders next day.
These are host interfaces; the candidate gateway below filters their responses.
The full Episode scheduler/source acceptance remains unfinished.
See `docs/research/lagent-execution-contracts.md` for scope and evidence.

Historical Episode execution, comparison and WebUI remain under development.
Outbox receivers must handle duplicate delivery; unknown external model outcomes
need reconciliation.
See `docs/tickets/lagent-experiments/implementation-progress.md` for evidence.

LE-008 now supplies an internal `CandidateGateway.call_json` dispatcher bound to a
host-created PhaseSession and Test-pinned sealed candidate. Its allowlist is observe,
data_catalog, query, submit_plan, save_memory and load_memory. Candidate new-order
intents cannot choose host fees, market rules or timestamps; plan success returns
only its acknowledgement. Reads use the frozen boundary; repeated queries reuse
filtered audit results, and action identities cannot switch tools. Only the designated
root main can submit plans or atomically commit structured working memory. Children
inherit the same data scope and can read the last committed same-Test/package memory.
Unknown tools, injected fields and host/provider exceptions yield fixed errors.
This dispatcher is not OS isolation or a runnable candidate service: native process,
filesystem/network containment, model/delegation execution, shared cost/call limits,
controlled search wiring and phase process cleanup remain to be integrated. Never
hand the Python dispatcher, records or provider callbacks to candidate code. No new
experiment API or CLI is exposed. See `docs/research/lagent-candidate-gateway-contracts.md`.

LE-009 adds the internal `CostBudget` explicit-budget ledger. It pins the price table,
disjoint billable usage semantics, call-specific upper-bound evidence and complete
no-model resource-envelope evidence to the Test's sealed conditions. Research,
environment and evaluation share the total limit with separate protected buckets;
model/search invocations cannot consume environment/evaluation reserves. Concurrent
reservations use a single CAS projection, and cost records/evidence references commit
atomically with balance changes. Start must commit before dispatch; a started or
unknown invocation must be reconciled, never blindly dispatched again on retry.
Unknown/cancelled outcomes retain their maximum hold until actual usage is known.
Only never-started reservations may release unused funds. Actual overruns are still
charged, invalidate cost/resource assumptions and stop new research authorizations.
Equivalent USD cost and a nullable supplier invoice are separate; subscription usage
is not priced as free. The new owner may settle late usage while the Test is active.
Calibrated budgets now use the frozen campaign below. Real tariff/usage/physical-bound
acceptance, full resource measurement and runtime enforcement remain unfinished.
Terminal reconciliation and experiment totals now use the interfaces below. `formal_ready` remains false,
and cost ledger mutation remains internal. Calibration control commands are described above. See `docs/research/lagent-cost-budget-contracts.md`.

`Calibrations.freeze` now pins a complete calibration plan, its root baseline, price
semantics and all predeclared task envelopes/phase counts before any Test starts.
A Definition has one immutable campaign, so retry cannot substitute cheaper samples
or reprice observed results. Only the original calibration Tests may initialize an
uncapped research measurement ledger; their per-call bounds, phase guards and
protected environment/evaluation reserves still apply. Ordinary Tests need the
matching completed calibration result and cannot enter measurement mode.
`complete` reads all original completed repeats, finished phase histories and
settled budget projections. Missing phase/resource metering, unknown usage or cost
failures block derivation. It takes the maximum research cost, scales by the sealed
multiplier and target/baseline phase counts, adds the pre-frozen target reserve and
rounds upward once. All target allocations and the budget fingerprint commit
atomically; holdout performance is never an input. Campaign detail inherits hidden
Definition scope and is unavailable to optimizer views. Fixtures do not prove real
Episode replay or provider acceptance; `formal_ready` stays false.

`TerminalCosts` appends immutable late settlements or never-started releases to a
terminal Test. It does not reopen that Test, modify its original budget/account
projections or bypass an active worker lease. Started/unknown calls retain their
maximum hold until a bound receipt is verified; actual overruns stay charged and
flagged. Duplicate/concurrent reconciliation commits once. Definitive receipts
cannot be silently replaced; later invoice/cost correction requires a future
explicit correction protocol. `effective_budget` overlays verified facts for
budget reads, calibration and totals, allowing previously unknown terminal
calibration usage to reconcile before derivation.
`ProposalCosts` records outer-proposal attempts and receipts separately from task
allowances; it neither invokes models nor authorizes external spending.
`ExperimentCosts.read` produces owner-only totals from one read snapshot, counting
all actual Test/proposal executions including failed/cancelled attempts and reruns.
Comparison/baseline/calibration summary references add no new charge. Pending holds,
unknown supplier bills and executed Tests missing cost evidence remain explicit.
Its `accounting_valid` flag describes accounting completeness, not formal Episode
eligibility; `formal_ready` stays false. No API/CLI is exposed. Real executors still
need to bind these global identities to provider dispatch/reconciliation and enforce
cost controls without blind retries of unknown external outcomes.

LE-008 also supplies internal `ModelInvocations.begin/poll/cancel/reconcile/stop_phase`.
It pins the Test candidate/model/effort and validates the existing LAgentAction
schema. Drivers need sealed cost-bound/single-attempt/host-action/reconciliation
capabilities and nonblocking poll/cancel APIs. Legacy CodexExecutor lacks that
adapter and is rejected as `cost_capability_missing`; no real call is made.
Each attempt reserves separately, and budget start plus a unique dispatch claim
commit atomically. Restart/concurrent recovery reconciles the same invocation
rather than dispatching again; only a settled transport failure can use the sealed
retry allowance. Model identity/schema/cost failures and late results cannot publish
an action, while actual or unknown costs remain recorded. Main/child identity,
inflight concurrency and configured total-child/step limits are enforced at this
host call boundary. Full delegation scheduling remains to be connected.
PhaseClock finish_close now waits for model-process quiescence; pending usage may
remain held after processes stop. A cancellation timeout never counts as confirmed
exit. Fixture drivers require explicit internal opt-in and cannot confer readiness.
Real candidate process/file/network isolation, production driver behavior, full
action/tool loops and computation metering are still pending. No API/CLI is exposed.
See `docs/research/lagent-model-invocation-contracts.md` for the exact boundary.

LE-008 now also has internal `DarwinCandidateRunner`, tested with real local Python
processes and temporary sentinel data. It copies verified sealed bytes into a
read-only package, creates private scratch, strips inherited env/FDs and applies a
default-deny macOS policy for files, networking and child processes. Explicit limits
bound retained output and drive wall/scratch monitoring plus TERM/KILL cleanup;
quiescence is reported only after wait4 reaps the child, with per-process CPU/RSS.
This is a one-shot byte transport, not a candidate tool loop or an Episode runner.
Apple's deprecated third-party SBPL backend, unsealed host runtime, missing hard
RSS/aggregate disk quotas, catchable CPU signal, host-crash recovery and absent
phase/budget/metering integration keep `formal_ready=false`. No unsandboxed fallback,
dependency installation, actual model call or experiment API/CLI is added. Do not
use the test limits as original-case resource budgets or treat these local tests as
formal isolation acceptance. See `docs/research/lagent-process-isolation-contracts.md`.

The subsequent `PhaseCandidateProcesses` adapter now binds that real process to a
Test-pinned gateway and phase. A bounded JSON-line channel supports the existing
six tools, checks phase permission before dispatch/response and preserves child
restrictions. An independent IO monitor enforces process deadlines while synchronous
host queries wait. Durable CAS dispatch claims prevent blind re-execution; cleanup
is recorded after reaping, and PhaseClock waits for both model and candidate cleanup.
Malformed/oversized/pipelined requests stop, and rejected process output persists only
as hashes. Unknown dispatch/cleanup or a lost lease cannot be called quiescent; this
adapter does not yet reconcile process identity after host crashes. Model/delegation
loops, budget/compute billing, heartbeat/recovery and formal isolation remain pending.
There is still no process-execution API/CLI or formal-ready runner. See
`docs/research/lagent-phase-process-contracts.md` for protocol and recovery boundaries.

Optional `GuardedCandidateRunner` now puts candidate ownership in an independent
guardian. Worker death/IPC loss triggers candidate termination and a durable receipt
after wait4/temporary-file cleanup. `CandidateProcessRecovery` lets the current Test
lease reconcile it during closing, committing receipt artifacts and cleanup state
atomically. A persistent cancellation marker plus the launch lock fences delayed
startup. No child intent proves non-dispatch only while holding that lock; an intent
without a valid receipt remains unknown, regardless of PID absence. There is no blind
relaunch or arbitrary PID kill. Real tests kill a temporary worker and recover the
same Test with a new lease; production services/models are not used for fault tests.
Guardian/whole-machine loss, full Episode/queue/heartbeat recovery, hard resource
envelopes and compute billing remain incomplete. `formal_ready` remains false and no
experiment API/CLI is exposed. See `docs/research/lagent-guardian-recovery-contracts.md`.

`CandidateComputeCosts` now connects actual guardian wait4 candidate CPU to the
research cost ledger. It requires a dedicated CPU scope/semantics, reserves before
dispatch, and commits budget start plus the unique process claim atomically. Main
and same-named child executions have separate billable identities in the shared
budget. Measurement/source artifacts and settlement or release commit together.
Unknown usage retains its hold; proven non-dispatch releases only never-started
reservations, while a committed start receives a zero candidate-CPU receipt with a
nullable supplier bill. Real overruns remain charged and block new research funding.
Current-lease recovery and terminal immutable cost overlays use the same evidence.
`collect` performs stopping reconciliation and accounting; use budget.read for a
non-mutating view. The CPU scope explicitly excludes worker/guardian CPU, memory,
I/O, replay and evaluation. Formal launch fails `compute_cost_capability_missing`;
only an explicit fixture maximum plus a fixture tariff enables development runs.
No hard upper bound, full resource accounting, real tariff or formal readiness is
claimed. No API/CLI is added. See `docs/research/lagent-candidate-compute-contracts.md`.

`EpisodeReplay` now provides an internal, fixture-only durable replay program.
It seals exact phase snapshots and ordered domain inputs, prepares each command
before execution, and resumes the same domain action after a commit/cursor gap.
Minute/queue execution, plan advancement, DAY release, payables, corporate actions,
special settlements and NAV share one
Test account across its declared dates. Research remains an explicit host handoff;
new-lease active phases return research_recovery_required with the original
deadline, requiring original process reconciliation before PhaseClock.resume.
Budget exhaustion skips later research while replay continues to the original
endpoint. Cancellation completes only an already prepared safe action, then
stops without a formal score; known required-evidence failures produce blocked.
ready_for_evaluation is a fixture replay projection, not a completed/scorable Test.
The 5-/3-day tests use short synthetic market sequences and host research callbacks;
they do not establish full market coverage or real LAgent/model execution. Production
model/delegation recovery and environment/evaluation cost accounting are pending;
the shared ResearchService integration below remains fixture-only. No new service,
API or CLI exists; formal_ready remains false. See
`docs/research/lagent-episode-replay-contracts.md`.

Corporate registration/effect/payment and special retirement/payment are sealed
host replay points. Every declared effect due within the original Episode must
appear exactly once at its proven time; same-time market effects precede entitlement
registration, which precedes the close checkpoint. Detachment precedes payment even
at the same instant. Later payments remain qualified receivables without extending
the task. Prepared commands retain original account action IDs across worker loss;
cancellation reconciles only the already prepared action. Missing/unsupported
terms, mismatched owned-lot allocations and missing replacement prices block replay.
Supplied fixture settlement allocations are not a production adapter for arbitrary
candidate holdings. Source coverage, complex tax rules and formal qualification
still require acceptance; no experiment execution command is enabled.

`EpisodeCandidates` now drives those phase handoffs with the Test-pinned Python
candidate, the real guardian backend and mandatory candidate CPU accounting. It
persists attempts and dispatch intent before the budget/process claim. Candidate
failure allowances reset per phase; worker interruptions first reconcile original
dispatch/usage and do not consume that allowance. An active same-generation intent
without a process claim requires worker recovery; it must not be blindly reissued.
Unknown guardian/usage facts retain holds and block advancement. Original deadlines
remain fixed. Cancellation callbacks persist the Episode stop before stopping the
process, so a transient signal cannot be forgotten by the next tick.
Host attempt IDs scope tool idempotency: recovery reads the latest committed memory
while accepted plans still deduplicate by plan_id. Required query failure and host
audit failure cannot become successful/scorable work merely because code exits 0;
optional data absence and normal plan rejection remain distinct. Verified durable
exit receipts survive a lost final transport message without re-execution.
These are fixture candidate executions, not real model/search/delegate research.
Shared queue/heartbeat/lifecycle, environment/evaluation budgeting and production
data/resource acceptance remain pending. No API/CLI or service is added. See
`docs/research/lagent-episode-candidate-contracts.md`.

The existing ResearchService now schedules business Requests and admitted fixture
Tests through one durable work index. A Test retains its own records/account and
never becomes a fabricated team/security Request. Admission seals its descriptor;
claiming binds both the Test lease and the singleton service epoch. Service handoff
immediately fences old Test writes, even before their local lease expires. The same
Service peer heartbeat renews Test ownership during slow candidate/tool execution.
Unknown cleanup keeps the shared lane occupied; never bypass it by starting another
service. Test cancellation is durable and reaches the original guardian/CPU cleanup.
Before-start cancellation launches nothing. Replay completion enters evaluating and
parks only after physical cleanup; the evaluator is still pending, so waiting does
not mean completed/scorable. Waiting cancellation is serviced without rerunning the
candidate. The current-requests service status now includes nullable active_test_id
and a shared queued_count; active_request_id/requests still identify business work.
No new API/CLI is published and default production service construction does not
enable fixture admission or an experiment factory. Schema migration adds/backfills
the shared index before reloading installed services; do not interrupt active business
work or start a second daemon. See `docs/research/lagent-shared-service-contracts.md`.

Internal evaluation now derives NAV diagnostics from the Episode's original committed
valuation IDs, preserving phase/cleanup/cost failures and immutable assessment inputs.
Retries return the original snapshot; another assessment must link rescore_of. A
comparison freezes the full pair/sample plan before execution and later retains the
first assessment identities, including missing or failed samples. Subsequent costs
or reassessments never overwrite the old comparison. Inconclusive summary numbers
and positive-repeat counts may be null; they are not zero returns or zero wins.
Different declared task durations now resolve normally and are reported in separate
groups; a combined raw-period score needs sealed allow_mixed_durations and weights.
Tuning/holdout cannot select a baseline. Pure arithmetic eligibility is not promotion:
formal replay/resource acceptance remains pending. Runtime fingerprint checks,
baseline CAS and holdout freezing are implemented, with selection controls exposed
above. Shared-service evaluating/waiting has not become completed; host assessment
production remains internal; comparison freezing/results have the
public commands above. See `docs/research/lagent-evaluation-contracts.md`.

Initial baseline registration, comparison decisions and final selection freeze now
have the public selection commands described above.
The initial root candidate must be fixed before execution; finalization checks the
expected selection revision and requires all planned Tests terminal with known
physical cleanup. Freeze prevents new candidate, tuning/selection/calibration plan
or optimization-rerun registrations in that experiment. Existing input retries stay
idempotent. A comparison-based selection commit now uses CAS and retains both the
initial baseline and the new current candidate; actual runtime qualification is
still unavailable from fixture Episode assessments.
A frozen selection admits exactly one complete holdout plan for its initial/current
candidate identities and original definition. Holdout reruns or renamed extra plans
are rejected. Exposure checks use actual trading dates across the single owner's
experiments and previous training runs, never task names. Unknown exposure dates
remain unresolved. Metadata-only reads of registered definitions/plans/Tests/selection
remain audited without consuming unseen market outcomes; result/event/data reads do.
Exposure arriving after freeze but before admission blocks a new plan; later result
reads preserve the original frozen facts and are visible in current exposure reports.
These operations add no model calls or formal execution capability. See
`docs/research/lagent-selection-holdout-contracts.md`.

Comparisons can now pin the expected current selection and complete per-task runtime
condition declarations, including corpus generation, search policy, actual model,
executor, fees, pricing, budget and scoring version. Declaration is not acceptance:
each original host assessment must independently attest matching actual conditions
and valid formal NAVs. Current Episode fixture assessments cannot do so. Eligible
comparison feedback only permits attempting selection; apply_comparison recomputes
the original evidence and changes the baseline in one expected-revision transaction.
Ties/invalid/unstable results keep the baseline. Old comparisons record stale_baseline
and cannot rebind to the current revision; final freeze records selection_frozen.
Selection decisions are permanent and idempotent, and non-promotions do not advance
the revision. Positive CAS tests use synthetic accepted-assessor contracts, not real
historical or model runs. No API/CLI is added; baseline reuse and the actual qualified
runtime/evaluation producer remain pending.

BaselineReuse now resolves a proposed plan against the expected current selection
and exact task runtime conditions. It chooses the earliest registered complete
qualified baseline cohort with the same specification, role, tasks, repeat IDs/index,
seed support/seed and package; it never ranks by returns or mixes groups. Baseline
slot IDs in the input are placeholders; the returned sealed plan records the original
Test IDs and its pinned source proofs. Only new_test_ids need execution. Reused Tests
retain original plan metadata, account/phase/model state and costs; no duplicate Test
or bill is created. A promoted candidate's original cohort can supply the next
baseline, without copying its actions into the new candidate. Failed/nonformal first
assessments cannot be replaced by a later rescore through reuse. No eligible cohort
means no new plan/sample admission. Retries retain their original source even if
eligibility or the current selection later changes. No calibration/holdout reuse or
new API/CLI is enabled; real qualification production remains pending. See
`docs/research/lagent-baseline-reuse-contracts.md`.
