# Task 16 Report: Coordinator Review and Archive Consistency Fixes

## Status

DONE

Implementation commit: `2226252`

Archive consistency implementation commit: `280469b`

Premarket linkage implementation commit: `9d326aa`

Eligibility capacity implementation commit: `c71125d`

Required commit subject: `fix: complete coordinator review paths`

Archive consistency commit subject: `fix: hide orphan report archives`

Premarket linkage commit subject: `fix: require archived premarket linkage`

Eligibility capacity commit subject: `fix: bound report eligibility queries`

## Summary

- Added working `main` wrappers for both declared report CLI entrypoints. The wrappers parse report inputs, read the authorized collector snapshot, invoke the matching coordinator, and allow dependency injection in tests.
- Review now resolves the exact verified premarket archive selected by `premarket_run_id` and confirms its advice payload against passed premarket database rows for the report date.
- Review blocks fail-closed when the selected archive's advice codes do not exactly equal the quality-checked candidate set.
- Review database projections are atomic across reviews, stock profiles, profile history, chart metadata, and report archive metadata. Report validation or write failures roll back those rows.
- Replaced the fixed `reviewed` outcome with bounded deterministic close/ledger evaluation: `followed_strength`, `missed_or_flat`, `risk_review`, or `no_market_data`. Review text records the compared closes and same-day ledger activity.
- No database schema, collector behavior, RID configuration, frontend, broker, or order behavior changed.
- Report list, detail, and current-state reads now require an exact `report_archive` path match joined to a `passed` advisor run. Failure archives are eligible only when joined to a `blocked` run.
- Marker-verified archives left behind by a rolled-back database transaction remain immutable but are not exposed as completed reports.
- Retrying the same immutable report run ID after such a failure raises `FileExistsError`; the retry's advisor run is committed as `failed`, making the required new report run ID explicit.
- Review now requires the exact selected premarket filesystem archive to have one matching `report_archive` row joined to a `passed` premarket `advisor_runs` row.
- The stored Markdown and JSON paths must normalize to the selected archive paths without resolving or trusting symlinks, and every archived advice item must belong to that one linked database run.
- Orphan premarket archives and archives combining advice from multiple passed premarket runs fail closed before any review rows are written.
- Report listing now checks only the verified filesystem candidates on the requested page, and report detail checks only its exact requested key.
- Current-state eligibility is restricted to its existing 30-day lookback, ordered newest-first, and capped without failing all eligibility when older archive history exceeds the cap.
- More than 500 historical passed archive rows, including rows whose archive files are absent, no longer hide a current database-backed verified report from list, detail, or current-state responses.

## TDD Evidence

The eligibility capacity regression failed before implementation because the global 501-row query returned no eligible keys:

```text
.venv311/bin/python -m pytest tests/advisor/test_web_api.py::test_current_report_remains_eligible_after_archive_history_exceeds_capacity -q
F                                                                        [100%]
1 failed in 0.52s
```

After bounding eligibility by page candidates, exact detail key, and current-state lookback, the regression and existing orphan controls passed:

```text
.venv311/bin/python -m pytest tests/advisor/test_web_api.py::test_current_report_remains_eligible_after_archive_history_exceeds_capacity tests/advisor/test_web_api.py::test_report_routes_list_and_serve_only_verified_archives tests/advisor/test_web_api.py::test_report_routes_hide_verified_archive_without_committed_archive_row -q
...                                                                      [100%]
3 passed in 0.43s
```

The new coordinator regressions failed before implementation with the expected eight failures:

```text
.venv311/bin/python -m pytest tests/advisor/test_coordinator.py -q
...FFFFFFFF.                                                             [100%]
8 failed, 4 passed in 1.97s
```

The failures covered the missing CLI `main` attributes, fixed `reviewed` outcome, date-wide morning advice loading, committed review rows after linkage failure, and missing candidate-scope validation.

A direct report-write rollback test then exposed the remaining internal profile commit:

```text
.venv311/bin/python -m pytest tests/advisor/test_coordinator.py -q
.........F...                                                            [100%]
1 failed, 12 passed in 1.81s
```

After adding the transaction-safe review profile upsert, the coordinator tests passed:

```text
.venv311/bin/python -m pytest tests/advisor/test_coordinator.py -q
.............                                                            [100%]
13 passed in 1.72s
```

The archive consistency regression then failed after a successful filesystem write and forced archive-row insertion failure:

```text
.venv311/bin/python -m pytest tests/advisor/test_coordinator.py::test_review_archive_insert_failure_rolls_back_and_hides_orphan_archive -q
F                                                                        [100%]
1 failed in 1.68s
```

After adding database-backed API eligibility, the same regression passed:

```text
.venv311/bin/python -m pytest tests/advisor/test_coordinator.py::test_review_archive_insert_failure_rolls_back_and_hides_orphan_archive -q
.                                                                        [100%]
1 passed in 1.29s
```

The premarket linkage regressions then failed before implementation because both an orphan archive and a mixed-run archive were accepted:

```text
.venv311/bin/python -m pytest tests/advisor/test_coordinator.py::test_review_rejects_selected_premarket_archive_without_archive_row tests/advisor/test_coordinator.py::test_review_rejects_archive_with_advice_from_multiple_premarket_runs -q
FF                                                                       [100%]
2 failed in 1.32s
```

After requiring the archive row and single linked advice run, the targeted regressions and selected-archive control passed:

```text
.venv311/bin/python -m pytest tests/advisor/test_coordinator.py::test_review_rejects_selected_premarket_archive_without_archive_row tests/advisor/test_coordinator.py::test_review_rejects_archive_with_advice_from_multiple_premarket_runs tests/advisor/test_coordinator.py::test_review_uses_exact_selected_premarket_archive -q
...                                                                      [100%]
3 passed in 1.26s
```

## Verification

Eligibility capacity required focused suite:

```text
.venv311/bin/python -m pytest tests/advisor/test_web_api.py tests/advisor/test_coordinator.py tests/advisor/test_reporting.py tests/advisor/test_quality_gate.py tests/advisor/test_astock_adapter.py tests/advisor/test_profiles.py tests/advisor/test_kline_chart.py -q
........................................................................ [ 32%]
........................................................................ [ 64%]
........................................................................ [ 96%]
.......                                                                  [100%]
223 passed in 4.26s
```

Eligibility capacity complete advisor suite:

```text
.venv311/bin/python -m pytest tests/advisor -q
........................................................................ [ 18%]
........................................................................ [ 36%]
........................................................................ [ 55%]
........................................................................ [ 73%]
........................................................................ [ 92%]
...............................                                          [100%]
391 passed in 6.03s
```

Eligibility capacity offline collector self-test:

```text
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
tests 133
suites 0
pass 133
fail 0
cancelled 0
skipped 0
todo 0
duration_ms 509.462833
```

Eligibility capacity static check:

```text
git diff --check
<no output; exit 0>
```

No live smoke test, Chrome operation, or MX page operation was performed.

Premarket linkage required focused suite:

```text
.venv311/bin/python -m pytest tests/advisor/test_coordinator.py tests/advisor/test_web_api.py tests/advisor/test_reporting.py tests/advisor/test_quality_gate.py tests/advisor/test_astock_adapter.py tests/advisor/test_profiles.py tests/advisor/test_kline_chart.py -q
........................................................................ [ 32%]
........................................................................ [ 64%]
........................................................................ [ 97%]
......                                                                   [100%]
222 passed in 4.03s
```

Premarket linkage complete advisor suite:

```text
.venv311/bin/python -m pytest tests/advisor -q
........................................................................ [ 18%]
........................................................................ [ 36%]
........................................................................ [ 55%]
........................................................................ [ 73%]
........................................................................ [ 92%]
..............................                                           [100%]
390 passed in 5.78s
```

Premarket linkage offline collector self-test:

```text
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
tests 133
suites 0
pass 133
fail 0
cancelled 0
skipped 0
todo 0
duration_ms 515.186834
```

Premarket linkage static check:

```text
git diff --check
<no output; exit 0>
```

No live smoke test, Chrome operation, or MX page operation was performed.

Previous Task 16 archive consistency verification is retained below.

Archive consistency required focused suite:

```text
.venv311/bin/python -m pytest tests/advisor/test_coordinator.py tests/advisor/test_web_api.py tests/advisor/test_reporting.py tests/advisor/test_quality_gate.py tests/advisor/test_astock_adapter.py tests/advisor/test_profiles.py tests/advisor/test_kline_chart.py -q
........................................................................ [ 32%]
........................................................................ [ 65%]
........................................................................ [ 98%]
....                                                                     [100%]
220 passed in 4.11s
```

Archive consistency complete advisor suite:

```text
.venv311/bin/python -m pytest tests/advisor -q
........................................................................ [ 18%]
........................................................................ [ 37%]
........................................................................ [ 55%]
........................................................................ [ 74%]
........................................................................ [ 92%]
............................                                             [100%]
388 passed in 5.82s
```

Archive consistency offline collector self-test:

```text
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
tests 133
suites 0
pass 133
fail 0
cancelled 0
skipped 0
todo 0
duration_ms 511.771208
```

Archive consistency static check:

```text
git diff --check
<no output; exit 0>
```

Previous Task 16 verification is retained below.

Required focused suite:

```text
.venv311/bin/python -m pytest tests/advisor/test_coordinator.py tests/advisor/test_reporting.py tests/advisor/test_quality_gate.py tests/advisor/test_astock_adapter.py tests/advisor/test_profiles.py tests/advisor/test_kline_chart.py -q
........................................................................ [ 48%]
........................................................................ [ 96%]
......                                                                   [100%]
150 passed in 2.51s
```

Required complete advisor suite:

```text
.venv311/bin/python -m pytest tests/advisor -q
........................................................................ [ 18%]
........................................................................ [ 37%]
........................................................................ [ 55%]
........................................................................ [ 74%]
........................................................................ [ 93%]
..........................                                               [100%]
386 passed in 5.48s
```

Required offline collector self-test:

```text
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
tests 133
suites 0
pass 133
fail 0
cancelled 0
skipped 0
todo 0
duration_ms 512.200583
```

Required static check:

```text
git diff --check
<no output; exit 0>
```

Additional syntax check:

```text
.venv311/bin/python -m py_compile advisor/coordinator.py advisor/reporting/premarket.py advisor/reporting/review.py tests/advisor/test_coordinator.py
<no output; exit 0>
```

No collector start, live smoke test, Chrome/MX page operation, RID/config value change, Tushare access, frontend change, broker action, or order behavior was performed.

## Changed Files

- `advisor/coordinator.py`
- `advisor/reporting/premarket.py`
- `advisor/reporting/review.py`
- `tests/advisor/test_coordinator.py`
- `.superpowers/sdd/task-16-report.md`

Archive consistency follow-up:

- `advisor/web/api.py`
- `tests/advisor/test_coordinator.py`
- `tests/advisor/test_web_api.py`
- `.superpowers/sdd/task-16-report.md`

Premarket linkage follow-up:

- `advisor/coordinator.py`
- `tests/advisor/test_coordinator.py`
- `.superpowers/sdd/task-16-report.md`

## Concerns

- Report files remain immutable and cannot participate in the SQLite transaction. Orphans are now hidden by DB-backed API eligibility, but reclaiming their filenames requires a distinct versioned report run ID; same-ID retries fail and are recorded as failed runs.
- Profile Markdown and chart image generation are filesystem projections. Their database metadata is rolled back on report failure, while already-written projection files may remain for a later successful run to replace.
- API report listing still slices the filesystem archive page before filtering against database eligibility. Correct eligible-only pagination requires changing the reporting contract's cursor snapshot semantics outside this follow-up's allowed scope; orphan files can still consume list limits and affect pagination metadata even though they are not exposed.
- Pre-existing untracked `.venv311` and `__pycache__` paths were left untouched and excluded from both commits.
