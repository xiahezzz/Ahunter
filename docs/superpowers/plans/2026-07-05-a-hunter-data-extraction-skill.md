# A Hunter Data Extraction Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create and validate a personal Codex skill that extracts A Hunter SQLite records and copies selected original media without analyzing or transforming content.

**Architecture:** Use a single instruction file at `~/.codex/skills/analyze-a-hunter-data/SKILL.md` plus generated `agents/openai.yaml` metadata. The skill uses `sqlite3 -readonly` for all database access, `media.local_path` for legacy/current media discovery, and byte-preserving filesystem copies only after the user requests a destination.

**Tech Stack:** Codex Skill Markdown/YAML, SQLite 3 CLI, POSIX shell commands, skill-creator `init_skill.py` and `quick_validate.py`.

## Global Constraints

- Permit filtering, sorting, pagination, joins, `COUNT`, `SUM`, table display, CSV/JSON export, media-path return, and original-media copying.
- Prohibit summarization, classification, cleaning, rewriting, OCR, entity/sentiment/topic/investment analysis, inference, and image transformation.
- Permit raw-payload extraction by default, limited to the rows and fields requested by the user.
- Require SQLite read-only mode and prohibit all schema/data/config/runtime mutations.
- Require explicit user destinations before CSV, JSON, or media files are written.
- Preserve raw stored values and source media bytes; detect filename collisions instead of overwriting.
- Do not modify or commit `config/allowed-rids.yaml`.

## File Structure

- Create: `~/.codex/skills/analyze-a-hunter-data/SKILL.md` — trigger metadata, extraction boundaries, dimensions, workflow, and exact query/export/copy patterns.
- Create: `~/.codex/skills/analyze-a-hunter-data/agents/openai.yaml` — generated user-facing metadata.
- No scripts, references, assets, README, or other auxiliary files.

---

### Task 1: Initialize, Author, and Validate the Extraction Skill

**Files:**
- Create: `~/.codex/skills/analyze-a-hunter-data/SKILL.md`
- Create: `~/.codex/skills/analyze-a-hunter-data/agents/openai.yaml`

**Interfaces:**
- Consumes: an A Hunter project root containing `data/state/events.sqlite` and media paths stored in `media.local_path`.
- Produces: `$analyze-a-hunter-data`, a read-only extraction workflow with terminal, CSV, JSON, and original-media outputs.

- [ ] **Step 1: Confirm the destination does not already exist**

Run:

```bash
test ! -e "$HOME/.codex/skills/analyze-a-hunter-data"
```

Expected: exit 0. If the directory exists, stop and inspect it rather than overwriting an existing skill.

- [ ] **Step 2: Initialize the skill with generated interface metadata**

Run the skill-creator initializer without optional resource directories:

```bash
python3 /Users/mac/.codex/skills/.system/skill-creator/scripts/init_skill.py \
  analyze-a-hunter-data \
  --path "$HOME/.codex/skills" \
  --interface 'display_name=A Hunter Data Extractor' \
  --interface 'short_description=Extract A Hunter SQLite records and original media' \
  --interface 'default_prompt=Use $analyze-a-hunter-data to extract requested A Hunter records or original media without analyzing them.'
```

Expected: the skill directory, template `SKILL.md`, and `agents/openai.yaml` are created.

- [ ] **Step 3: Replace the template with the extraction-only instructions**

Write `SKILL.md` with exactly two frontmatter fields:

```yaml
---
name: analyze-a-hunter-data
description: Extract stored A Hunter events, raw payloads, decoded text, parsed JSON, media metadata, original media files, media-job state, ingestion counters, and decode-failure records from the local SQLite database. Use when Codex must retrieve, filter, join, paginate, count, export, or copy A Hunter data without analyzing, summarizing, classifying, cleaning, OCRing, interpreting, or transforming it.
---
```

The body must contain these operational sections and rules:

```markdown
# A Hunter Data Extraction

## Boundary

Perform extraction only. Allow filtering, sorting, pagination, joins, `COUNT`,
`SUM`, terminal display, CSV/JSON export, and byte-preserving copies of selected
original media. Do not summarize, classify, clean, rewrite, OCR, identify
entities, infer sentiment/topics/meaning, make investment judgments, or modify
images.

Never modify the database, source media, configuration, or runtime files.
Do not use `INSERT`, `UPDATE`, `DELETE`, `REPLACE`, `CREATE`, `ALTER`, `DROP`,
`VACUUM`, writable PRAGMAs, or migration commands.

## Locate the project and database

Resolve the project root from the user's supplied path or current workspace.
Require both `src/events/schema.sql` and `data/state/events.sqlite`. If multiple
matches exist, ask the user which project to use. Set `DB` to the absolute
database path and verify it exists before querying.

Always invoke SQLite with `-readonly`. Keep raw millisecond timestamp columns
in every export; formatted times may only be additional display columns.

## Supported dimensions

- Events: event ID, schema version, RID, source message ID, OID, receive/source
  timestamps, raw payload/hash/expiry, decoded text, parsed JSON, content hash,
  and ingest run ID.
- Runs: run ID and start timestamp.
- Media: event ID, RID, source URL, URL/content hashes, content type, authoritative
  local path, and download timestamp.
- Media jobs: event ID, RID, source URL/hash, status, attempts, next-attempt
  timestamp, and error code.
- Operational records: ingestion counter bucket/kind/count and decode-failure
  hash/class/bucket/count.
- Joins: event-media, event-run, and media-job-parent-event.

`media.local_path` is authoritative for both legacy hash-prefix and current
date-grouped media layouts. Raw payload extraction is allowed by default, but
select only the fields and rows required by the request.

## Extraction workflow

1. Confirm project, requested fields, filters, ordering, limit, and output mode.
2. If a file export or media copy is requested, require an explicit destination.
3. Inspect column names from `src/events/schema.sql`; do not guess columns.
4. Build one read-only `SELECT` or `WITH ... SELECT` statement. Use parameters
   for user-provided values and add a deterministic `ORDER BY` plus a bounded
   `LIMIT` unless the user explicitly requests all matching rows.
5. Run the query in table, CSV, or JSON mode.
6. For media copies, query paths first, validate that every source is a regular
   file, preflight all destination collisions, then copy without changing bytes.
7. Report query scope, returned row count, requested output path, and missing or
   unavailable fields. Do not interpret the extracted content.

## Read-only query patterns

Use table output for inspection:

    sqlite3 -readonly -header -column "$DB" "SELECT ... ORDER BY ... LIMIT ...;"

Use `.parameter` for user inputs:

    sqlite3 -readonly -header -column \
      -cmd '.parameter init' \
      -cmd '.parameter set :rid 20099' \
      "$DB" \
      'SELECT event_id, rid, received_at, decoded_text
         FROM events
        WHERE rid = :rid
        ORDER BY received_at DESC, event_id
        LIMIT 100;'

Use `LEFT JOIN` when events without media must remain visible:

    SELECT e.event_id, e.rid, e.received_at, e.decoded_text,
           m.content_type, m.local_path, m.downloaded_at
      FROM events AS e
      LEFT JOIN media AS m ON m.event_id = e.event_id AND m.rid = e.rid
     WHERE e.received_at >= :from_ms AND e.received_at < :to_ms
     ORDER BY e.received_at, e.event_id, m.local_path;

Allow basic aggregates only when requested:

    SELECT rid, count(*) AS event_count
      FROM events
     WHERE received_at >= :from_ms AND received_at < :to_ms
     GROUP BY rid
     ORDER BY rid;

## Export records

Write only to a destination explicitly requested by the user. Refuse an existing
output file unless the user explicitly authorizes replacement.

CSV:

    sqlite3 -readonly -header -csv "$DB" "SELECT ...;" > "$OUTPUT.csv"

JSON:

    sqlite3 -readonly -json "$DB" "SELECT ...;" > "$OUTPUT.json"

After export, report the exact path and row count. Do not alter field values or
replace raw millisecond timestamps with formatted strings.

## Return or copy original media

To return media references, select `event_id`, `rid`, `content_type`,
`content_hash`, and `local_path`. Do not open images for OCR or interpretation.

For a requested copy, first export one authoritative `local_path` per line to a
temporary path list. Reject missing files, non-regular files, duplicate target
filenames, and any target that already exists. Create the requested destination,
copy with `cp -p`, and compare SHA-256 before reporting success. Do not resize,
convert, rename, annotate, or recompress files.

## Result contract

Return only extraction facts: database path, selected tables/fields, filters,
ordering, limit, row count, export/copy destination, and missing/unavailable
items. Include no conclusions or characterization of record contents.
```

- [ ] **Step 4: Validate the skill structure**

Run:

```bash
python3 /Users/mac/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  "$HOME/.codex/skills/analyze-a-hunter-data"
```

Expected: validation succeeds.

- [ ] **Step 5: Inspect generated output and enforce scope**

Run:

```bash
rg -n 'TODO|TBD|INSERT|UPDATE|DELETE|OCR|sentiment|summary|readonly|media.local_path' \
  "$HOME/.codex/skills/analyze-a-hunter-data"
find "$HOME/.codex/skills/analyze-a-hunter-data" -maxdepth 3 -type f -print
```

Expected: no placeholders; mutation/analysis terms appear only in prohibitions; `readonly` and `media.local_path` rules are present; only `SKILL.md` and `agents/openai.yaml` exist.

## Completion Criteria

- `quick_validate.py` succeeds.
- The Skill describes extraction, dimensions, read-only SQL, basic aggregates, CSV/JSON export, and safe original-media copying.
- The Skill contains no content-analysis or transformation workflow.
- Personal Skill files exist only under `~/.codex/skills/analyze-a-hunter-data/`.
- The project worktree retains only the user's pre-existing `config/allowed-rids.yaml` modification.
