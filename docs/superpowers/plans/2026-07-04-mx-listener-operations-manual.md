# MX Listener Operations Manual Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a copy-and-paste Markdown operations manual for routine MX collector startup, monitoring, data inspection, maintenance, and troubleshooting.

**Architecture:** Add one lifecycle-organized document under `docs/`. It will reference the existing collector, self-test, Chrome CDP endpoint, RID configuration, and SQLite ledger without changing runtime behavior.

**Tech Stack:** Markdown, zsh commands, Chrome DevTools Protocol HTTP endpoints, Node.js 24.18.0, SQLite 3.

## Global Constraints

- Normal commands run from `/Users/mac/Documents/Ahunter/a_hunter`.
- Node commands use `/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node`.
- Dedicated Chrome uses `$HOME/.chrome-mx-debug-profile` and CDP port 9333.
- Never run the collector with `sudo`.
- Only explicitly user-authorized positive integer RIDs belong in `config/allowed-rids.yaml`.
- Use `curl --noproxy '*'` for loopback CDP checks.
- Exclude development commands, real trading, legacy page injection, and legacy frame decoding.
- Do not expose credentials, cookies, tokens, Socket.IO session IDs, or Chrome debugger identifiers.

## File structure

- Create `docs/mx-listener-operations-manual.md`: the complete operator-facing command manual.
- Read but do not modify `agents.md`, `scripts/mx-websocket/README.md`, `scripts/run-collector.mjs`, `scripts/self-test.mjs`, `src/events/schema.sql`, and `config/allowed-rids.yaml` while verifying command accuracy.

---

### Task 1: Write and validate the operations manual

**Files:**
- Create: `docs/mx-listener-operations-manual.md`
- Reference: `docs/superpowers/specs/2026-07-04-mx-listener-operations-manual-design.md`

**Interfaces:**
- Consumes: `scripts/run-collector.mjs --cdp http://127.0.0.1:9333`, `scripts/self-test.mjs`, Chrome `/json/version` and `/json/list`, and the existing SQLite schema.
- Produces: a standalone Markdown manual requiring no source-code knowledge.

- [ ] **Step 1: Write the fixed-path and safety preface**

State the project root, Node binary, Chrome profile, CDP port, database path, media path, and RID configuration path. State explicitly that the operator must not use `sudo`, must keep the dedicated Chrome and MX page open, and may stop long-running commands with `Ctrl-C`.

- [ ] **Step 2: Write first-time setup and daily startup commands**

Include these exact operations in lifecycle order:

```bash
cd /Users/mac/Documents/Ahunter/a_hunter

open -na "Google Chrome" --args \
  --remote-debugging-port=9333 \
  --user-data-dir="$HOME/.chrome-mx-debug-profile"

curl --noproxy '*' -sS http://127.0.0.1:9333/json/version
curl --noproxy '*' -sS http://127.0.0.1:9333/json/list

/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs

caffeinate -i \
  /Users/mac/.local/share/chrome-devtools-mcp/node/bin/node \
  scripts/run-collector.mjs \
  --cdp http://127.0.0.1:9333
```

Explain the required success evidence: `webSocketDebuggerUrl` in `/json/version`, the exact MX URL in `/json/list`, `114` passing tests, and a collector process that remains in the foreground without an error loop. Explain that a quiet collector terminal is normal.

- [ ] **Step 3: Write RID configuration and live-reload guidance**

Document the valid YAML form without inventing a RID:

```yaml
allowed_rids: [123]
```

Explain that only the user supplies the positive integer value, an empty list records nothing, and the running collector reloads valid atomic replacements while invalid configuration fails closed.

- [ ] **Step 4: Write read-only data inspection commands**

Include commands for total events, recent decoded text, counters, downloaded media, media-job status, and database file size:

```bash
sqlite3 -header -column data/state/events.sqlite \
"SELECT count(*) AS event_count FROM events;"

sqlite3 -header -column data/state/events.sqlite \
"SELECT rid,
        datetime(received_at / 1000, 'unixepoch', 'localtime') AS received_time,
        decoded_text
 FROM events
 ORDER BY received_at DESC
 LIMIT 20;"

sqlite3 -header -column data/state/events.sqlite \
"SELECT kind, sum(count) AS total
 FROM ingest_counters
 GROUP BY kind
 ORDER BY kind;"

sqlite3 -header -column data/state/events.sqlite \
"SELECT rid,
        datetime(downloaded_at / 1000, 'unixepoch', 'localtime') AS downloaded_time,
        content_type,
        local_path
 FROM media
 ORDER BY downloaded_at DESC
 LIMIT 20;"

sqlite3 -header -column data/state/events.sqlite \
"SELECT status, count(*) AS total
 FROM media_jobs
 GROUP BY status
 ORDER BY status;"

du -sh data/state/events.sqlite data/media 2>/dev/null
```

State that SQLite WAL mode allows these read-only queries while the collector runs.

- [ ] **Step 5: Write stop, restart, backup, and process checks**

Use `Ctrl-C` as the normal shutdown path and document the 30-second drain window. Include non-destructive checks and a stopped-collector backup:

```bash
lsof -nP -iTCP:9333 -sTCP:LISTEN

pgrep -fl 'scripts/run-collector.mjs'

mkdir -p "$HOME/Documents/Ahunter-backups"
cp -p data/state/events.sqlite \
  "$HOME/Documents/Ahunter-backups/events-$(date +%Y%m%d-%H%M%S).sqlite"
```

Require stopping the collector before the simple file-copy backup. Do not include `kill -9`, database deletion, profile deletion, or any other destructive recovery shortcut.

- [ ] **Step 6: Write symptom-based troubleshooting**

Provide direct checks and resolutions for:

- `Collector connection failed (Error)`: verify listener, `/json/version`, correct `--cdp`, and dead proxy bypass;
- `authorization_required`: verify `/json/list`, open and log in to the MX URL in the dedicated profile, then restart the collector;
- collector stays quiet: explain normal quiet behavior, query counters and event count, verify configured RID and page login;
- no data with `allowed_rids: []`: add only a user-authorized positive integer RID;
- permission denied or root-owned files after `sudo`: stop and inspect with `ls -l data/state`, then correct ownership explicitly as an exceptional administrative repair;
- display off disconnects collection: keep the lid open, connect power, and use `caffeinate -i`;
- Chrome port conflict: select one unused port and use that same value in Chrome launch, curl checks, and collector `--cdp`.

- [ ] **Step 7: Add a compact command index**

End with a table mapping “start Chrome,” “check CDP,” “self-test,” “start collector,” “view events,” “view counters,” “view media,” and “stop” to their command or section. Keep lifecycle sections authoritative so the index does not introduce alternate commands.

- [ ] **Step 8: Validate content and repository state**

Run:

```bash
rg -n 'sudo .*run-collector|--cdp http://127\.0\.0\.1:9222' \
  docs/mx-listener-operations-manual.md
```

Expected: no output.

Run:

```bash
rg -n '9333|caffeinate -i|authorization_required|events\.sqlite|allowed_rids|Ctrl-C' \
  docs/mx-listener-operations-manual.md
```

Expected: every required operating concept appears.

Run:

```bash
git diff --check
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
```

Expected: no whitespace errors and all 114 tests pass. Do not modify or commit the user's current `config/allowed-rids.yaml` value.

- [ ] **Step 9: Commit only the manual and plan**

```bash
git add docs/mx-listener-operations-manual.md \
  docs/superpowers/plans/2026-07-04-mx-listener-operations-manual.md
git commit -m "docs: add MX listener operations manual"
```
