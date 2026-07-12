# Advisor Runtime Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox ('- [ ]') syntax for tracking.

**Goal:** Turn the already-tested advisor components into one runnable, fail-closed local A-share advisor loop with free market data, accepted MX evidence, daily analysis, durable profiles/charts/ledger projections, and commands that launchd can schedule.

**Architecture:** Keep the Node 24 MX collector unchanged and read its accepted SQLite ledger through a bounded read-only adapter. Use the existing advisor SQLite schema as the single operational database, inject external data/analyst dependencies behind protocols, and coordinate premarket/review runs in Python transactions plus immutable report publication. Every external or quality ambiguity archives a blocking failure and exposes no recommendation.

**Tech Stack:** Python 3.11+, SQLite, pytest, requests, pandas, matplotlib/mplfinance, Pydantic, PyYAML, TradingAgents-astock local adapter, Node 24.18.0 for the existing collector self-test.

## Global Constraints

- Work only inside '/Users/mac/Documents/Ahunter/a_hunter'.
- Do not modify existing collector behavior, discover or add RIDs, or operate the MX page.
- Run every collector or self-test command with '/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node'.
- Empty 'config/allowed-rids.yaml' is intentionally inactive and must yield no MX evidence or media.
- Only accepted, allowlisted MX rows may cross into advisor evidence; never persist rejected/undecodable/non-allowlisted content.
- A data-quality failure blocks analyst recommendations and review conclusions and creates an immutable failure archive.
- Never connect to a broker, submit an order, or present advice as an executed trade.
- Required market-data paths use free sources learned from '/Users/mac/Documents/TradingAgents-astock'; Tushare and paid/API-key-only market data are forbidden.
- External LLM credentials are runtime-only upstream configuration and must never enter files, logs, reports, API responses, or evidence.
- All report/run dates use Asia/Shanghai and all reads/writes are bounded.
- Use strict RED-GREEN-REFACTOR TDD for every behavior change and request an independent review after every task.

---

### Task 14: Free A-Share Data and One Operational Database

**Files:**
- Modify: 'advisor/config.py'
- Modify: 'config/advisor.yaml'
- Modify: 'config/data-sources.yaml'
- Modify: 'advisor/data_sources/contracts.py'
- Rewrite: 'advisor/data_sources/free_sources.py'
- Modify: 'advisor/data_sources/backfill.py'
- Modify: 'advisor/db/repository.py'
- Test: 'tests/advisor/test_config.py'
- Test: 'tests/advisor/test_free_sources.py'
- Test: 'tests/advisor/test_market_backfill.py'

**Interfaces:**
- Produces 'SinaDailyBarProvider(session: requests.Session | None = None, clock: Callable[[], datetime] | None = None)'.
- Produces 'ConfiguredProviderRegistry.from_yaml(path: Path) -> ConfiguredProviderRegistry'.
- Produces 'resolve_state_db(config: AdvisorConfig, root: Path) -> Path'.
- Produces 'update_market_database(db_path: Path, provider: MarketDataProvider, codes: Sequence[str], start: date, end: date, *, sleep: Callable[[float], None]) -> BackfillResult'.
- The CLI 'advisor-backfill --config PATH --codes CODE[,CODE...] --start YYYY-MM-DD --end YYYY-MM-DD' is runnable and non-zero on partial/ambiguous source data.

- [ ] **Step 1: Write failing configuration and provider tests**

Add tests proving that market and advisor paths resolve to the same operational SQLite file, relative paths cannot escape the repository, Tushare remains rejected, and the provider parses a recorded Sina response into typed bars without network access.

~~~python
def test_storage_uses_one_operational_database(tmp_path):
    config = configured_for(tmp_path, database="data/advisor/advisor.sqlite")
    assert resolve_state_db(config, tmp_path) == tmp_path / "data/advisor/advisor.sqlite"

def test_sina_provider_parses_and_bounds_daily_bars(recorded_session):
    bars = SinaDailyBarProvider(recorded_session, fixed_clock).fetch_daily_bars(
        "600519", date(2023, 7, 12), date(2026, 7, 12)
    )
    assert bars[0].source == "sina_http"
    assert bars[0].as_of_date == bars[0].trade_date
    assert bars[-1].trade_date <= date(2026, 7, 12)
    assert len(bars) <= 800
~~~

- [ ] **Step 2: Run focused tests and verify RED**

Run:

~~~bash
.venv311/bin/python -m pytest tests/advisor/test_config.py tests/advisor/test_free_sources.py -q
~~~

Expected: failures for missing single-database resolver and live provider.

- [ ] **Step 3: Implement the free provider and registry**

Use the direct Sina daily K-line endpoint and response shape already documented in 'TradingAgents-astock/tradingagents/dataflows/a_stock.py'. Request only a six-digit A-share code, 'scale=240', 'ma=no', and 'datalen=800'. Validate HTTP status, content type/JSON shape, date range, finite positive OHLC, 'low <= open/close <= high', non-negative volume, unique ascending dates, and a maximum of 800 rows. Hash canonical normalized row JSON for 'content_hash'. Never silently return an empty list after a source error; raise a typed 'MarketSourceError'.

The registry must read only configured free source names and rate limits. Its first working historical path is 'sina'; unsupported enabled sources are reported as degraded metadata, not selected as fake providers.

- [ ] **Step 4: Make backfill resumable, atomic per code, and observable**

Add:

~~~python
@dataclass(frozen=True)
class BackfillResult:
    requested_codes: tuple[str, ...]
    completed_codes: tuple[str, ...]
    failed_codes: tuple[str, ...]
    inserted_rows: int

def update_market_database(
    db_path: Path,
    provider: MarketDataProvider,
    codes: Sequence[str],
    start: date,
    end: date,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> BackfillResult:
    requested = validate_backfill_request(codes, start, end)
    completed: list[str] = []
    failed: list[str] = []
    inserted = 0
    for code in requested:
        try:
            inserted += update_one_code(db_path, provider, code, start, end)
            completed.append(code)
        except MarketSourceError:
            record_failed_source_attempt(db_path, provider, code, start, end)
            failed.append(code)
    return BackfillResult(requested, tuple(completed), tuple(failed), inserted)
~~~

Validate codes and dates before opening SQLite. Migrate the configured operational database, process at most 200 explicit codes, use one transaction per code, upsert only when the incoming canonical hash differs, record every attempt in 'market_sources', and never delete a prior good row after a failed fetch. Reject bars outside the requested interval or with 'as_of_date > end'.

- [ ] **Step 5: Add CLI and three-year completeness tests**

Tests must prove:

- a 36-month request is issued;
- the command writes the same DB the API reads;
- a rerun is idempotent;
- a failed second code does not corrupt the first code;
- a partial result exits non-zero and records the failed source;
- no Tushare string appears in a selected endpoint or provider.

- [ ] **Step 6: Verify and commit**

Run:

~~~bash
.venv311/bin/python -m pytest tests/advisor/test_config.py tests/advisor/test_free_sources.py tests/advisor/test_market_backfill.py tests/advisor/test_web_api.py -q
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
git diff --check
~~~

Commit: 'feat: add live free astock market updater'

---

### Task 15: Read-Only MX Evidence Handoff and Persisted Quality Gate

**Files:**
- Rewrite: 'advisor/evidence/mx_adapter.py'
- Create: 'advisor/evidence/service.py'
- Rewrite: 'advisor/quality.py'
- Create: 'advisor/reporting/failure.py'
- Modify: 'advisor/reporting/contracts.py'
- Test: 'tests/advisor/test_mx_evidence_quality.py'
- Create: 'tests/advisor/test_quality_gate.py'
- Test: 'tests/advisor/test_reporting.py'

**Interfaces:**
- Produces 'read_collector_snapshot(events_db: Path, allowed_rids_path: Path, *, as_of: datetime, limit: int = 100) -> CollectorSnapshot'.
- Produces 'persist_evidence(connection: sqlite3.Connection, run_id: str, snapshot: CollectorSnapshot, *, as_of: datetime) -> list[EvidenceRecord]'.
- Produces 'evaluate_run_quality(connection: sqlite3.Connection, request: QualityRequest) -> QualityGateResult'.
- Produces 'write_failure_report(report_date: str, run_type: Literal["premarket", "review"], failures: Sequence[QualityResult], output_dir: Path, *, run_id: str) -> ReportPaths'.

- [ ] **Step 1: Replace synthetic MX tests with the real collector schema**

Create fixture tables from 'src/events/schema.sql'. Insert two authorized accepted events, one non-allowlisted event, pending/failed media jobs, and decode-failure counters. Tests must prove:

~~~python
def test_empty_allowed_rids_returns_inactive_blocking_snapshot(
    tmp_path, create_collector_db, write_allowed_rids
):
    db = create_collector_db(tmp_path)
    allowed = write_allowed_rids(tmp_path, [])
    as_of = datetime(2026, 7, 12, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    snapshot = read_collector_snapshot(db, allowed, as_of=as_of)
    assert snapshot.events == ()
    assert snapshot.quality.blocking_failure

def test_only_allowlisted_accepted_rows_become_bounded_evidence(
    tmp_path, create_collector_db, write_allowed_rids
):
    db, expected_hash = create_collector_db(tmp_path, authorized_rid=123)
    allowed = write_allowed_rids(tmp_path, [123])
    as_of = datetime(2026, 7, 12, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    snapshot = read_collector_snapshot(db, allowed, as_of=as_of, limit=1)
    assert [event.rid for event in snapshot.events] == [123]
    assert snapshot.events[0].content_hash == expected_hash
    assert "raw_payload" not in snapshot.events[0].to_dict()
~~~

Also prove malformed YAML, symlinked DB/config, missing required schema, timestamps after 'as_of', and ambiguous collector counters fail closed.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

~~~bash
.venv311/bin/python -m pytest tests/advisor/test_mx_evidence_quality.py tests/advisor/test_quality_gate.py -q
~~~

Expected: failures because the real snapshot and quality service do not exist.

- [ ] **Step 3: Implement a descriptor-pinned, bounded, read-only adapter**

Open SQLite with 'mode=ro', reject symlinks, validate required columns using 'PRAGMA table_info', and cap scanned events/media jobs/failures/counters. Parse the user-authored YAML through 'yaml.safe_load'; only positive integer entries in 'allowed_rids' authorize rows. Do not discover or add a RID.

Expose only stable IDs, RID, accepted content hash, bounded decoded summary, received/source timestamps, and bounded local media metadata. Never return raw payload, source URLs, credentials, cookies, tokens, session/debug identifiers, or non-allowlisted rows. Derive 'evidence_id' from a domain-separated SHA-256 of source type + event ID + content hash.

- [ ] **Step 4: Persist normalized evidence idempotently**

Persist only records at or before the run 'as_of'. Use parameterized SQL and one transaction. Store bounded source references, not raw content. Re-running the same snapshot must not duplicate 'events_normalized' or 'evidence'.

- [ ] **Step 5: Implement the complete quality gate**

Define:

~~~python
@dataclass(frozen=True)
class QualityRequest:
    run_id: str
    run_type: str
    as_of: datetime
    candidate_codes: tuple[str, ...]
    collector: CollectorSnapshot

@dataclass(frozen=True)
class QualityGateResult:
    status: Literal["passed", "blocked"]
    checks: tuple[QualityResult, ...]
~~~

Persist checks for collector state, trading-calendar availability, market staleness, three-year candidate coverage, future-data leakage, ledger replay validity, and required analyst-contract readiness. Any missing/invalid input is blocking. Optional-source degradation is a warning only when another selected source proves coverage.

- [ ] **Step 6: Archive explicit failure reports**

Failure publication must use the existing immutable marker/hash/claim-lock machinery, report type 'failure', and include the attempted run type and bounded quality details. It must contain no advice, review conclusion, analyst prose, raw MX text, or secrets.

- [ ] **Step 7: Verify and commit**

Run:

~~~bash
.venv311/bin/python -m pytest tests/advisor/test_mx_evidence_quality.py tests/advisor/test_quality_gate.py tests/advisor/test_reporting.py -q
.venv311/bin/python -m pytest tests/advisor -q
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
git diff --check
~~~

Commit: 'feat: add mx evidence quality handoff'

---

### Task 16: Daily Run Coordinator, Analyst Decision Contract, Profiles, and Charts

**Files:**
- Modify: 'advisor/config.py'
- Modify: 'config/advisor.yaml'
- Modify: 'advisor/agents/astock_adapter.py'
- Create: 'advisor/runtime/__init__.py'
- Create: 'advisor/runtime/coordinator.py'
- Create: 'advisor/runtime/decisions.py'
- Create: 'advisor/runtime/projections.py'
- Modify: 'advisor/reporting/premarket.py'
- Modify: 'advisor/reporting/review.py'
- Modify: 'advisor/profiles/service.py'
- Modify: 'advisor/charts/kline.py'
- Modify: 'pyproject.toml'
- Create: 'tests/advisor/test_daily_coordinator.py'
- Test: 'tests/advisor/test_astock_adapter.py'
- Test: 'tests/advisor/test_profiles.py'
- Test: 'tests/advisor/test_kline_chart.py'

**Interfaces:**
- Produces 'run_premarket(request: PremarketRunRequest, deps: RuntimeDependencies) -> DailyRunResult'.
- Produces 'run_review(request: ReviewRunRequest, deps: RuntimeDependencies) -> DailyRunResult'.
- Produces executable console commands 'advisor-report-premarket' and 'advisor-report-review'.
- Produces 'parse_portfolio_decision(output: AnalystOutput) -> AdviceDraft'.
- Produces 'project_stock_artifacts(connection, run: ProjectionRequest) -> ProjectionResult'.

- [ ] **Step 1: Write coordinator contract tests with real SQLite and injected fakes**

Tests must prove the exact sequence and persisted result without mocking SQLite:

~~~python
def test_premarket_run_persists_evidence_all_roles_advice_profiles_chart_and_archive(
    migrated_state_db, fake_runtime_dependencies, premarket_request
):
    result = run_premarket(premarket_request, fake_runtime_dependencies)
    assert result.status == "passed"
    assert db_role_names(migrated_state_db, result.run_id) == set(ANALYST_ROLES)
    assert verified_archive(result.report_paths)["json"]["advice_ids"]

def test_any_quality_or_analyst_failure_archives_failure_and_no_advice(
    migrated_state_db, blocked_runtime_dependencies, premarket_request
):
    result = run_premarket(premarket_request, blocked_runtime_dependencies)
    assert result.status == "blocked"
    assert advice_rows(migrated_state_db, result.run_id) == []
    assert verified_archive(result.report_paths)["report_type"] == "failure"
~~~

Add review tests proving it reads the active same-day premarket archive, links every review to an existing advice ID, compares close/action and recorded ledger action, and fails closed if the morning chain is missing or ambiguous.

- [ ] **Step 2: Run focused coordinator tests and verify RED**

Run:

~~~bash
.venv311/bin/python -m pytest tests/advisor/test_daily_coordinator.py -q
~~~

Expected: module or callable missing.

- [ ] **Step 3: Make TradingAgents runtime explicitly configurable**

Add configuration for the local repository path and upstream provider/model names, but no secret values. Validate that the repository contains the graph module and all 16 required roles. Runtime credentials remain in the upstream provider environment; health errors must name only the missing variable name, never its value.

Extend 'AnalystOutput' so the portfolio-manager output carries a bounded structured decision payload. When upstream gives only markdown, parse exactly one '**Rating**: Buy|Overweight|Hold|Underweight|Sell', one non-empty executive summary, and one non-empty investment thesis. Ambiguous/missing fields raise 'DataQualityBlockedError'.

Map the five ratings to advisor actions without pretending to execute: 'watch_buy', 'watch_add', 'hold', 'watch_reduce', 'watch_exit'. The confidence field is a documented ordinal strength mapping '(0.80, 0.65, 0.50, 0.65, 0.80)', not a probability, and the JSON payload must include 'confidence_basis=rating_strength'.

- [ ] **Step 4: Implement transactional run coordination**

Each command computes its date/as-of in Asia/Shanghai unless an explicit test/operator date is supplied. It creates an 'advisor_runs' row, obtains market/MX/ledger inputs, persists evidence, evaluates pre-analysis quality, runs all 16 roles per bounded candidate, validates every output, persists outputs, evaluates post-analysis quality, and only then persists advice/reviews and publishes an immutable archive.

Database work is transactional; report publication happens through the existing claim/marker protocol. On any exception, rollback recommendation rows, mark the run blocked/failed with a sanitized message, and publish an explicit failure archive. A retry uses an explicit versioned run ID and supersession link.

- [ ] **Step 5: Project every noticed stock into profile/history and chart assets**

Candidate triggers are accepted events, advice/review codes, analyst candidate references, and ledger holdings. Ensure a 'securities' row before profile creation. Merge bounded profile dimensions without erasing prior non-empty information, write 'stock_profile_history', atomically render 'data/advisor/profiles/<code>.md', generate local and report K-line PNGs, and register 'chart_assets'.

Extend charts with advice markers, user-trade markers, MX-event markers, and available limit-up/down context. Query only local stored market/evidence/ledger rows with 'as_of <= run.as_of'.

- [ ] **Step 6: Implement executable premarket/review CLIs**

The console commands load config, resolve the one state DB, accept '--date' and '--as-of' for deterministic/manual runs, and default to Shanghai now. They print only run ID, status, and archive paths. They never print analyst evidence or environment variables. Exit 0 only for a completed passed or intentionally archived blocked run; malformed configuration/runtime errors exit non-zero after a best-effort failure archive.

- [ ] **Step 7: Verify and commit**

Run:

~~~bash
.venv311/bin/python -m pytest tests/advisor/test_daily_coordinator.py tests/advisor/test_astock_adapter.py tests/advisor/test_profiles.py tests/advisor/test_kline_chart.py tests/advisor/test_reporting.py -q
.venv311/bin/python -m pytest tests/advisor -q
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
git diff --check
~~~

Commit: 'feat: add runnable daily advisor loop'

---

### Task 17: Durable Ledger Import, A-Share Consistency, and Portfolio Snapshots

**Files:**
- Create: 'advisor/ledger/store.py'
- Modify: 'advisor/ledger/model.py'
- Modify: 'advisor/ledger/importer.py'
- Modify: 'advisor/web/api.py'
- Modify: 'advisor/runtime/projections.py'
- Test: 'tests/advisor/test_ledger.py'
- Test: 'tests/advisor/test_web_api.py'
- Test: 'tests/advisor/test_daily_coordinator.py'

**Interfaces:**
- Produces 'LedgerStore(db_path: Path)' with 'append', 'import_many', 'replay', and 'snapshot' methods.
- Produces 'import_ledger_csv(db_path: Path, csv_path: Path, *, account_id: str) -> LedgerMutationResult'.
- Produces 'create_portfolio_snapshot(connection, account_id: str, as_of: datetime) -> PortfolioSnapshot'.
- API and CLI use the same store and validation path.

- [ ] **Step 1: Write failing durable import and consistency tests**

Use the migrated real schema. Prove CSV import is all-or-nothing, duplicate-safe, bounded, and visible immediately through '/api/current-state'. Add A-share analytical flags for non-lot buys and same-day sells:

~~~python
def test_import_persists_atomically_and_api_replays_same_state(
    migrated_state_db, valid_ledger_csv
):
    imported = import_ledger_csv(
        migrated_state_db, valid_ledger_csv, account_id="default"
    )
    assert imported.inserted == 3
    assert api_current_state(migrated_state_db)["ledger"] == imported.ledger

def test_historical_facts_are_kept_but_t1_and_lot_inconsistencies_are_flagged(
    migrated_state_db, non_lot_same_day_rows
):
    result = LedgerStore(migrated_state_db).import_many(non_lot_same_day_rows)
    assert {flag.code for flag in result.quality_flags} == {"lot_size", "t_plus_one"}
    assert result.inserted == len(non_lot_same_day_rows)
~~~

The flags are quality/analytics facts; they must not mutate, reject, or invent the user's otherwise structurally valid historical trade.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

~~~bash
.venv311/bin/python -m pytest tests/advisor/test_ledger.py tests/advisor/test_web_api.py -q
~~~

Expected: missing store/durable CLI behavior.

- [ ] **Step 3: Extract one bounded transactional ledger store**

Move canonical validation/replay and capacity handling out of the web module. Keep parameterized SQL, 'BEGIN IMMEDIATE', deterministic '(account_id, trade_date, transaction_id)' ordering, duplicate rejection, oversell rejection, and no partial import. Bound IDs, strings, files, rows, and response sizes.

Both FastAPI and CSV CLI call this service. No method may import a broker SDK or create an order-like object.

- [ ] **Step 4: Add daily snapshots and advice-to-action projection**

At review time, value positions with the latest local close at or before 'as_of', persist one versioned portfolio snapshot per account/run, and associate same-day actual trades to advice by code/date without claiming causality. Surface cash, market value, realized/unrealized PnL, exposure, flags, and matched advice IDs in the review context.

- [ ] **Step 5: Verify and commit**

Run:

~~~bash
.venv311/bin/python -m pytest tests/advisor/test_ledger.py tests/advisor/test_web_api.py tests/advisor/test_daily_coordinator.py -q
.venv311/bin/python -m pytest tests/advisor -q
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
git diff --check
~~~

Commit: 'feat: complete durable investment ledger'

---

## Plan Self-Review

- Spec coverage: Task 14 closes free data, three-year storage, and disconnected DB gaps. Task 15 closes accepted MX evidence and quality/failure archive gaps. Task 16 closes daily coordination, all-agent runtime, report linkage, profiles, and charts. Task 17 closes durable import, snapshots, A-share consistency, and advice/action linkage.
- Placeholder scan: no deferred implementation step is accepted; every task has named interfaces, RED tests, production behavior, verification commands, and a commit boundary.
- Type consistency: coordinator consumes Task 14's operational DB/provider and Task 15's snapshot/quality results; projections and Task 17 share the same SQLite connection and Shanghai 'as_of'.
- Safety: collector code remains unchanged, RIDs remain user-authorized only, data-quality failures archive no recommendations, and no broker/order capability is introduced.
