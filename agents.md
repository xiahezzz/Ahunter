# Event foundation operating rules

Phase 1 is a passive, read-only MX event collector. It records only user-authorized RIDs for later evidence work. The collector itself does not place or simulate trades and does not implement Tushare backfill, evidence packs, analysis, reports, evaluation, or Champion/Challenger promotion. These collector implementation limits do not prohibit the primary agent from separately analyzing successfully extracted data.

## Personal project scope

A Hunter is a single-owner personal project. Optimize its features and interfaces for the owner's local workflow; do not add multi-user, tenancy, collaboration, role/permission, or general-purpose configuration complexity unless the user explicitly requests it.

Research Team configuration accepts any non-empty selection of locally published Research Agents. Do not require a universal Agent or warn that a Team's evidence coverage is narrow. Every selected Agent remains required, and an Agent failure blocks only the dependent Team.

## Mandatory safety rules

- Never use any Superpowers skill or workflow in this repository. This includes skills named `superpowers:*`, unprefixed aliases distributed by Superpowers, and instructions that delegate work to a Superpowers skill. The `writing-plans`, `using-git-worktrees`, and `test-driven-development` skills are explicitly prohibited as well. Use only this repository's native development workflow.
- Run every collector and test command with the Node 24 absolute path shown below.
- Run every test command in a newly spawned clean non-login shell; never reuse the current shell for testing. Explicitly remove all HTTP/HTTPS/ALL proxy variables and set both `NO_PROXY=*` and `no_proxy=*` so tests, especially live network, market-source, and preflight checks, cannot inherit the current shell's proxy route.
- Treat an empty `config/allowed-rids.yaml` as intentionally inactive and fail closed: collect no content and download no media.
- Only the user may supply or authorize a RID. Never infer, discover, or add a RID from observed traffic.
- Run `scripts/quarantine-legacy-output.mjs` once before the first collector start. It preserves prior decoded output outside the new event ledger; never run it against real user data from a development worktree.
- Run the offline self-test before every collector start and after every code change. Do not start when it fails.
- Run the live smoke test only when the user has already enabled Chrome debugging and opened the authorized, logged-in MX page.
- Only the explicit same-origin WebUI action labeled “启动专用 Chrome” or the user-requested `ahunter mx chrome start` command may start the fixed local Chrome executable with the fixed loopback debugging port and isolated MX profile. Both use the same local API, accept no URL or launch parameters, and must never navigate, reload, log in, click, type, inject, or inspect page content. Listener start/stop, CLI recovery, LaunchAgents, and automatic retries must never invoke either action.
- Use Chrome DevTools only. Never use Computer Use for this workflow.
- If authorization or login expires, stop collection and ask the user to log in. Never enter, request, record, or expose credentials, cookies, tokens, Socket.IO session IDs, or Chrome debugging identifiers.
- Do not generate downstream analytical conclusions, reports, or stock recommendations after any data-quality failure until the failure is resolved.
- Routine Phase 1 collection, smoke testing, reconnect, and recovery must never navigate, reload, click, type into, inject into, or otherwise operate the MX page. The collector passively connects through Chrome DevTools `Network` events. The disabled legacy page-injection diagnostic may be used only when the user explicitly requests that specific diagnostic action; never use it for routine recovery or as part of collector/smoke operation.
- Never perform real trading or submit orders.

## Agent analysis boundary

The `analyze-a-hunter-data` skill is extraction-only. While that skill is active, follow its boundary and return stored data without analysis or investment judgment.

After extraction is complete, the primary agent may independently analyze successfully extracted data, supplement it with lawful external research, generate analytical reports and simulated scenarios, and provide stock research opinions or recommendations. Do not attribute that downstream work to the extraction skill. Research opinions never authorize real trading or order submission.

If a required data-quality check fails, do not produce downstream analytical conclusions or stock recommendations until the failure is resolved.

## RID configuration

The user explicitly supplies or authorizes RID values. Apply those values through the WebUI, `ahunter mx rids replace`, or an explicit edit of `config/allowed-rids.yaml`. CLI/API replacement uses the current configuration version and replaces the complete set. The safe default is:

```yaml
allowed_rids: []
```

Authorized values must be positive integer RIDs, for example `allowed_rids: [123]`. Stop rather than adding any value inferred from traffic.

## Exact operating commands

Use this clean-shell wrapper for every test command, replacing the final command as needed:

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c 'COMMAND'
```

From the repository root, quarantine legacy output once before the first real Listener service start. It is a preservation operation, not a development-worktree command:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/quarantine-legacy-output.mjs
```

Run the required offline test before every service start and after every code change:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
```

When the user has already enabled Chrome debugging and opened the authorized page, the optional live check is:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/smoke-test.mjs
```

For daily operation, use the independent Listener service through the local WebUI or the service CLI. Its status read is non-mutating:

```bash
./.venv-runtime/bin/advisor-services status
```

After the user has explicitly approved installation/loading of the MX LaunchAgent, start or stop it with:

```bash
./.venv-runtime/bin/advisor-services start mx-listener
./.venv-runtime/bin/advisor-services stop mx-listener
```

These commands must never open, navigate, reload, click, type into, inject into, or otherwise control Chrome or the MX page. The separate explicit WebUI launcher or user-requested `ahunter mx chrome start` may open only the fixed isolated Chrome; the user alone restores login state and opens the authorized MX page. `waiting_for_chrome` and `waiting_for_authorization` are expected passive states. On login expiry, connection failure, failed self-test, or failed data-quality checks, keep the Listener stopped or waiting as appropriate, correct the configuration or ask the user to restore authorization, then rerun the offline self-test before recovery. Run the live smoke only when user-enabled debug Chrome remains available. `scripts/run-collector.mjs` is not the daily service entry point.

## Data handling

Store no credentials or debugging identifiers. Rejected, undecodable, and non-allowlisted content must not enter SQLite, JSON output, logs, or media paths. Raw accepted payloads expire after 30 days; hashes remain for deduplication and audit. Do not use event data for downstream analytical conclusions or stock recommendations after a quality check fails until the failure is resolved.

## Advisor operations

### Agent CLI and mandatory skill synchronization

Use `ahunter` for agent operations covered by the Web API. It is an HTTP client for the existing local API, defaults to `http://127.0.0.1:8000`, and outputs JSON. Run `rtk ahunter commands` for the machine-readable command catalog or `rtk ahunter <command> --help`. The repository runtime entry is `.venv-runtime/bin/ahunter`; `python -m advisor.cli` is the equivalent module entry. Existing `advisor-*` maintenance/service CLIs remain supported for operations outside the Web API. The CLI never implicitly starts the API, services, or Chrome, and never retries a write automatically.

The authoritative skill source is `skills/ahunter/SKILL.md` and its `references/` directory. The personal installation at `~/.codex/skills/ahunter` links to this directory so repository updates apply immediately. Preserve the separate extraction-only `analyze-a-hunter-data` skill.

**Every change to this project, including documentation and configuration changes, must include a CLI/skill impact check before completion.** Review whether it changes capabilities, API routes or fields, command names/options, outputs/errors, asynchronous task behavior, installation, authorization boundaries, or operating/recovery instructions. Update the CLI and skill in the same change whenever affected. When no update is needed, explicitly state why in the completion report or PR description. A passing automated check does not replace this semantic review.

- Maintain every `/api/` operation in `advisor/cli/catalog.py`; add/change its named command and HTTP client behavior as needed. Preserve backend validation and quality gates instead of duplicating business logic in the CLI.
- Update `skills/ahunter/SKILL.md` and `references/workflows.md` for behavior or workflow changes. Regenerate `references/commands.md` with `rtk .venv311/bin/python -m advisor.cli.maintenance --write` when the command catalog changes.
- Run `python -m advisor.cli.maintenance --check` using the clean-shell wrapper above. The required offline self-test includes API route/query coverage and generated skill-reference checks through `tests/advisor/test_agent_cli.py`; missing commands, removed routes, changed query parameters, or stale command documentation fail verification.
- Run relevant CLI tests for changed request bodies, serialization, responses, and downloads. Verify installed entry points and the personal skill link when packaging or installation changes.
- JSON stdout represents HTTP success, not business completion. Preserve request IDs, submission identities, configuration version tokens, pagination, and failed/blocked quality states in both implementation and skill instructions. Use explicit user-authorized RID sets and recorded ledger data; never infer authorization or create real orders.

The advisor is separate from the passive MX collector. Its self-contained Research Engine reads accepted MX events and public A-share Provider Adapters. Team mode runs the repository-owned Agent catalog and fixed Decision Pipeline through the local Codex CLI; Agents receive only sealed Snapshot products declared by their manifests. LAgent mode uses one logical main investigator with arbitrary task-specific subagents and host-executed Data Product/query actions under pinned, versioned configuration. LAgent uses the same durable queue, evidence boundary and product quality gates, with mandatory task/action/attempt/artifact observations. It must not connect to a broker, submit orders, or create order-like objects or instructions. Reviews never become implicit Agent memory.

### Restart the local API after changes

The Advisor API is a long-lived LaunchAgent and does not automatically reload Python modules or repository configuration. After every change that can affect API behavior—including changes under `advisor/`, `config/`, Research manifests, API routes, schemas, or runtime dependencies—run the required offline self-test and then restart the API. Do not assume that a successful frontend refresh means the current source is loaded; an old API process can continue serving stale behavior indefinitely.

From the repository root, restart the already-loaded local API LaunchAgent with:

```bash
rtk launchctl kickstart -k "gui/$(id -u)/com.ahunter.advisor-api"
```

Before restarting, the latest code change must have passed the required clean-shell self-test described above. After restarting, verify that launchd owns a new healthy process and exercise the API path affected by the change. At minimum, check the service and one relevant endpoint:

```bash
rtk launchctl print "gui/$(id -u)/com.ahunter.advisor-api"
rtk curl --silent --show-error --fail-with-body --max-time 10 http://127.0.0.1:8000/api/services
```

For Research catalog or manifest changes, also verify all catalog surfaces because they share one fail-closed loader:

```bash
rtk curl --silent --show-error --fail-with-body --max-time 10 http://127.0.0.1:8000/api/research/data-catalog
rtk curl --silent --show-error --fail-with-body --max-time 10 http://127.0.0.1:8000/api/research/agent-access
rtk curl --silent --show-error --fail-with-body --max-time 10 http://127.0.0.1:8000/api/research/agents
rtk curl --silent --show-error --fail-with-body --max-time 10 http://127.0.0.1:8000/api/research/teams
```

If `kickstart` reports that the service is not loaded, do not start an ad-hoc Uvicorn process. Inspect the installed LaunchAgent and use the repository's launchd installation/loading workflow so there is still exactly one API owner.

Keep development and live advisor dependencies in separate virtual environments. `.venv311` is the test environment and may install `.[dev]`; `.venv-runtime` is the live advisor runtime and installs the base A Hunter project. Create or refresh the runtime with:

```bash
/opt/local/bin/python3.11 -m advisor.runtime_env
```

Advisor launchd rendering defaults to `.venv-runtime/bin/python`. Do not point live advisor launch agents at `.venv311`.

Use the repository-owned public Provider catalog and the local MX evidence adapter only. Tushare and paid feeds are not part of required advisor operation. The Research Engine does not use application credential configuration.

Market Daily 的唯一实时行情来源是新浪公开接口。同一个新浪适配器负责沪深 A 股清单、原始日 K、前复权因子、沪深交易日观察和缺失 K 线证据；运行路径不得调用 Eastmoney、TDX、交易所行情接口或任何备源，也不得要求数据源密钥。所有新浪请求共享进程级限流器，默认上限为每秒 2 次；连接错误、HTTP 429 和 5xx 只允许有界重试，耗尽后保留 `source_missing`，不得切换来源或伪造数据。

Before a live run, validate the catalog, storage, execution policy, and local Codex session:

```bash
.venv-runtime/bin/advisor-research preflight
```

The first single-Subject run is explicit and reproducible; the code is never selected by a model:

```bash
.venv-runtime/bin/advisor-research run \
  --codes 600519 \
  --as-of 2026-08-06T08:30:00+08:00 \
  --events-db data/state/events.sqlite \
  --allowed-rids config/allowed-rids.yaml \
  --output-dir reports
```

It writes `reports/YYYY-MM-DD/<cycle_id>/cycle.json`, `index.md`, and an independent `teams/<team>@<version>/` directory. A successful Team directory contains `conclusion.json` and `report.md`; a blocked Team contains only `status.json`. The cycle completion marker is `complete.json`. The 22:30 review reads only published Team Conclusions and writes Team-scoped review files under `reports/YYYY-MM-DD/reviews/teams/`; it never calls Codex or compares Teams.

The scheduled 08:30 entry runs `advisor.scheduler.premarket` and the 22:30 entry runs `advisor.scheduler.review`. A scheduled batch may use explicit `--codes`, qualified local MX event codes, and current ledger positions, subject to the configured maximum; no model chooses candidates.

If preflight or any Data Product, Agent, Decision Stage, or publication quality gate fails, mark the affected cycle or Team `blocked` and do not generate a replacement conclusion. Diagnose the status record, correct the source or configuration, rerun with a new cycle ID, and preserve the prior immutable artifacts.
