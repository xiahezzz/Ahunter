# Event-driven A-share investment advisor design

- 状态：已确认设计，待用户审阅书面规格
- 日期：2026-07-11
- 时区：Asia/Shanghai
- 项目根目录：`/Users/mac/Documents/Ahunter/a_hunter`
- 参考仓库：`/Users/mac/Documents/TradingAgents-astock`

## 1. Goal

Build a local Mac-based event-driven A-share investment advisor. The system
combines information flow, capital flow, and analyst flow to produce daily
research advice, daily review, stock profiles, K-line charts, portfolio ledger
views, archived reports, and a local frontend.

The system is an advisor, not a real-time trading agent. It never connects to a
broker, never submits orders, and never treats generated advice as permission to
trade. User trades are tracked through a local ledger, initially by manual entry
or file import.

## 2. Confirmed requirements

1. The primary market is mainland China A-shares.
2. The system outputs today's investment advice every trading day at 08:30.
3. The system outputs a 22:30 daily review that explicitly evaluates the 08:30
   advice from the same day. Routine reports are archived.
4. The system maintains stock profiles for every stock it pays attention to.
   Profiles record important dimensions, investment logic, catalysts, events,
   risks, and related information.
5. The system learns from `TradingAgents-astock` and uses all of its analysis
   agents as this repository's analyst layer.
6. All market and company data sources are free sources learned from
   `TradingAgents-astock`; Tushare is not part of this design.
7. The existing MX source remains connected in this repository and supplies the
   proprietary information flow through the existing authorized collector.
8. The system is an investment advisor, not a live trading agent.
9. The system builds a recent three-year stock database and can generate K-line
   chart images.
10. The system includes an investment ledger that tracks user trades and current
    exposure.
11. The system includes a frontend page that shows the current trading and
    investment state.
12. The system includes local keepalive and scheduling for Mac operation.

## 3. Non-goals and safety boundaries

- Do not place, route, simulate-through-broker, or submit real orders.
- Do not connect to a brokerage API in the first implementation phase.
- Do not infer, discover, or add MX RIDs from observed traffic. Only the user may
  supply or authorize RIDs.
- Do not navigate, reload, click, type into, inject into, or otherwise operate
  the MX page during routine collection, smoke testing, reconnect, or recovery.
- Do not save credentials, cookies, tokens, Socket.IO session IDs, or Chrome
  debugging identifiers.
- Do not generate downstream stock recommendations after a required data-quality
  failure until the failure is resolved.
- Do not use Tushare, paid market data, or API-key-only market data for the
  required A-share data pipeline.
- Do not claim guaranteed returns. Reports must state that they are research and
  advisory outputs, not orders or profit guarantees.

## 4. Current project context

This repository already contains the Phase 1 passive MX collector:

- `scripts/run-collector.mjs`
- `scripts/self-test.mjs`
- `scripts/smoke-test.mjs`
- `config/allowed-rids.yaml`
- `src/events/schema.sql`
- `src/ingestion/*`
- `src/media/*`
- `agents.md`

The collector stores only user-authorized RID events in SQLite and remains a
separate safety-critical component. The advisor system consumes accepted events
after they have been stored; it does not weaken or bypass the collector's
fail-closed behavior.

The earlier `2026-07-03-a-share-agent-harness-design.md` established useful
concepts such as evidence packets, daily reports, simulation rules, and
evaluation, but it used Tushare and different report times. This design replaces
those parts with free sources and the requested 08:30 / 22:30 schedule.

## 5. Architecture recommendation

Use a two-layer architecture:

```text
Existing Node 24 MX collector
  - passive Chrome DevTools Network listener
  - authorized RID filtering
  - event and media ledger
        |
        v
New Python advisor service
  - free A-share data adapters
  - three-year market database
  - event normalization and evidence builder
  - TradingAgents-astock analyst orchestration
  - reports, reviews, stock profiles, ledger, charts
  - local API and dashboard
        |
        v
launchd-managed local runtime
```

This keeps the safety-sensitive collector in the existing Node implementation
while placing analyst orchestration, market data, charting, reporting, and the
frontend API in a Python service that can reuse `TradingAgents-astock` patterns.

### Alternatives considered

1. **Recommended: Node collector plus Python advisor service.** This preserves
   the existing collector boundary, reuses A-share analyst and data-source work,
   and keeps the implementation testable by subsystem.
2. **All-Node rewrite.** This would unify the runtime but would require
   rewriting `TradingAgents-astock` data adapters, analyst graph concepts, and
   charting support. It adds schedule risk without improving safety.
3. **Embed this repository into `TradingAgents-astock`.** This is quick for
   analysis demos but mixes the MX collector's strict safety rules with a larger
   external framework. Long-term maintenance and data governance are worse.

## 6. System modules

### 6.1 MX information-flow adapter

The existing collector remains authoritative for MX data ingestion. The advisor
reads only accepted rows from `data/state/events.sqlite`.

Responsibilities:

- Read authorized `events`, `media`, and media-job status.
- Convert accepted MX messages into evidence candidates.
- Preserve event IDs, RIDs, timestamps, content hashes, and media links.
- Respect collector data-quality failures and stop advice generation when the
  collector has marked a blocking quality state.

It must not:

- Start routine page operations.
- Infer RIDs.
- Expose raw secrets or debugging identifiers.
- Reclassify rejected or undecodable content as usable evidence.

### 6.2 Free A-share data adapters

The advisor uses free sources and retrieval methods learned from
`TradingAgents-astock`, especially `tradingagents/dataflows/a_stock.py` and the
tool routing in `tradingagents/dataflows/interface.py`.

Required source categories:

| Source | Use |
| --- | --- |
| mootdx | OHLCV K-lines, financial snapshots, F10 text |
| Tencent Finance | quotes, PE, PB, market cap, turnover, limit prices |
| Eastmoney | dragon-tiger board, lockup, fund flow, sectors, stock news |
| Sina Finance | historical K-line fallback, financial statement fallback |
| 10jqka | consensus EPS, hot stocks, northbound data supplement |
| Cailian Press | global and macro finance news |
| Baidu Stock | concept blocks and capital-flow supplement |

The data adapter layer must implement:

- Per-source rate limits and retries.
- Response schema normalization.
- Source timestamp capture.
- `as_of` filtering to prevent future data leakage.
- Cache keys based on source, endpoint, parameters, date, and schema version.
- Clear failure states when data is stale, missing, or inconsistent.

### 6.3 Three-year market database

The market database stores the recent three years of stock data needed for
daily advice, charting, stock profiles, and evaluation.

Recommended storage:

- `data/advisor/market.sqlite` for normalized relational data and indexes.
- Optional Parquet snapshots under `data/advisor/parquet/` for large historical
  tables once volumes justify it.

Minimum datasets:

- A-share security master and trading calendar.
- Daily OHLCV, adjustment factors, limit-up and limit-down prices.
- Major indexes, including CSI 300 as a benchmark.
- Industry and concept membership.
- Sector and concept performance.
- Turnover, market cap, valuation fields, and liquidity fields.
- Dragon-tiger board data.
- Fund-flow and northbound-flow data when available from free sources.
- Lockup and reduction-risk data.
- Financial statement summaries and consensus fields available from free
  sources.
- News and announcement summaries with source timestamps.

Every row that can affect advice must record:

- `source`
- `source_published_at` or equivalent source timestamp when available
- `fetched_at`
- `as_of_date`
- `schema_version`
- `content_hash` or parameter hash
- data-quality status

### 6.4 Event normalization and evidence builder

The evidence layer combines MX events, free data, analyst outputs, stock
profiles, and ledger state into bounded inputs for reports.

Evidence categories:

- Information flow: MX events, news, announcements, policy items, Cailian Press
  items, and profile notes.
- Capital flow: price/volume, turnover, fund flow, northbound flow,
  dragon-tiger board, sector rotation, and liquidity.
- Analyst flow: analyst reports, bull/bear debate, risk debate, manager
  synthesis, and prior review outcomes.

Evidence objects must include:

- Stable `evidence_id`
- `as_of`
- related stock code and name when applicable
- source type and source ID
- summary text
- confidence and data-quality flags
- facts versus inferences
- conflicts or missing fields

Reports may cite only evidence that passes the data-quality gate for the report
time.

### 6.5 TradingAgents-astock analyst layer

The advisor imports or adapts the full analysis chain from
`TradingAgents-astock`:

1. Market analyst
2. Social media / sentiment analyst
3. News analyst
4. Fundamentals analyst
5. Policy analyst
6. Hot-money / capital-flow tracker
7. Lockup watcher
8. Quality gate
9. Bull researcher
10. Bear researcher
11. Research manager
12. Trader
13. Aggressive risk debater
14. Neutral risk debater
15. Conservative risk debater
16. Portfolio manager

The local adaptation changes semantics, not role names:

- `Trader` produces advisory trade intent, not executable orders.
- `Portfolio Manager` produces final suggested actions and exposure guidance,
  not broker instructions.
- All role outputs must cite evidence IDs where feasible.
- Analyst outputs become part of the analyst-flow evidence and stock profile
  history.
- The quality gate can block recommendation generation.

The first implementation phase may call the external repository as an installed
editable Python dependency or through a thin local adapter. It should not copy
large external source files into this repository unless a later implementation
plan justifies vendoring.

### 6.6 Stock profiles

The stock profile is the durable memory for every stock the advisor notices.

Profile creation triggers:

- A stock appears in accepted MX events.
- A stock appears in the 08:30 advice or 22:30 review.
- A stock appears in analyst outputs as a candidate, risk, peer, or sector
  driver.
- A stock exists in the ledger as a holding or historical trade.
- A stock is manually added by the user.

Profile dimensions:

| Dimension | Content |
| --- | --- |
| Identity | code, name, exchange, board, industry, concepts |
| Thesis | core logic, catalyst, expected holding window, invalidation |
| Information flow | MX events, news, announcements, policy references |
| Capital flow | price trend, liquidity, fund flow, dragon-tiger, sector strength |
| Fundamentals | valuation, financial snapshot, profitability, balance-sheet risks |
| Analyst flow | analyst conclusions, bull/bear conflicts, risk-debate summary |
| Ledger exposure | current position, cost, realized/unrealized PnL, advice alignment |
| History | prior advice, review results, profile changes |
| Assets | K-line chart paths and report asset paths |

Profiles should be stored as structured rows in the advisor database and
rendered as Markdown snapshots under `data/advisor/profiles/<code>.md` or
`reports/YYYY-MM-DD/profiles/` when included in a daily report.

### 6.7 Investment ledger

The ledger tracks user trading state. It is read/write local state but has no
broker execution ability.

First-phase input methods:

- Manual frontend entry.
- CSV import using a documented local schema.
- CLI import for repeatable testing.

Ledger entities:

- Cash movements.
- Trades.
- Fees and taxes.
- Positions.
- Daily portfolio snapshots.
- Advice-to-action links.
- Realized and unrealized PnL.

Ledger rules:

- A-share lot size and T+1 constraints are used for analytics and consistency
  checks.
- User-entered trades are treated as historical facts after validation.
- Generated advice is stored separately from actual user trades.
- The 22:30 review compares advice, market outcome, and actual ledger action
  when the user has recorded trades.

### 6.8 Daily reports and archive

The reporting subsystem has two scheduled report families.

#### 08:30 premarket advice

Output path:

- `reports/YYYY-MM-DD/premarket.md`
- `reports/YYYY-MM-DD/premarket.json`
- chart assets under `reports/YYYY-MM-DD/assets/`

Required sections:

- Data-quality status.
- Market regime and major index context.
- Information-flow highlights.
- Capital-flow highlights.
- Analyst-flow synthesis.
- Suggested watchlist and candidate actions.
- Current ledger exposure and risk.
- Entry conditions, invalidation conditions, and risk controls.
- Evidence list.
- Research disclaimer.

#### 22:30 daily review

Output path:

- `reports/YYYY-MM-DD/review.md`
- `reports/YYYY-MM-DD/review.json`
- chart assets under `reports/YYYY-MM-DD/assets/`

Required sections:

- Data-quality status.
- What the 08:30 advice said.
- What happened in the market.
- Candidate-by-candidate review.
- Evidence that worked, failed, or was missing.
- Ledger impact if the user recorded trades.
- Stock-profile updates.
- Next-day carryover watchlist.
- Research disclaimer.

Reports are immutable once archived except for explicitly versioned reruns. A
rerun writes a new `run_id` and records why it replaced or superseded a prior
report.

### 6.9 K-line chart generation

The chart generator produces PNG files for reports, frontend previews, and stock
profiles.

Minimum first-phase charts:

- Daily candlestick chart.
- Volume bars.
- Moving averages.
- Advice markers.
- User trade markers.
- MX event markers when mapped to the stock.
- Limit-up and limit-down context when available.

Output paths:

- `reports/YYYY-MM-DD/assets/<code>-kline.png`
- `data/advisor/charts/<code>/<date>-kline.png`

Charts must be generated from local stored market data, not live-only transient
responses, so report reruns are reproducible.

### 6.10 Frontend dashboard

The frontend is a local dashboard for operating the advisor and viewing current
investment state. It should open directly to the working interface, not a
marketing landing page.

Core first-screen information:

- Current date, last successful data update, and report status.
- 08:30 advice summary.
- Current ledger state: cash, positions, exposure, realized and unrealized PnL.
- Watchlist and high-priority stock profiles.
- Information-flow, capital-flow, and analyst-flow status.
- Data-quality and scheduler health.

Required workflows:

- Open a daily report archive.
- Open a stock profile.
- View K-line chart assets.
- Add or import ledger trades.
- See blocking data-quality failures.
- See whether the collector, market updater, advisor scheduler, and frontend are
  alive.

The first implementation should use a local FastAPI service for the advisor API
and a Vite/React dashboard for the browser UI. The API owns data access and
report/profile retrieval; the frontend renders the current state and submits
ledger entries or imports through documented API endpoints.

### 6.11 Scheduler and Mac keepalive

The local runtime uses macOS `launchd` for keepalive and timed runs.

Managed processes:

- Existing MX collector, started only through the approved Node 24 command flow
  and only after required self-tests.
- Market data updater.
- Advisor scheduler.
- Frontend server.

Scheduled jobs:

- Trading-day premarket refresh before 08:30.
- 08:30 advice report.
- Post-close market-data completion checks.
- 22:30 review report.
- Periodic health snapshots.

Keepalive rules:

- If collector self-test fails, the collector stays stopped.
- If Chrome login expires, collection stops and asks the user to restore login.
- If market data quality fails, advice generation is blocked and a failure
  report is archived.
- If the frontend fails, it can restart without altering collector or advisor
  data.
- Logs and PID/state files stay under `data/state/` or `logs/`.

## 7. Data-quality gates

Each daily report run has a quality gate before recommendations are generated.

Blocking failures:

- MX collector records a required data-quality failure.
- The trading calendar cannot determine whether today is a trading day.
- Market data is stale beyond the configured tolerance.
- Three-year history is missing for a required candidate and the report cannot
  explain the gap.
- Evidence includes data published after the report `as_of`.
- The ledger has invalid or unbalanced transactions.
- Analyst outputs cannot be parsed into the required contract.

Non-blocking degraded states:

- A single optional free source is unavailable and another source covers the
  field.
- A stock has incomplete fundamentals but price, liquidity, and evidence gaps
  are clearly disclosed.
- A K-line chart for a non-core stock fails while the report can still cite
  structured data.

Blocking failures produce an archived failure report and no stock advice.

## 8. Advisor data model

The final schema is an implementation-plan item, but the design requires these
logical tables:

- `advisor_runs`: scheduled and manual run metadata.
- `data_quality_checks`: quality checks, severity, and blocking status.
- `securities`: stock identity and classification.
- `market_daily`: three-year OHLCV and derived daily fields.
- `market_sources`: raw-source metadata and fetch status.
- `events_normalized`: MX and external events mapped to securities.
- `evidence`: report-ready evidence objects.
- `analyst_outputs`: outputs from each analyst role.
- `stock_profiles`: current profile state.
- `stock_profile_history`: profile updates by run.
- `advice`: 08:30 advice objects.
- `reviews`: 22:30 review objects.
- `ledger_accounts`: cash and portfolio accounts.
- `ledger_transactions`: cash movements, trades, fees, and adjustments.
- `positions`: current and historical position state.
- `portfolio_snapshots`: daily exposure and PnL.
- `chart_assets`: generated chart metadata.
- `report_archive`: immutable report files, run IDs, and replacement links.

All tables that feed advice need `created_at`, `updated_at`, `as_of`, source
fields where applicable, and schema versions.

## 9. Directory layout

Recommended new directories:

```text
a_hunter/
|-- advisor/
|   |-- data_sources/
|   |-- agents/
|   |-- evidence/
|   |-- profiles/
|   |-- ledger/
|   |-- reporting/
|   |-- charts/
|   |-- scheduler/
|   `-- web/
|-- config/
|   |-- advisor.yaml
|   |-- data-sources.yaml
|   |-- ledger-import.schema.json
|   `-- launchd/
|-- data/
|   |-- advisor/
|   |-- media/
|   `-- state/
|-- reports/YYYY-MM-DD/
|-- tests/
|   |-- advisor/
|   |-- unit/
|   `-- integration/
`-- docs/superpowers/
```

The existing collector directories remain in place. New advisor code should not
move or rewrite collector files unless a later implementation plan specifically
requires a safe adapter change.

## 10. First-phase implementation boundary

The first implementation phase should deliver a minimal end-to-end advisor
loop:

1. Advisor configuration and database migrations.
2. Free-source market data adapter scaffold and resumable three-year daily
   K-line backfill for the full A-share universe. Test and smoke environments
   may run a configured stock subset, but production completeness is measured
   against the full configured A-share universe.
3. K-line PNG generation from stored data.
4. Stock profile creation and updates from MX events, advice, ledger entries,
   and analyst outputs.
5. Investment ledger with manual and CSV import.
6. TradingAgents-astock adapter for the full analyst role set.
7. 08:30 premarket report command and archive.
8. 22:30 review command and archive.
9. Local dashboard with current state, reports, profiles, charts, ledger, and
   health status.
10. `launchd` plist templates and operational documentation for Mac keepalive.
11. Self-tests and integration tests covering the main gates.

The first phase does not need automated strategy promotion, paid data, broker
integration, or a full historical backtesting lab.

## 11. Testing and verification

Required tests:

- Free-source adapter contract tests with recorded or synthetic responses.
- Source timestamp and `as_of` leakage tests.
- Three-year database completeness checks.
- K-line chart generation smoke tests that verify non-empty PNG output.
- Stock profile creation and update tests.
- Ledger import, position, cash, fee, and PnL tests.
- Advice-to-review linkage tests.
- Report archive immutability tests.
- Data-quality blocking tests.
- TradingAgents adapter output-contract tests.
- Frontend API tests for current state and report/profile retrieval.
- `launchd` template validation by static inspection.
- Existing collector self-test remains mandatory after collector-adjacent
  changes and before collector starts.

## 12. Operational flow

Daily flow on a trading day:

1. Keep the authorized MX collector running under the existing safety rules.
2. Before 08:30, update market data and normalize overnight events.
3. Run data-quality checks.
4. Build 08:30 evidence packet.
5. Run analyst flow and generate advice.
6. Archive `premarket.md`, `premarket.json`, and chart assets.
7. During the day, continue collecting MX events and allow user ledger updates.
8. After market close, update market data and ledger snapshots.
9. At 22:30, build review evidence linked to the morning advice.
10. Run review analysis, update stock profiles, and archive review outputs.
11. Frontend reflects the latest run status and archived artifacts.

## 13. Acceptance criteria

The design is implemented when current-state evidence proves all of the
following:

1. The advisor uses A-share defaults and free data sources learned from
   `TradingAgents-astock`.
2. No required data path uses Tushare.
3. The existing MX collector continues to fail closed on empty allowed RIDs.
4. 08:30 advice and 22:30 same-day review can be generated and archived.
5. The 22:30 review links back to the 08:30 advice IDs.
6. Stock profiles are created for all stocks surfaced by events, advice,
   analyst outputs, or ledger holdings.
7. The local database contains recent three-year market data for the configured
   A-share universe or explicitly configured subset.
8. K-line PNG files can be generated from stored market data and referenced by
   reports and profiles.
9. The ledger tracks user-entered trades, cash, positions, fees, and PnL without
   submitting orders.
10. The frontend displays current advice, ledger state, reports, profiles,
    charts, data quality, and process health.
11. Mac keepalive and scheduling are documented and represented by launchd
    templates.
12. Blocking data-quality failures prevent stock recommendations and produce an
    archived failure report.
13. Tests cover the critical data, report, ledger, chart, frontend, and safety
    gates.

## 14. Implementation sequencing after this spec

After the user reviews and approves this written spec, the next step is to use
the writing-plans workflow to create a detailed implementation plan. The plan
should break the work into independently testable stages, starting with schema,
data adapters, and report contracts before frontend polish.
