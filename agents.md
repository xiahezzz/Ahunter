# Event foundation operating rules

Phase 1 is a passive, read-only MX event collector. It records only user-authorized RIDs for later evidence work. The collector itself does not place or simulate trades and does not implement Tushare backfill, evidence packs, analysis, reports, evaluation, or Champion/Challenger promotion. These collector implementation limits do not prohibit the primary agent from separately analyzing successfully extracted data.

## Mandatory safety rules

- Run every collector and test command with the Node 24 absolute path shown below.
- Treat an empty `config/allowed-rids.yaml` as intentionally inactive and fail closed: collect no content and download no media.
- Only the user may supply or authorize a RID. Never infer, discover, or add a RID from observed traffic.
- Run `scripts/quarantine-legacy-output.mjs` once before the first collector start. It preserves prior decoded output outside the new event ledger; never run it against real user data from a development worktree.
- Run the offline self-test before every collector start and after every code change. Do not start when it fails.
- Run the live smoke test only when the user has already enabled Chrome debugging and opened the authorized, logged-in MX page.
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

The user edits `config/allowed-rids.yaml` explicitly. The safe default is:

```yaml
allowed_rids: []
```

Authorized values must be positive integer RIDs, for example `allowed_rids: [123]`. Stop rather than adding any value inferred from traffic.

## Exact operating commands

From the repository root, quarantine legacy output once before the first start:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/quarantine-legacy-output.mjs
```

Run the required offline test before every start and after every change:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
```

When the user has already enabled Chrome debugging, the optional live check is:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/smoke-test.mjs
```

Start the collector without navigating or reloading the page:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/run-collector.mjs --cdp http://127.0.0.1:9222
```

Stop with `Ctrl-C` in the collector terminal. On login expiry, connection failure, failed self-test, or failed data-quality checks, keep it stopped; correct the configuration or ask the user to restore the authorized login, then rerun the offline self-test before recovery. Run the live smoke again only if user-enabled debug Chrome remains available.

## Data handling

Store no credentials or debugging identifiers. Rejected, undecodable, and non-allowlisted content must not enter SQLite, JSON output, logs, or media paths. Raw accepted payloads expire after 30 days; hashes remain for deduplication and audit. Do not use event data for downstream analytical conclusions or stock recommendations after a quality check fails until the failure is resolved.
