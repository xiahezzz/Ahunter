# MX Listener Operations Manual

This manual is the routine operating procedure for the authorized, passive MX listener on this Mac. Run commands from Terminal in the order shown. No source-code knowledge is required.

## 1. Safety rules and fixed paths

| Item | Fixed value |
| --- | --- |
| Project root | `/Users/mac/Documents/Ahunter/a_hunter` |
| Node binary | `/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node` |
| Dedicated Chrome profile | `$HOME/.chrome-mx-debug-profile` |
| Chrome DevTools (CDP) port | `9333` |
| Event database | `data/state/events.sqlite` |
| Downloaded media | `data/media/` |
| RID configuration | `config/allowed-rids.yaml` |
| MX page | `https://mx.2026.naaifu.cn/` |

- Do not use `sudo` to start Chrome, run tests, start the collector, inspect data, or make backups. It can create root-owned files that the normal user cannot update.
- Keep the dedicated Chrome application and the logged-in MX page open for the entire collection session. Do not use that dedicated window for unrelated browsing.
- A command that stays in the foreground can be stopped with `Ctrl-C`. Use `Ctrl-C` for the collector's normal shutdown; do not force-stop it.
- The listener only records authorized inbound data. Only put a RID in the configuration when the user has explicitly authorized it.

## 2. First-time setup

### 2.1 Open the project

All later commands assume this working directory:

```bash
cd /Users/mac/Documents/Ahunter/a_hunter
```

### 2.2 Configure the authorized RID

`config/allowed-rids.yaml` must contain an array of positive integers. This is the valid one-line form:

```yaml
allowed_rids: [123]
```

Here, `123` only demonstrates the required YAML form. The user must supply the actual positive integer RID; do not infer one from traffic or copy this example unless that value is explicitly authorized. `allowed_rids: []` is valid and deliberately records nothing.

The running collector watches this file. It reloads a valid configuration when an editor performs an atomic replacement (write a new file and rename it over the old one). An invalid or unreadable replacement fails closed to an empty allowlist, so it records nothing until a valid configuration is installed.

### 2.3 Start dedicated Chrome and log in

Start a separate Chrome instance with the fixed profile and CDP port:

```bash
open -na "Google Chrome" --args \
  --remote-debugging-port=9333 \
  --user-data-dir="$HOME/.chrome-mx-debug-profile"
```

In that dedicated Chrome window, open exactly `https://mx.2026.naaifu.cn/` and log in. Leave both Chrome and the MX tab open.

### 2.4 Check Chrome DevTools

These commands bypass shell proxy settings for the local endpoint:

```bash
curl --noproxy '*' -sS http://127.0.0.1:9333/json/version
curl --noproxy '*' -sS http://127.0.0.1:9333/json/list
```

Success evidence:

- `/json/version` returns JSON containing `webSocketDebuggerUrl`.
- `/json/list` returns a page target whose `url` is the exact MX URL `https://mx.2026.naaifu.cn/` and which has a `webSocketDebuggerUrl`.

Do not copy or share debugger URLs; they are only success evidence in the local terminal.

### 2.5 Run the offline self-test

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
```

Proceed only when the summary reports exactly `114` passing tests and no failures.

### 2.6 Quarantine legacy output once

Before the first-ever collector start, run this one-time preservation step:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/quarantine-legacy-output.mjs
```

A safe result reports either `No legacy output to quarantine` or the quarantine destination. This command preserves any legacy output by moving it aside; it does not decode or import that output. Do not repeat it as part of daily startup.

## 3. Daily startup checklist

Repeat these steps after a restart or whenever beginning a collection session:

1. Open Terminal and enter the project root:

   ```bash
   cd /Users/mac/Documents/Ahunter/a_hunter
   ```

2. Start dedicated Chrome if it is not already open:

   ```bash
   open -na "Google Chrome" --args \
     --remote-debugging-port=9333 \
     --user-data-dir="$HOME/.chrome-mx-debug-profile"
   ```

3. Open `https://mx.2026.naaifu.cn/` in that dedicated profile, confirm that it is logged in, and leave the page open.

4. Verify CDP and the MX page:

   ```bash
   curl --noproxy '*' -sS http://127.0.0.1:9333/json/version
   curl --noproxy '*' -sS http://127.0.0.1:9333/json/list
   ```

   Confirm `webSocketDebuggerUrl` in `/json/version` and the exact MX URL in `/json/list`.

5. Run the self-test:

   ```bash
   /Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
   ```

   Confirm exactly `114` passing tests.

6. In a terminal that can remain open, start the collector:

   ```bash
   caffeinate -i \
     /Users/mac/.local/share/chrome-devtools-mcp/node/bin/node \
     scripts/run-collector.mjs \
     --cdp http://127.0.0.1:9333
   ```

Success means the process remains in the foreground without repeatedly printing errors. A quiet collector terminal is normal: accepted messages are stored rather than printed. Leave this terminal, dedicated Chrome, and the MX page open. `caffeinate -i` prevents idle system sleep while the command runs, but keep the laptop lid open and connect power for long sessions.

## 4. View recorded information

Open a second Terminal window, enter the project root, and use the following read-only commands. The database uses SQLite WAL mode, so these queries are safe while the collector is running.

### Total accepted events

```bash
sqlite3 -header -column data/state/events.sqlite \
"SELECT count(*) AS event_count FROM events;"
```

### Most recent decoded text

```bash
sqlite3 -header -column data/state/events.sqlite \
"SELECT rid,
        datetime(received_at / 1000, 'unixepoch', 'localtime') AS received_time,
        decoded_text
 FROM events
 ORDER BY received_at DESC
 LIMIT 20;"
```

### Ingestion counters

```bash
sqlite3 -header -column data/state/events.sqlite \
"SELECT kind, sum(count) AS total
 FROM ingest_counters
 GROUP BY kind
 ORDER BY kind;"
```

### Recently downloaded media

```bash
sqlite3 -header -column data/state/events.sqlite \
"SELECT rid,
        datetime(downloaded_at / 1000, 'unixepoch', 'localtime') AS downloaded_time,
        content_type,
        local_path
 FROM media
 ORDER BY downloaded_at DESC
 LIMIT 20;"
```

### Media-job status

```bash
sqlite3 -header -column data/state/events.sqlite \
"SELECT status, count(*) AS total
 FROM media_jobs
 GROUP BY status
 ORDER BY status;"
```

### Database and media disk usage

```bash
du -sh data/state/events.sqlite data/media 2>/dev/null
```

## 5. Stop, restart, process checks, and backup

### Normal shutdown

In the collector terminal, press `Ctrl-C` once. The collector stops accepting new frames, gives queued event and media work up to 30 seconds to drain, and then closes the database. Wait for the command to return to the shell prompt before closing Terminal, closing Chrome, restarting, or backing up.

### Non-destructive process checks

Check whether something is listening on the fixed Chrome port:

```bash
lsof -nP -iTCP:9333 -sTCP:LISTEN
```

Check whether the collector process is running:

```bash
pgrep -fl 'scripts/run-collector.mjs'
```

No output means that the corresponding listener or collector process was not found.

### Restart

Stop the collector with `Ctrl-C`, wait for the shell prompt, resolve the reason for restarting, rerun the self-test, verify `/json/version` and `/json/list`, and then use the start-collector command in the daily checklist. Keep using the same dedicated Chrome profile and port.

### Back up the database

First stop the collector with `Ctrl-C` and wait for it to exit. A simple file-copy backup must not be made while the collector is running. Then run:

```bash
mkdir -p "$HOME/Documents/Ahunter-backups"
cp -p data/state/events.sqlite \
  "$HOME/Documents/Ahunter-backups/events-$(date +%Y%m%d-%H%M%S).sqlite"
```

This creates a timestamped database copy without changing the live database. Downloaded files are stored separately under `data/media/`; include that directory in the machine's normal backup if those files must also be retained.

## 6. One-time disclaimer-content migration

Run this procedure only after the updated collector and migration script have been installed. The migration rewrites normalized event text and JSON while preserving event, media, and media-job counts and associations.

### Stop the collector and confirm that it is stopped

In the collector terminal, press `Ctrl-C` once. Wait until the command has exited and the shell prompt has returned. Do not run the migration while the collector is running.

From the project root, verify that no collector process remains:

```bash
pgrep -fl 'scripts/run-collector.mjs'
```

Proceed only if this command produces no output. If it prints a process, return to that collector terminal, stop it with `Ctrl-C`, wait for the shell prompt, and repeat the check. Do not force-stop the collector.

### Record the pre-migration aggregate baseline

With the collector stopped, run this read-only aggregate query and save its four numeric results with the migration record:

```bash
sqlite3 -header -column data/state/events.sqlite \
"SELECT
   (SELECT count(*) FROM events) AS events,
   (SELECT count(*) FROM media) AS media,
   (SELECT count(*) FROM media_jobs) AS media_jobs,
   (SELECT count(*)
      FROM media AS m
      LEFT JOIN events AS e ON e.event_id = m.event_id
     WHERE e.event_id IS NULL)
   +
   (SELECT count(*)
      FROM media_jobs AS j
      LEFT JOIN events AS e ON e.event_id = j.event_id
     WHERE e.event_id IS NULL) AS orphans;"
```

`orphans` must be `0`. Do not run the migration if it is nonzero. The `events`, `media`, and `media_jobs` counts are the exact pre-migration baseline to compare with the migration report.

### Run the migration

With the collector stopped, run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node \
  scripts/migrate-disclaimer-content.mjs
```

The migration report contains aggregate counts only. Success requires `eventsBefore` to equal `eventsAfter`, `mediaBefore` to equal `mediaAfter`, `mediaJobsBefore` to equal `mediaJobsAfter`, and `orphansAfter: 0`. Do not restart the collector if any of these checks fails.

Run the same migration command a second time. A successful repeated run reports `updated: 0`, showing that there was nothing left to change.

### Verify the exact disclaimer is absent

Run this read-only aggregate query:

```bash
sqlite3 -header -column data/state/events.sqlite \
"SELECT
  coalesce((
    SELECT count(*)
    FROM events AS e
    JOIN json_tree(e.parsed_content_json, '$.parsed') AS node
    WHERE node.type = 'text'
      AND trim(node.atom, char(9) || char(10) || char(11) || char(12) || char(13) || char(32)) = '免责声明：信息来源于官方媒体/网络新闻等，仅信息分享，不作为投资建议！'
      AND (node.parent IS NULL OR typeof(node.key) = 'integer' OR node.key = 'msg')
  ), 0) AS parsed_removable_matches,
  coalesce((
    SELECT count(*)
    FROM events AS e
    JOIN json_each(e.parsed_content_json, '$.texts') AS text
    WHERE text.type = 'text'
      AND trim(text.atom, char(9) || char(10) || char(11) || char(12) || char(13) || char(32)) = '免责声明：信息来源于官方媒体/网络新闻等，仅信息分享，不作为投资建议！'
  ), 0) AS extracted_text_matches;"
```

Both `parsed_removable_matches` and `extracted_text_matches` must be numeric zero. This query counts only exact trimmed values in removable locations: the `parsed` root, `parsed` array elements, `msg` properties, and entries in `texts`. It intentionally preserves longer strings that quote the disclaimer and exact values in non-message properties such as `attribution`.

### Verify event-image associations

Run this read-only local query:

```bash
sqlite3 -header -column data/state/events.sqlite \
"SELECT e.event_id, e.rid,
        datetime(e.received_at / 1000, 'unixepoch', 'localtime') AS received_time,
        e.decoded_text, m.content_type, m.local_path
 FROM events AS e
 LEFT JOIN media AS m ON m.event_id = e.event_id AND m.rid = e.rid
 ORDER BY e.received_at DESC, m.local_path;"
```

Review the results locally to confirm that downloaded media remains associated with its event and RID and has a nonempty `local_path`. An empty `decoded_text` together with a nonempty `local_path` is valid for an image-only event; it is not evidence of a failed migration.

Restart the collector only after the migration report, repeated-run check, exact-disclaimer query, and event-image association review all succeed.

## 7. Troubleshooting by symptom

### `Collector connection failed (Error)` repeats

1. Run `lsof -nP -iTCP:9333 -sTCP:LISTEN` and confirm Chrome is listening.
2. Run `curl --noproxy '*' -sS http://127.0.0.1:9333/json/version` and confirm `webSocketDebuggerUrl` appears. `--noproxy '*'` prevents a dead proxy from intercepting loopback traffic.
3. Confirm the collector command uses exactly `--cdp http://127.0.0.1:9333`.
4. If there is no listener, start dedicated Chrome using the daily checklist. When the checks succeed, stop the error loop with `Ctrl-C` and restart the collector.

### `authorization_required`

CDP is reachable, but it exposes no inspectable page target on the MX origin `https://mx.2026.naaifu.cn`. Run:

```bash
curl --noproxy '*' -sS http://127.0.0.1:9333/json/list
```

Confirm that the list contains the exact root URL `https://mx.2026.naaifu.cn/`. In the dedicated Chrome profile, open that recommended page and log in, then leave the page open. Restart the collector after the target appears in `/json/list`.

### The collector stays quiet

Quiet operation is normal because accepted events are written to SQLite rather than printed. Run the total-event and ingestion-counter queries in section 4. If the values do not change when authorized inbound activity is expected, verify that `config/allowed-rids.yaml` contains the user-authorized RID and that the MX page remains open and logged in in the dedicated profile.

### No data with `allowed_rids: []`

An empty allowlist intentionally records nothing. Add only a positive integer RID explicitly supplied and authorized by the user, using the valid YAML form in section 2. Never infer a RID from observed traffic.

### Permission denied or root-owned files after `sudo`

This is exceptional recovery for files created by prior `sudo` use, not a routine startup step. First stop the collector with `Ctrl-C`, wait for it to exit, and recursively list root-owned entries under both storage paths when present:

```bash
find data/state data/media -user root -ls 2>/dev/null
```

No output means no root-owned entry was found. The error redirect keeps an optional, not-yet-created `data/media` directory from producing an error message.

Do not restart until the database files under `data/state` and any downloaded files under `data/media` are writable by the normal account. After confirming that the affected files should belong to the current user and the Mac's normal `staff` group, run:

```bash
sudo chown -R "$USER":staff data/state
if [ -e data/media ]; then sudo chown -R "$USER":staff data/media; fi
```

Then repeat the recursive `find` command. Resume all routine operation as the normal user without `sudo` only when it reports no root-owned entries. If the expected owner or group is uncertain, stop and ask the Mac administrator instead of guessing.

### Collection disconnects when the display turns off

Keep the laptop lid open, connect power, and start the collector with the documented `caffeinate -i` command. Turning the display off is acceptable; closing the lid can suspend the Mac and disconnect collection.

### Chrome port conflict

If port 9333 is already used by an unrelated process, inspect it with `lsof -nP -iTCP:9333 -sTCP:LISTEN`. Select one unused port. Use that same value in `--remote-debugging-port`, both `/json/version` and `/json/list` curl URLs, and the collector's `--cdp` URL for the entire session. Do not mix port values. Return to 9333 when the conflict is resolved so the fixed daily commands apply again.

## 8. Compact command index

The lifecycle sections above are authoritative; this index only points back to those same commands.

| Operation | Command or section |
| --- | --- |
| One-time legacy quarantine | `/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/quarantine-legacy-output.mjs` — section 2.6 |
| Start Chrome | `open -na "Google Chrome" --args --remote-debugging-port=9333 --user-data-dir="$HOME/.chrome-mx-debug-profile"` — section 3 |
| Check CDP | `curl --noproxy '*' -sS http://127.0.0.1:9333/json/version` and `/json/list` — section 3 |
| Self-test | `/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs` — section 3 |
| Start collector | `caffeinate -i ... scripts/run-collector.mjs --cdp http://127.0.0.1:9333` — section 3 |
| View events | Total-event and recent-decoded-text queries — section 4 |
| View counters | Ingestion-counter query — section 4 |
| View media | Downloaded-media and media-job queries — section 4 |
| Stop | Press `Ctrl-C` once and allow the 30-second drain window — section 5 |
| Migrate disclaimer content | Stop-check, migration, repeated-run, and verification procedure — section 6 |
