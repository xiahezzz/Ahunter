# A Hunter Research

A Hunter turns time-bounded market and event evidence into traceable research conclusions while keeping publication subject to explicit quality controls.

**LAgent Mode**: Free research driven by exactly one logical main investigator.
It selects any configured Data Product and may create arbitrary task-specific
subagents, which return findings to the main investigator. It shares the durable
Research Request queue and publication quality boundary but does not run a Team or
the fixed Decision Pipeline. Instructions, model settings, data selection and
execution limits are versioned configuration. Host-owned action records expose
the task tree, evidence, queries, attempts, usage and failures without private reasoning.

## Language

**LAgent Inner Loop**:
A research process in which a fixed LAgent version generates, evaluates, and improves solutions to one task using feedback made available for that task.
_Avoid_: modifying LAgent itself, one model call, one simulated trading day

**LAgent Outer Loop**:
A research process that proposes changes to the LAgent conducting the Inner Loop and selects versions by evaluating their ability to improve task solutions.
_Avoid_: tuning a trading solution, subagent delegation, portfolio rebalancing

**LAgent Experiment Platform**:
The host of LAgent experiments whose rules and conditions remain fixed while candidate LAgent versions are compared, independently of either optimization loop.
_Avoid_: outer-loop agent, tunable LAgent implementation

**LAgent Outer Optimizer**:
The decision maker in the LAgent Outer Loop that proposes and selects changes to the inner LAgent's implementation and permitted configuration while keeping the Experiment Platform's rules unchanged.
_Avoid_: experiment platform, evaluator, inner-loop strategy search

**LAgent Evolution Tree**:
The observable lineage of candidate LAgent versions, showing where alternatives branch, their stated improvement hypotheses, linked evaluations, and subsequent selection decisions. It distinguishes manual submissions from optimizer-generated candidates and retains unsuccessful branches as possible parents for newly evaluated candidates without undoing their prior selection history.
_Avoid_: subagent task tree, model reasoning tree, equity curve, success-only history

**LAgent Test Record**:
The permanently retained lifecycle and outcome of one test of a specific LAgent version under an exact Experiment Specification, including incomplete and unsuccessful tests. Repeated tests have distinct records, and later reruns or version-selection decisions never replace their history.
_Avoid_: latest score, successful-run-only report, disposable test log

**LAgent Promotion Score**:
The configured aggregate used to compare a candidate with the current baseline on a fixed task set; its default is mean terminal return, averaging planned repeats within each task and then applying the configured task weights. It refers to the underlying Test Records and never substitutes for their individual outcomes or validity.
_Avoid_: best single run, parent-version improvement alone, cross-experiment global score

**LAgent Baseline Promotion**:
The automatic selection of an evaluated candidate as the experiment's current baseline after it meets the experiment's preset promotion rules against that baseline. It preserves prior comparison and selection history and does not replace a running Episode's version or deploy a business configuration.
_Avoid_: production deployment, candidate mutation, replacing historical test results

**LAgent Tuning Task**:
An evaluation task whose completed-test feedback may be repeatedly used to improve and select candidate LAgent versions, while each Episode still obeys its historical information cutoffs. Performance on this task is tuning evidence rather than evidence from an unseen final holdout.
_Avoid_: final blind test, future information within an Episode, task with resettable feedback history

**LAgent Selection Validation Task**:
An evaluation task reserved for comparing candidate versions under a predetermined selection policy. Repeated selection feedback makes it part of the optimization process, even when detailed trajectories remain hidden from the optimizer.
_Avoid_: final unseen holdout, tuning trace, independent proof of generalization

**LAgent Final Holdout Task**:
A task reserved until candidate selection is frozen, used to measure the selected version against the initial baseline without feeding its results into the same selection process. Any exposure and later use of its feedback remain part of its permanent history.
_Avoid_: reusable secret tuning set, task made unseen by renaming

**LAgent Calibration Episode**:
A retained baseline run used to establish a comparable resource-cost allowance before formal comparisons begin. Its cost and outcome remain observable, but it is not also counted as a formal comparison sample.
_Avoid_: free warmup, selected low-cost run, duplicated evaluation sample

**LAgent Equal-Cost Evaluation**:
A comparison of LAgent versions under the same total resource-cost allowance per task, including their delegated work, retries, and evaluated strategy execution, without a separate token-count cap.
_Avoid_: unlimited-cost comparison, equal wall-clock duration, main-agent-only budget

**LAgent Research Budget Exhaustion**:
The point at which an Episode can no longer authorize agent research, while previously accepted instructions and existing positions continue through the original test period using resources reserved within the same total allowance. It stops further model work without implying liquidation, early return measurement, or automatic order renewal.
_Avoid_: user cancellation, infrastructure failure, shortened evaluation period

**LAgent Evaluation Episode**:
One stateful research-and-decision run by a fixed LAgent version across simulated market days, using only information available in each permitted Decision Window. Its positions and task memory persist across windows, and its sole main investigator may delegate research within those windows.
_Avoid_: sealed list of trades, one daily Research Request, strategy-code submission

**LAgent Decision Window**:
A configured premarket or postmarket period for LAgent research and decisions during an Evaluation Episode, fixed for the experiment and shared by its main investigator and subagents.
_Avoid_: event wake-up, intraday decision point, agent-chosen observation time

**LAgent Window Snapshot**:
The fixed information view shared by the main investigator and all subagents throughout one scheduled Decision Phase, containing only evidence available to the Episode by its recorded cutoff. Actual research duration does not advance that cutoff or the simulated market clock.
_Avoid_: live updating feed, auction timestamp without publication evidence, wall-clock deadline

**LAgent Search Time Policy**:
The experiment's declared trust in a search service's date filtering for the historical cutoff applied to every web query, without independently verifying each page's archived version. Date-only material becomes eligible after its stated date ends, and this policy does not imply intraday timestamp precision or prove the absence of later edits.
_Avoid_: prompt-only cutoff, unrestricted live search, independently verified web archive

**LAgent Decision Phase**:
A predetermined research-and-decision stage within a permitted Decision Window, with its own fixed information snapshot. The premarket window has stages before and after the opening auction, permitting both buying and selling with currently available resources and allowing confirmed, usable auction sale proceeds to inform the later plan without an event-triggered wake-up.
_Avoid_: intraday callback, agent-chosen observation time, advancing information during model execution

**LAgent Premarket Plan**:
The main investigator's research-backed plan and simulated standing instructions finalized in a scheduled premarket Decision Phase for subsequent environment execution. Each phase's plan is accepted only when all instructions and their combined account requirements pass validation; later fills remain independent, and rejection of a subsequent plan never undoes earlier executions.
_Avoid_: intraday discretionary trading, guaranteed fills, real broker orders

**LAgent Postmarket Review**:
The main investigator's review in the configured postmarket window of the day's plan, simulated execution, and information available by the review instant, retained as task memory for subsequent permitted decisions.
_Avoid_: outer-loop code modification, retroactive trade correction, intraday review

**LAgent Day Limit Order**:
A premarket simulated instruction specifying a security, buy or sell direction, quantity, and limit price for one trading day; any unfilled remainder expires at that day's end. It is executed by the environment without further agent decisions and carries no promise of a fill.
_Avoid_: conditional order, market order, real broker order, persistent multi-day order

**LAgent Premarket Cancellation**:
A request fixed in the post-auction premarket phase to cancel the unfilled remainder of an existing simulated order, executed by the environment when market rules permit. Acceptance of the request does not confirm cancellation or release the order's reserved resources, and intervening fills remain valid.
_Avoid_: immediate resource release, undoing a fill, intraday agent decision, automatic replacement order

**LAgent Premarket Replacement**:
A precommitted process that cancels an existing simulated order and, after confirmed cancellation and renewed validation, submits a same-security, same-direction DAY limit order at a price fixed before the decision cutoff. Its replacement quantity is the fixed combined target less the old order's cumulative fills; it neither guarantees replacement nor permits new intraday agent decisions.
_Avoid_: atomic exchange amendment, dynamic repricing, duplicate target quantity, general conditional order

**LAgent Trading Universe**:
The historical set of A-share securities on the Shanghai and Shenzhen exchanges eligible for consideration in an Evaluation Episode, excluding the Beijing exchange. Membership does not itself establish current tradability, account eligibility, or executable liquidity.
_Avoid_: today's surviving-stock list, all exchange-listed instruments, all securities mentioned in research

**LAgent Account Policy**:
The configurable funding and account rules governing how an Evaluation Episode may use cash, holdings, and any permitted credit or securities borrowing, fixed for comparable runs. Planned sale proceeds are unavailable until the sale is confirmed and the policy makes the net proceeds usable, including opening-auction proceeds used for a later premarket plan.
_Avoid_: token budget, initial capital, current buying power, agent-chosen leverage policy

**LAgent Execution Evidence**:
The historical market observations needed to assess simulated execution under the experiment's declared fill model. Formal ordinary limit-order evaluation requires at least historical minute data, with finer evidence where the model requires it; a touched price alone is not a guaranteed fill.
_Avoid_: agent-reported fill, daily-range guarantee, proof of actual broker execution

**LAgent Fixed-Path Replay**:
An execution simulation in which candidate orders face the same historical market path without changing subsequent market prices or other participants' behavior. Fills remain subject to the experiment's timing, liquidity, queue, slippage, and fee rules rather than being guaranteed by a touched price.
_Avoid_: market-impact simulation, unlimited liquidity, cost-free execution, guaranteed historical-price fill

**LAgent Experiment Specification**:
The resolved definition of an experiment's task period, initial account, environment, evaluation rules, resource allowances, and permitted agent variations, fixed before comparable runs begin. A reusable preset supplies values for a specification without turning those values into permanent platform rules.
_Avoid_: hidden defaults, mutable running experiment, hard-coded five-day test

**LAgent Terminal Return**:
The return computed from an Evaluation Episode's initial and terminal account equity under its fixed valuation and cash-flow policy, including remaining positions without inventing liquidation fills. This experiment measures terminal equity at the last trading session's close.
_Avoid_: cash-only profit, forced liquidation proceeds, research-cost-adjusted score

**Research Engine**:
The A Hunter-owned capability that coordinates research work and produces evidence-backed research conclusions without depending on another research application.
_Avoid_: TradingAgents runtime, external analyst framework

**Local Operator Console**:
The WebUI and its API exposed only through the machine's loopback interface for its owner to inspect and control A Hunter; it is never a shared, LAN, or Internet-facing service.
_Avoid_: hosted dashboard, remote administration console, LAN service

**Research Agent**:
An independent, Team-agnostic specialist that produces one structured research finding for a subject and time boundary from explicitly declared data products.
_Avoid_: workflow node, analyst function

**Research Scope**:
The classification of whether a Research Agent or Research Team studies the whole Shanghai-Shenzhen A-share market or one Market Security. A published legacy Manifest that predates this declaration is permanently classified as Security Research Scope.
_Avoid_: data breadth, Data Product scope, feed scope

**Research Scope Parity**:
The rule that Market and Security Agents and Teams share the same identity, versioning, publication, data-access, Capsule, query, execution, retry, audit, and lifecycle semantics. Research Scope alone changes the Research Subject and the directly compatible input and conclusion contracts selected for that subject.
_Avoid_: Market Agent subsystem, scope-specific Team lifecycle, duplicated execution rules

**Market Research Scope**:
A Research Scope whose subject is the Shanghai-Shenzhen A-share market as a whole at a time boundary and therefore requires no Market Security. It may identify noteworthy industries or securities but does not produce a conclusion for every security.
_Avoid_: all-stock batch, one report per security, broad input data

**Security Research Scope**:
A Research Scope whose subject is one Market Security at a time boundary.
_Avoid_: stock list, candidate batch, market-wide research

**Research Subject**:
The exact object studied by a Research Cycle: the whole Shanghai-Shenzhen A-share market for Market Research Scope, or one Market Security for Security Research Scope. Its Scope must match every Research Team selected by the Cycle.
_Avoid_: input code, candidate batch, report target

**Research Boundary**:
The immutable evidence cutoff for one Research Cycle. A Manual Security Research Request fixes it when accepted, while a Manual Market Research Request fixes it when the Research Service starts execution and seals its Whole-Market Intraday Snapshot; evidence after the applicable instant remains excluded.
_Avoid_: completion time, browser-supplied time, unrecorded moving cutoff

**Agent Manifest**:
The immutable, versioned declaration of a Research Agent's identity, Research Scope, Agent Data Access, Agent Instructions, and Research Finding contract. Research Scope remains unchanged across every version of one stable Agent identity.
_Avoid_: agent class registration, plugin metadata

**Agent Instructions**:
The owner-authored, versioned guidance that defines a Research Agent's specialist task; engine-owned execution constraints are combined with it at invocation time but are not part of the editable guidance.
_Avoid_: complete prompt, system prompt, runtime prompt

**Agent Instructions Draft**:
A mutable, non-persistent Local Operator Console form based on the latest Agent Manifest that may change only Agent Instructions; leaving with unpublished changes requires explicit discard confirmation.
_Avoid_: saved prompt, draft Agent Manifest, persistent draft

**Agent Instructions Revision**:
A versioned change that creates the next immutable Agent Manifest with revised Agent Instructions while preserving every previous version and every non-instruction field from the latest Manifest. The version stream remains linear: reusing historical Instructions still creates the next version rather than branching or reactivating history.
_Avoid_: prompt edit, in-place Agent update, Agent overwrite

**Agent Data Access**:
The immutable declaration of the exact Data Products and bounded product-specific feed scopes exposed to a Research Agent invocation under the same access rules for Market and Security Agents; Provider identities are provenance, not selectable Agent permissions.
_Avoid_: Provider access, runtime grant, Agent tool list

**Agent Access Draft**:
A mutable, non-persistent WebUI form based on one exact Agent Manifest version that may change only Agent Data Access while preserving the remaining Agent contract unchanged.
_Avoid_: Agent editor, mutable Agent Manifest, saved draft

**Agent Access Publication**:
The validation and atomic creation of the next immutable Agent Manifest version from an Agent Access Draft, without changing existing Agent or Team versions.
_Avoid_: permission update, in-place Agent edit, automatic Team upgrade

**Research Finding**:
A structured, evidence-linked conclusion produced by one Research Agent for a single subject and time boundary.
_Avoid_: analyst output, raw payload

**Data Product**:
A normalized, time-bounded research dataset with explicit provenance and quality status, independent of the external source that supplied it.
_Avoid_: vendor response, tool output

**Data Product Manifest**:
The repository-owned, versioned declaration of a Data Product's request and result contracts, time semantics, freshness, conflict tolerance, and retention rules.
_Avoid_: provider configuration, Agent tool list

**Market Information Product**:
The time-bounded, provenance-preserving Data Product used by Market Agents for macroeconomic, policy, regulatory, exchange, and material market events. It prioritizes original publications from competent public authorities and exchanges, uses financial media as supplemental context, and never presents unverified rumor as fact.
_Avoid_: stock-code news search, rumor feed, source-free market narrative

**Historical Shanghai-Shenzhen A-share Universe**:
The versioned set of all Shanghai and Shenzhen RMB-denominated common stocks listed at any point in a requested history window, including ST, suspended, and later-delisted stocks while excluding Beijing-listed stocks, B shares, funds, bonds, and indexes.
_Avoid_: all-exchange A-share universe, current stock list, active tickers

**Market Security**:
A Shanghai or Shenzhen common stock identified canonically by its exchange-assigned six-digit code and bounded by its listing and delisting dates.
_Avoid_: surrogate security ID, code alias

**Canonical Daily Bar**:
The single immutable, unadjusted OHLCV and turnover fact for one security and trading date, normalized to CNY per share, shares, and CNY; adjustment factors are stored separately so adjusted price series can be derived at query time.
_Avoid_: adjusted stored bar, bar revision history, rewritten historical price

**Research Price Series**:
The forward-adjusted series derived from Canonical Daily Bars and a pinned adjustment-factor version for cross-session returns and technical indicators, while actual price levels remain unadjusted.
_Avoid_: stored adjusted bar, raw-price technical series

**Whole-Market Intraday Snapshot**:
An immutable, time-bounded Data Product containing contemporaneous observations and evidenced coverage for the active Shanghai-Shenzhen A-share market, used for intraday breadth, sentiment, and sector-strength research without creating one Research Run per security.
_Avoid_: yesterday's close, per-security quote batch, live mutable feed

**Whole-Market Snapshot Provider**:
A Provider Adapter that returns one coherent bulk observation for the expected market under the Whole-Market Intraday Snapshot contract. Fallback replaces the entire observation; per-security fan-out and cross-source splicing are excluded.
_Avoid_: quote loop, field merger, partial-source patch

**Whole-Market Intraday Readiness**:
The quality assertion that a Whole-Market Intraday Snapshot covers at least 99% of expected Market Securities and at least 95% of every sector after evidenced suspensions and out-of-listing securities are counted as legitimate absences. Actual coverage is always disclosed, and failing either threshold blocks dependent Market Insights.
_Avoid_: perfect quote count, best-effort breadth, undisclosed missing securities

**Industry Sector Taxonomy**:
The repository-owned, versioned first-level Eastmoney industry-membership snapshot that assigns Market Securities to the industry sectors used by sector-strength research. A Research Record pins its taxonomy version independently of its Whole-Market Snapshot Provider so quote-source fallback cannot change sector membership.
_Avoid_: quote-provider sector label, concept board, geographic board, style index, unversioned classification

**Industry Sector Taxonomy Refresh**:
The once-per-trading-date, on-demand attempt made by the Research Service when the first Market Research needing sector membership begins. A successfully sealed taxonomy version is reused and pinned by every later Research Record on that trading date.
_Avoid_: per-run taxonomy fetch, mutable intraday membership, unpinned latest classification

**Industry Sector Taxonomy Readiness**:
The quality assertion that sector-dependent Market Insights use either the current trading date's sealed taxonomy or the latest successful version no more than five trading dates old, with its version and age disclosed. An older or absent taxonomy blocks only sector-strength and sector-rotation Insights; independent market-sentiment and macro-policy Insights may continue.
_Avoid_: undisclosed stale membership, unlimited taxonomy fallback, whole-report failure

**Sector Strength Assessment**:
A sector-oriented Market Insight whose analysis method is chosen by the executing Market Agent from pinned market evidence. It discloses a structured method summary covering inputs, time windows, strength or rotation criteria, ranking or grouping basis, and evidence references without exposing prompts or private model reasoning. The Market Decision Pipeline validates the Insight contract but does not impose a shared strength formula, composite score, or ranking method across Agents.
_Avoid_: engine-owned sector score, universal sector ranking formula, hidden method, chain-of-thought disclosure, uncontracted output

**Market Daily Cold Start**:
The one-time materialization of Canonical Daily Bars from the latest completed trading session back five calendar years for the Historical Shanghai-Shenzhen A-share Universe, after which history is retained and only new sessions are appended.
_Avoid_: rolling five-year cache, current-stock backfill

**Market Daily Catch-up**:
The recurring load that refreshes Market Securities and fetches each security's missing interval from its last evidenced coverage through the latest completed session, normally reducing to one new day.
_Avoid_: today-only append, full-history reload

**Market Daily Ingestion Run**:
A resumable whole-universe load whose per-security writes commit independently while full-market readiness is sealed only after every expected security reaches an evidenced terminal outcome.
_Avoid_: one giant transaction, best-effort import

**Market Daily Service**:
The long-running project-owned application service that owns security refresh, cold start, catch-up, resumability, quality state, progress, and due-time execution for Market Daily ingestion independently of how its process is hosted.
_Avoid_: Research Agent, launchd job, scheduler script

**Market Daily Trigger Adapter**:
A thin project-owned entry point that invokes the Market Daily Service through CLI, Web API, or host scheduling without owning ingestion rules or run state.
_Avoid_: ingestion service, workflow implementation

**Market Daily Request**:
A durable, idempotent request written to the advisor database by a trigger adapter and claimed exclusively by the running Market Daily Service.
_Avoid_: in-process callback, duplicate background task

**MX Listener Service**:
The long-running project-owned service that passively records user-authorized MX events; a ready, logged-in MX browser session remains an operator-provided authorization boundary outside the service.
_Avoid_: MX browser automation, collector script, launchd job

**Dedicated MX Chrome Launcher**:
The explicit local WebUI operator action that starts the fixed Chrome executable with one fixed loopback debugging endpoint and an isolated MX profile; it launches the application only, while navigation, login, page interaction, and authorization remain with the operator.
_Avoid_: browser automation, general-purpose browser launcher, MX Listener Service

**MX Listener Liveness**:
The evidence that one hosted MX Listener Service instance is presently active, independent of browser readiness, processing health, or recent MX activity.
_Avoid_: MX Listener Readiness, recent event activity

**MX Listener Readiness**:
The observable ability of the MX Listener Service to attach passively to an operator-provided MX page and receive Chrome DevTools Network events; message silence alone neither disproves readiness nor proves that authorization expired.
_Avoid_: login status, recent accepted-event count, process liveness

**MX Listener Health**:
The assessment of whether the active MX Listener Service can safely process and store authorized events without unresolved internal faults, independent of browser readiness and message activity.
_Avoid_: MX Listener Readiness, recent event activity

**RID Authorization Set**:
The owner-declared set of positive integer MX room identifiers whose inbound events the MX Listener Service may record; an empty set means collection is deliberately inactive.
_Avoid_: discovered rooms, observed RIDs, subscribed channels

**Accepted MX Event**:
An MX event that belonged to the RID Authorization Set when received and passed the Listener's acceptance rules; it remains a historical record if that RID is later removed from the set.
_Avoid_: currently authorized event, observed frame, unfiltered MX message

**MX RID Feed**:
The immutable, time-bounded package of quality-checked Accepted MX Events from exactly one RID, preserved as a separately identified input within an MX event Data Product.
_Avoid_: Provider Adapter, per-RID product type, mixed-RID event list

**MX Information View**:
The owner-facing historical view of all Accepted MX Events, rendered from safe normalized text, structured content, media, timestamps, and provenance whether or not their RID remains currently authorized.
_Avoid_: raw-payload browser, current-RID-only feed, research conclusion

**A Hunter Service Set**:
The uniformly managed collection of independently hosted long-running project services, currently the MX Listener Service, Market Daily Service, and Research Service, with shared lifecycle and status surfaces but separate runtimes and failure domains.
_Avoid_: monolithic daemon, unrelated launch jobs

**Market Session Coverage**:
The evidenced outcome for one security on one target session: a validated traded bar, a legitimate absence such as suspension or being outside the listing interval, or an unresolved source failure.
_Avoid_: synthetic zero-volume bar, missing row treated as success

**Market Daily Readiness**:
A scope-specific quality assertion: whole-universe work requires a complete Market Daily Ingestion Run, while single-security work requires complete coverage only for that Market Security.
_Avoid_: one global availability flag, partial universe treated as complete

**Observed Trading Session**:
A completed Shanghai-Shenzhen market date evidenced by matching benchmark daily observations from the primary and fallback market sources and persisted locally for scheduling and quality checks.
_Avoid_: predicted weekday, hard-coded holiday assumption

**Provider Adapter**:
An A Hunter-owned implementation that obtains one provider observation for a declared Data Product through the Research Engine's narrow fetch interface.
_Avoid_: Agent tool, global vendor router, external plugin

**Conflicted Data Product**:
A Data Product whose provider observations disagree beyond its versioned tolerance and therefore cannot enter a sealed Research Data Snapshot.
_Avoid_: averaged source, best-effort merge

**Research Data Snapshot**:
The immutable collection of Data Products sealed for one Research Cycle and exposed to each Invocation only through its declared subset.
_Avoid_: live vendor state, mutable agent context

**Research Query Interface**:
The shared, read-only capability through which either a Market Agent or Security Agent filters, searches, or aggregates only its declared Data Products inside the current sealed Snapshot, with every query and result hash recorded. Agent queries have no count, result-row, result-byte, security-count, rank-size, or session-window budget. Scope-appropriate operations may evolve through the versioned interface without creating a separate access, isolation, or audit model for Market Agents.
_Avoid_: Market-only execution rules, live provider tool, private Agent memory, unrestricted artifact search

**Research History Window**:
The full locally retained history visible at or before a Research Boundary through a declared historical Data Product. Each Agent chooses and discloses the working time window through the shared Research Query Interface rather than receiving an engine-fixed lookback.
_Avoid_: mandatory 5-day window, mandatory 20-day window, evidence after boundary

**Research Artifact Store**:
The local, content-addressed store of immutable Research Cycle inputs and outputs, indexed by SQLite and governed by source-specific retention rights.
_Avoid_: report directory, mutable database row, Git-tracked runtime data

**Decision Stage**:
An engine-owned step that synthesizes research findings or transforms them into a bounded research conclusion.
_Avoid_: Research Agent

**Decision Review**:
A structured assessment produced independently by a fixed Bull, Bear, Aggressive, Neutral, or Conservative Decision Stage and seen together only by its downstream manager.
_Avoid_: debate message, peer conversation, Research Finding

**Research Team**:
A named, versioned, non-empty selection of Research Agents that all share one Research Scope and whose membership expresses one investment style without any universally required Agent. Different stable Team IDs cannot publish the same exact Agent membership.
_Avoid_: workflow, selected-agents list, mandatory-core Team, duplicate-style Team

**A-share Core Team**:
The initial Security Research Team `a_share_core@1`, containing the Security-scoped Market, Social, News, Fundamentals, Policy, Hot Money, and Lockup Research Agents.
_Avoid_: all-agent graph, permanent universal team

**Market Breadth Agent**:
The initial Market Agent `market_breadth@1`, responsible for whole-market breadth, trading sentiment, and liquidity without issuing per-security conclusions.
_Avoid_: Security Market Agent, stock selector, market-wide Team

**Sector Rotation Agent**:
The initial Market Agent `sector_rotation@1`, responsible for first-level industry strength and rotation using an Agent-chosen, disclosed method without recommending securities.
_Avoid_: concept-board Agent, engine sector score, stock recommender

**Market Macro Policy Agent**:
The initial Market Agent `market_macro_policy@1`, responsible for macroeconomic, policy, and market-information implications at whole-market scope without issuing per-security conclusions.
_Avoid_: Security Policy Agent, company-news Agent, stock recommender

**A-share Market Overview Team**:
The initial Market Research Team `a_share_market_overview@1`, containing exactly `market_breadth@1`, `sector_rotation@1`, and `market_macro_policy@1`. It never emits Research Candidates or per-security conclusions.
_Avoid_: stock screener, candidate recommender, A-share Core Team

**Team Manifest**:
The immutable, versioned declaration that pins a Research Team's Research Scope and the exact identities and versions of its Research Agents, and is never overwritten, deleted, or archived after publication. Research Scope remains unchanged across every version of one stable Team identity.
_Avoid_: runtime agent selection, team script, mutable Team record

**Team Draft**:
A mutable, non-persistent WebUI form containing an owner-assigned stable Team ID, Research Scope, Chinese title, and latest-version Agent identities of that same Scope that cannot be selected by a Research Run; revisions retain and lock both Team ID and Scope, and leaving the form discards it.
_Avoid_: temporary Team, runtime Agent list

**Team Publication**:
The Scope-compatible validation, latest Agent-version resolution, and atomic creation of the system-assigned next repository-owned Team Manifest version from a Team Draft; published versions are never overwritten, and a manual Research Run may select any exact published version, including a historical version.
_Avoid_: save Team, run selected Agents

**Daily Team Set**:
The possibly empty, owner-selected set of exact published Team versions used by scheduled Daily Research Batches, with at most one version per stable Team ID, changed through a dedicated WebUI activation control independently of Team Publication.
_Avoid_: default Teams, all published Teams, auto-enabled Team

**Research Request**:
A durable submission that asks the Research Service to execute explicitly recorded research work, retaining its origin, selection, and Scope-specific Boundary policy independently of the submitting process. Every intentional trigger creates a distinct Request even when Team, subject, and date repeat; there is no per-day quota or Team-and-date deduplication.
_Avoid_: process invocation, HTTP task, scheduler callback, daily run slot

**Manual Research Request**:
A Research Request from the Local Operator Console for one exact published Team version to study one scope-matching Research Subject; it always records request acceptance time separately from its Scope-specific Research Boundary.
_Avoid_: synchronous HTTP run, browser task, ephemeral process spawn

**Research Trigger Adapter**:
A thin WebUI, scheduler, or CLI entry point that validates intent and submits a Research Request without running a Research Cycle or Daily Research Batch itself.
_Avoid_: Research Service, workflow executor, Codex launcher

**Research Service**:
The long-running project-owned service that exclusively claims and executes Research Requests independently of WebUI, scheduler, and CLI trigger processes, preserving pending work across restarts.
_Avoid_: Web API background task, CLI-owned run, scheduler-owned batch

**Research Execution Queue**:
The durable collection of accepted Research Requests from which the Research Service runs at most one Research Cycle at a time across the system. The active Cycle is never preempted; at each Cycle boundary, pending Manual Research Requests run in submission order before unstarted scheduled Cycles, while Research Invocations inside the active Cycle may still run concurrently under the Codex Execution Policy.
_Avoid_: parallel Cycle pool, HTTP task queue, serial Agent execution

**Research Record**:
The durable owner-facing lifecycle record of one research execution, retaining its origin, exact Team, Research Subject, request time, Research Boundary once fixed, status, and publication reference when one exists. It remains visible when execution is pending, running, passed, partial, blocked, failed, or cancelled and no Team Report exists.
_Avoid_: report, completed Cycle, output directory

**Research History**:
The Local Operator Console view of all Research Records across WebUI manual requests, scheduled requests, CLI requests, and existing published Cycles, with each origin identified explicitly.
_Avoid_: successful reports list, manual-only history, report-directory browser

**Research Report Explorer**:
The dedicated Local Operator Console area for querying Research Records through server-side pagination and Team-based filters without a fixed recent-result cap or separate home-page report concept. It defaults to published `passed` and `partial` Team Reports ordered by publication time from newest to oldest, but can include `blocked`, `failed`, and `cancelled` Records through a status filter. Its primary Team filter selects one stable Team identity across all versions, with an optional exact-version refinement; every result displays the exact Team version used. Research Agents are not independent report-filter dimensions.
_Avoid_: home-page Market Team, primary Team, latest-100 list, report-directory browser, client-side-only filtering, current-progress panel

**Research Record Detail**:
The Local Operator Console view that safely renders a passed or partial Team Report together with its exact Team version, Research Subject, request time, Research Boundary, and evidence quality. For a blocked, failed, or cancelled Record it renders the stopping stage and bounded reason without exposing raw logs, prompts, private model reasoning, or Codex session material.
_Avoid_: report file path, raw Markdown injection, log viewer, prompt inspector

**Research Progress**:
The bounded owner-facing projection of a Research Record's durable phase, completed and total Agent counts, current Decision Stage, last update time, and limited failure reason. It never exposes raw logs, prompts, model reasoning, or Codex session material.
_Avoid_: process log, model transcript, indeterminate running flag

**Current Research Queue View**:
The unfiltered Local Operator Console view of the single running Research Record followed by every queued Record in actual execution order, showing Team, Research Scope, Research Subject, origin, phase, progress, and available cancellation without depending on a selected Team.
_Avoid_: Team-filtered queue, one-record status card, hidden pending request, guessed queue order

**Research Cancellation**:
The owner-requested, auditable termination of a Research Request. A queued Request becomes cancelled immediately; a running Request records cancellation intent and stops at a safe execution boundary. Its Research Record and already persisted audit artifacts remain available permanently.
_Avoid_: delete request, erase history, kill-and-forget, cancel completed research

**Research Rerun**:
A new Manual Research Request created from any terminal Research Record, copying its exact Team version and Research Subject while receiving a new request time, Scope-specific Research Boundary, and Research Data Snapshot. It records its predecessor without resuming, replacing, or mutating it.
_Avoid_: retry completed request, overwrite report, reuse old boundary, mutable history

**Research Run**:
One auditable evaluation of a subject at a time boundary by one Research Team through the shared Decision Pipeline.
_Avoid_: report job, batch

**Research Cycle**:
The coordinated set of Research Runs for one subject and time boundary that share a sealed Research Data Snapshot and record the exact Team, Agent, Data Product, Decision Pipeline, and Codex Execution Policy versions used.
_Avoid_: multi-team aggregation, mutable batch

**Daily Research Batch**:
The scheduling envelope that launches one independent Research Cycle per selected subject under the same time boundary, Team selection, and Codex Execution Policy.
_Avoid_: multi-subject Cycle, portfolio-level conclusion

**Research Cycle State Machine**:
The explicit A Hunter-owned orchestration of snapshot materialization, bounded Agent execution, per-Team Decision Pipelines, and publication through durable stage boundaries.
_Avoid_: LangGraph, implicit callback graph, resumable Codex process

**Blocked Research Run**:
A Research Run that cannot produce a publishable team conclusion because a required invocation or quality condition did not complete successfully.
_Avoid_: partial team conclusion, degraded advice

**Research Invocation**:
One logical request to execute a Research Agent against a Research Data Snapshot through bounded Invocation Attempts, accepting at most one Research Finding.
_Avoid_: shared agent chat, team prompt

**Research Invocation Key**:
The stable identity derived from the Agent version, subject, time boundary, declared input artifact hashes, and Codex Execution Policy version, used to share one Invocation across dependent Teams in a Research Cycle.
_Avoid_: Team run ID, random task ID, cache by Agent name

**Invocation Attempt**:
One recorded Codex execution for a Research Invocation, using the same Run Capsule and Codex Execution Policy as every retry of that Invocation.
_Avoid_: alternative opinion, candidate finding

**Run Capsule**:
The isolated, immutable package of instructions, declared inputs, evidence, and output contract exposed to one Research Invocation or Decision Stage.
_Avoid_: repository workspace, shared agent context

**Codex Execution Policy**:
The repository-owned, versioned mapping from Research Invocation and Decision Stage classes to explicit Codex model, reasoning, timeout, concurrency, and invocation settings, applied identically across all Research Teams.
_Avoid_: user-default model settings, per-agent model choice, team-specific execution profile

**Decision Pipeline**:
An engine-owned sequence of quality gates, synthesis, independent review, final adjudication, and publication checks selected by Research Scope and applied identically to every Research Team within that Scope.
_Avoid_: team workflow, Team-customized pipeline, one cross-Scope pipeline

**Team Conclusion**:
The final evidence-linked, non-executing research conclusion produced by one Research Team under the fixed conclusion contract for its Research Scope.
_Avoid_: global recommendation, cross-team signal

**Security Team Conclusion**:
A Team Conclusion for one Market Security, containing a five-level Decision Stance, qualitative Decision Conviction, deterministic evidence quality, risks, invalidation conditions, and time horizon.
_Avoid_: market outlook, portfolio order, Market Team Conclusion

**Market Insight**:
A typed, evidence-linked unit within a Market Team Conclusion whose allowed kinds and fields are owned by the versioned Market Decision Pipeline; it may describe whole-market conditions, industry or theme views, style-based security selections, or another explicitly contracted market-level finding. It is either passed with contracted content or blocked with a bounded evidence or isolated-execution reason.
_Avoid_: mandatory market regime, free-form essay, per-security Team Conclusion

**Research Candidate**:
A Market Security surfaced by a Market Insight for explicit follow-up under a stated style or selection method, with an evidence-linked rationale and risks. It is not a Security Team Conclusion and never starts Security research automatically.
_Avoid_: buy recommendation, automatic child Run, portfolio selection

**Market Team Conclusion**:
A Team Conclusion for the whole Shanghai-Shenzhen A-share market, using a common quality, risk, invalidation, and time-horizon envelope around one or more Market Insights without requiring one universal primary verdict.
_Avoid_: single Market Regime, arbitrary report, Security Team Conclusion

**Partial Market Conclusion**:
A published Market Team Conclusion containing at least one passed and at least one blocked Market Insight, including an Insight blocked by an isolated Agent timeout or schema-invalid result. Its Research Record has terminal status `partial`; a conclusion with all contracted Insights passed is `passed`, while no publishable Insight or failure of the overall quality gate is `blocked` and produces no report. The `failed` status is reserved for orchestration, persistence, or publication failure that prevents a coherent result from being safely completed.
_Avoid_: silently omitted Insight, passed-with-hidden-gaps, blocked whole report with valid Insight

**Team Report**:
The independently published JSON and Markdown representation of one passed or partial Team Conclusion within a Research Cycle. A blocked, failed, or cancelled Research Record has no Team Report.
_Avoid_: blocked-status report, combined Team report, comparison section

**Cycle Index**:
The navigation-only artifact that lists Research Cycle provenance, Team run status, and Team Report locations without interpreting conclusions.
_Avoid_: daily synthesis, Team comparison, recommendation summary

**Daily Team Brief**:
The deterministic, Team-specific rendering of that Team's reports and blocked statuses across the subjects in one Daily Research Batch, without a new model conclusion.
_Avoid_: cross-Team daily report, portfolio ranking, generated daily verdict

**Decision Stance**:
The bounded advisory posture `watch_buy`, `watch_add`, `hold`, `watch_reduce`, or `watch_exit` expressed by a Team Conclusion without placing an order.
_Avoid_: trade instruction, portfolio mutation

**Decision Conviction**:
The explicitly justified `low`, `medium`, or `high` strength of a Portfolio Manager's judgment, kept separate from deterministic evidence quality.
_Avoid_: probability, numeric confidence, evidence grade
