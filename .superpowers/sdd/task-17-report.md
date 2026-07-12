# Task 17 Report: Ledger Replay Parity Fix

## Status

DONE

Implementation commit: `e7e2ea3f9717ff9dbbeb73ed6b9156b383357e5e`

Required commit subject: `fix: harden ledger replay parity`

## Summary

- Added one shared deterministic replay key ordered by trade date, cash, buy, sell, then fee/tax, with transaction ID only breaking ties inside a transaction type.
- Same-day buys now replay before lexically earlier sells, allowing imports to complete while retaining the non-blocking A-share T+1 quality flag.
- Review runs materialize snapshots for every account with eligible ledger activity, including cash-only and unrelated accounts.
- Review snapshot IDs include the review run source so repeated account/as-of materializations remain separate durable rows; import snapshots retain their existing stable upsert behavior.
- Review ledger context now includes `pricing_status` and `quality_flags` for every materialized account.
- Removed the API's duplicate ledger validator and routed API and CSV imports through the shared model rules, including Beijing `430047` support.
- Added a 5 MiB CSV limit, a 4096-character CSV field limit, and a 4096-character API ledger field limit before transaction construction.
- Preserved passive collector behavior and made no RID configuration, broker/order, frontend visual, schema, or unrelated changes.

## TDD Evidence

Initial regression RED:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py::test_same_day_buy_replays_before_lexically_earlier_sell_and_flags_t_plus_one tests/advisor/test_ledger.py::test_csv_import_rejects_file_over_byte_limit_before_database_write tests/advisor/test_ledger.py::test_csv_import_rejects_oversized_field_before_database_write tests/advisor/test_web_api.py::test_api_and_csv_share_beijing_stock_code_validation tests/advisor/test_web_api.py::test_api_ledger_rejects_oversized_string_field_before_database_write tests/advisor/test_coordinator.py::test_review_versions_snapshots_for_every_active_ledger_account -q
FFFFFF                                                                   [100%]
6 failed in 2.03s
```

After correcting the unrelated-account fixture, the review regression failed on the missing snapshot coverage:

```text
.venv311/bin/python -m pytest tests/advisor/test_coordinator.py::test_review_versions_snapshots_for_every_active_ledger_account -q
F                                                                        [100%]
1 failed in 1.47s
```

Focused regression GREEN:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py::test_same_day_buy_replays_before_lexically_earlier_sell_and_flags_t_plus_one tests/advisor/test_ledger.py::test_csv_import_rejects_file_over_byte_limit_before_database_write tests/advisor/test_ledger.py::test_csv_import_rejects_oversized_field_before_database_write tests/advisor/test_web_api.py::test_api_and_csv_share_beijing_stock_code_validation tests/advisor/test_web_api.py::test_api_ledger_rejects_oversized_string_field_before_database_write tests/advisor/test_coordinator.py::test_review_versions_snapshots_for_every_active_ledger_account -q
......                                                                   [100%]
6 passed in 1.39s
```

## Verification

Required focused matrix:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py tests/advisor/test_web_api.py tests/advisor/test_coordinator.py tests/advisor/test_quality_gate.py -q
........................................................................ [ 42%]
........................................................................ [ 85%]
........................                                                 [100%]
168 passed in 5.37s
```

Complete advisor suite:

```text
.venv311/bin/python -m pytest tests/advisor -q
........................................................................ [ 16%]
........................................................................ [ 33%]
........................................................................ [ 50%]
........................................................................ [ 67%]
........................................................................ [ 84%]
....................................................................     [100%]
428 passed in 7.61s
```

Offline collector self-test after the final code edit:

```text
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
tests 133
pass 133
fail 0
cancelled 0
skipped 0
todo 0
duration_ms 516.612041
```

Static verification:

```text
git diff --check
<no output; exit 0>
```

No live smoke test, Chrome operation, MX page operation, collector start, RID change, broker action, or order action was performed.

## Changed Files

- `advisor/ledger/model.py`
- `advisor/ledger/importer.py`
- `advisor/web/api.py`
- `advisor/coordinator.py`
- `tests/advisor/test_ledger.py`
- `tests/advisor/test_web_api.py`
- `tests/advisor/test_coordinator.py`
- `.superpowers/sdd/task-17-report.md`

## Concerns

Pre-existing untracked `.venv311` and `__pycache__` paths remain untouched and are not included in either Task 17 fix commit.

## Task 17 Final Boundedness Fix

Implementation commit: `f752ad71cb4a528b4cd7b355a2659da3baa6ef71`

Required commit subject: `fix: bound ledger materialized state`

### Summary

- Rejects non-finite replayed cash, realized P&L, and position cost basis through a shared `LedgerState` validator.
- Computes and validates all affected account states, position exposures, market values, and unrealized P&L aggregates before issuing position or snapshot writes.
- Rolls back ledger transactions, positions, and snapshots atomically when replay or materialized aggregates are non-finite.
- Bounds API imports before `LedgerTransaction` construction by top-level row count, per-row key count, a fixed allowed key set, and string length.
- Rejects nested lists/dictionaries and oversized unknown fields with deterministic HTTP 422 responses and no database writes.
- Preserves passive collector behavior and makes no RID configuration, broker/order, frontend visual, schema, or unrelated changes.

### TDD Evidence

Initial regression RED:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py::test_import_rejects_non_finite_replayed_cash_atomically tests/advisor/test_web_api.py::test_ledger_import_rejects_non_finite_replayed_cash_atomically tests/advisor/test_web_api.py::test_ledger_import_rejects_nested_values_before_database_write tests/advisor/test_web_api.py::test_ledger_import_rejects_huge_unknown_field_before_database_write -q
FFFF.                                                                    [100%]
4 failed, 1 passed in 0.85s
```

Explicit per-row key-bound RED:

```text
.venv311/bin/python -m pytest tests/advisor/test_web_api.py::test_ledger_import_rejects_rows_over_key_limit_before_database_write -q
F                                                                        [100%]
1 failed in 0.57s
```

Focused regression GREEN:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py::test_import_rejects_non_finite_replayed_cash_atomically tests/advisor/test_web_api.py::test_ledger_import_rejects_non_finite_replayed_cash_atomically tests/advisor/test_web_api.py::test_ledger_import_rejects_nested_values_before_database_write tests/advisor/test_web_api.py::test_ledger_import_rejects_huge_unknown_field_before_database_write tests/advisor/test_web_api.py::test_ledger_import_rejects_rows_over_key_limit_before_database_write -q
......                                                                   [100%]
6 passed in 0.33s
```

Snapshot aggregate regression:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py::test_import_rejects_non_finite_snapshot_aggregates_atomically -q
.                                                                        [100%]
1 passed in 0.22s
```

### Verification

Required focused matrix:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py tests/advisor/test_web_api.py tests/advisor/test_coordinator.py tests/advisor/test_quality_gate.py -q
........................................................................ [ 41%]
........................................................................ [ 82%]
...............................                                          [100%]
175 passed in 5.51s
```

Complete advisor suite:

```text
.venv311/bin/python -m pytest tests/advisor -q
........................................................................ [ 16%]
........................................................................ [ 33%]
........................................................................ [ 49%]
........................................................................ [ 66%]
........................................................................ [ 82%]
........................................................................ [ 99%]
...                                                                      [100%]
435 passed in 7.83s
```

Offline collector self-test:

```text
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
tests 133
pass 133
fail 0
cancelled 0
skipped 0
todo 0
duration_ms 494.829417
```

Static verification:

```text
git diff --check
<no output; exit 0>
```

No live smoke test, browser screenshot QA, MX page operation, collector start, RID change, broker action, or order action was performed.

## Final CSV Import Contract Fix

Implementation commit: `1dc8834`

Required commit subject: `fix: enforce ledger csv import contract`

### Summary

- Enforced the public `import_ledger_csv(db_path, csv_path, *, account_id=...)` contract by making `account_id` required and keyword-only.
- Removed suffix-based reversed-order compatibility so the first positional path is always the database and the second is always the CSV data file.
- Updated direct CSV import tests and API parity tests to use the required order explicitly.

### RED Evidence

Missing required `account_id` and suffix-based path swapping initially failed as expected:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py::test_import_ledger_csv_requires_account_id_keyword tests/advisor/test_ledger.py::test_import_ledger_csv_does_not_swap_paths_based_on_suffix -q
FF                                                                       [100%]
Failed: DID NOT RAISE TypeError
FileNotFoundError: [Errno 2] No such file or directory: '.../advisor.csv'
2 failed in 0.31s
```

### GREEN Evidence

Focused contract tests after removing compatibility:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py::test_import_ledger_csv_requires_account_id_keyword tests/advisor/test_ledger.py::test_import_ledger_csv_does_not_swap_paths_based_on_suffix -q
..                                                                       [100%]
2 passed in 0.27s
```

Ledger import suite:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py -q
............................                                             [100%]
28 passed in 0.44s
```

### Verification

Required focused matrix:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py tests/advisor/test_web_api.py tests/advisor/test_coordinator.py tests/advisor/test_quality_gate.py -q
........................................................................ [ 38%]
........................................................................ [ 76%]
.............................................                            [100%]
189 passed in 5.61s
```

Complete advisor suite:

```text
.venv311/bin/python -m pytest tests/advisor -q
........................................................................ [ 16%]
........................................................................ [ 32%]
........................................................................ [ 48%]
........................................................................ [ 64%]
........................................................................ [ 80%]
........................................................................ [ 96%]
.................                                                        [100%]
449 passed in 7.88s
```

Offline collector self-test:

```text
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
tests 133
pass 133
fail 0
cancelled 0
skipped 0
todo 0
duration_ms 506.465125
```

Static verification before the code commit:

```text
git diff --check
<no output; exit 0>
```

No live smoke test, browser screenshot QA, MX page operation, collector start, RID change, broker action, or order action was performed.

### Changed Files

- `advisor/ledger/importer.py`
- `tests/advisor/test_ledger.py`
- `tests/advisor/test_web_api.py`
- `.superpowers/sdd/task-17-report.md`

### Concerns

Pre-existing untracked `.venv311` and `__pycache__` paths remain untouched and are not included in the final contract-fix commits.

## Final Interface/Boundary Fixes

### RED Evidence

Plan-contract positional import order initially failed because `import_ledger_csv(db_path, csv_path, ...)` treated the database path as the CSV:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py -q -k 'plan_contract'
F                                                                        [100%]
FileNotFoundError: [Errno 2] No such file or directory: '.../advisor.sqlite'
1 failed, 25 deselected in 0.19s
```

Ledger API bounded malformed JSON bodies initially escaped as parser exceptions instead of deterministic 4xx responses:

```text
.venv311/bin/python -m pytest tests/advisor/test_web_api.py -q -k 'json_value_errors or json_recursion_errors'
FFFF                                                                     [100%]
ValueError: Exceeds the limit (4300 digits) for integer string conversion
RecursionError: maximum recursion depth exceeded while decoding a JSON array from a unicode string
4 failed, 88 deselected in 1.87s
```

### GREEN Evidence

Focused plan-contract import test:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py -q -k 'plan_contract'
.                                                                        [100%]
1 passed, 25 deselected in 0.31s
```

Focused malformed JSON decode tests:

```text
.venv311/bin/python -m pytest tests/advisor/test_web_api.py -q -k 'json_value_errors or json_recursion_errors'
....                                                                     [100%]
4 passed, 88 deselected in 0.36s
```

Ledger and web coverage:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py tests/advisor/test_web_api.py -q
........................................................................ [ 61%]
..............................................                           [100%]
118 passed in 2.39s
```

### Final Verification

Required focused matrix:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py tests/advisor/test_web_api.py tests/advisor/test_coordinator.py tests/advisor/test_quality_gate.py -q
........................................................................ [ 38%]
........................................................................ [ 77%]
...........................................                              [100%]
187 passed in 5.58s
```

Complete advisor suite:

```text
.venv311/bin/python -m pytest tests/advisor -q
........................................................................ [ 16%]
........................................................................ [ 32%]
........................................................................ [ 48%]
........................................................................ [ 64%]
........................................................................ [ 80%]
........................................................................ [ 96%]
...............                                                          [100%]
447 passed in 7.74s
```

Offline collector self-test:

```text
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
tests 133
pass 133
fail 0
cancelled 0
skipped 0
todo 0
duration_ms 513.71975
```

No live smoke test, browser screenshot QA, MX page operation, collector start, RID change, broker action, or order action was performed.

### Changed Files

- `advisor/ledger/model.py`
- `advisor/ledger/importer.py`
- `advisor/web/api.py`
- `tests/advisor/test_ledger.py`
- `tests/advisor/test_web_api.py`
- `.superpowers/sdd/task-17-report.md`

### Concerns

Pre-existing untracked `.venv311` and `__pycache__` paths remain untouched and are not included in either boundedness commit.

## Task 17 Review Fixes

Implementation commit: `6c89a95`

Required commit subject: `fix: persist ledger review projections`

### Summary

- Split review snapshot materialization so historical snapshots use the review `as_of`, while current `positions` are refreshed from full ledger history and do not regress after later trades.
- Added durable `advice_trade_matches` projection rows linking same-day ledger transactions to advice by code/date/run, without broker or order capability.
- Added raw request byte caps before JSON decoding for `/api/ledger/transactions` and `/api/ledger/import`, while preserving parsed-shape bounds.
- Removed the unused duplicate API candidate replay validator.

### TDD Evidence

Initial regression RED:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py::test_historical_review_snapshot_does_not_regress_current_positions tests/advisor/test_coordinator.py::test_review_versions_snapshots_for_every_active_ledger_account tests/advisor/test_web_api.py::test_ledger_transaction_rejects_oversized_raw_body_before_json_decode tests/advisor/test_web_api.py::test_ledger_import_rejects_oversized_raw_body_before_json_decode -q
FFFF                                                                     [100%]
4 failed in 1.69s
```

Focused regression GREEN:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py::test_historical_review_snapshot_does_not_regress_current_positions tests/advisor/test_coordinator.py::test_review_versions_snapshots_for_every_active_ledger_account tests/advisor/test_web_api.py::test_ledger_transaction_rejects_oversized_raw_body_before_json_decode tests/advisor/test_web_api.py::test_ledger_import_rejects_oversized_raw_body_before_json_decode -q
....                                                                     [100%]
4 passed in 1.48s
```

### Verification

Required focused matrix:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py tests/advisor/test_web_api.py tests/advisor/test_coordinator.py tests/advisor/test_quality_gate.py -q
........................................................................ [ 40%]
........................................................................ [ 80%]
..................................                                       [100%]
178 passed in 5.73s
```

Complete advisor suite:

```text
.venv311/bin/python -m pytest tests/advisor -q
........................................................................ [ 16%]
........................................................................ [ 32%]
........................................................................ [ 49%]
........................................................................ [ 65%]
........................................................................ [ 82%]
........................................................................ [ 98%]
......                                                                   [100%]
438 passed in 7.83s
```

Offline collector self-test:

```text
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
tests 133
pass 133
fail 0
cancelled 0
skipped 0
todo 0
duration_ms 508.506
```

Static verification:

```text
git diff --check
<no output; exit 0>
```

No live smoke test, browser screenshot QA, MX page operation, collector start, RID change, broker action, or order action was performed.

### Changed Files

- `advisor/db/schema.sql`
- `advisor/ledger/importer.py`
- `advisor/coordinator.py`
- `advisor/web/api.py`
- `tests/advisor/test_ledger.py`
- `tests/advisor/test_coordinator.py`
- `tests/advisor/test_web_api.py`
- `.superpowers/sdd/task-17-report.md`

### Concerns

Pre-existing untracked `.venv311` and `__pycache__` paths remain untouched and are not included in the review-fix commits.

## Final Review-Loop Fixes

### RED Evidence

Streamed oversized bodies without Content-Length were initially rejected only after both ASGI chunks were consumed:

```text
.venv311/bin/python -m pytest tests/advisor/test_web_api.py -q -k 'streamed_body_over_limit'
FF                                                                       [100%]
2 failed, 86 deselected in 0.59s
```

The required `LedgerStore` facade was initially absent:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py -q -k 'ledger_store'
ModuleNotFoundError: No module named 'advisor.ledger.store'
1 error in 0.19s
```

### GREEN Evidence

Focused streamed-body and existing raw-body bounds:

```text
.venv311/bin/python -m pytest tests/advisor/test_web_api.py -q -k 'streamed_body_over_limit or oversized_raw_body'
....                                                                     [100%]
4 passed, 84 deselected in 0.39s
```

Focused store facade contract:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py -q -k 'ledger_store'
..                                                                       [100%]
2 passed, 23 deselected in 0.14s
```

### Final Verification

Required focused matrix:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py tests/advisor/test_web_api.py tests/advisor/test_coordinator.py tests/advisor/test_quality_gate.py -q
........................................................................ [ 39%]
........................................................................ [ 79%]
......................................                                   [100%]
182 passed in 5.53s
```

Complete advisor suite:

```text
.venv311/bin/python -m pytest tests/advisor -q
........................................................................ [ 16%]
........................................................................ [ 32%]
........................................................................ [ 48%]
........................................................................ [ 65%]
........................................................................ [ 81%]
........................................................................ [ 97%]
..........                                                               [100%]
442 passed in 7.80s
```

Offline collector self-test:

```text
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
tests 133
pass 133
fail 0
cancelled 0
skipped 0
todo 0
duration_ms 510.113542
```

No live smoke test, browser screenshot QA, MX page operation, collector start, RID change, broker action, or order action was performed.

## Final Replay/Quality/CSV Decoder Fixes

Implementation commit: `9c57aa0`

Required commit subject: `fix: restore canonical ledger replay order`

### Summary

- Restored canonical ledger replay ordering to `(account_id, trade_date, transaction_id)` by removing transaction-type priority from the shared transaction sort key.
- Added a same-day net-availability allowance for canonical prefixes that temporarily oversell only because same-day buy/sell transaction IDs are arbitrary; true oversells across days still reject.
- Limited A-share lot-size quality flags to non-lot buys; odd-lot sells remain structurally valid sell-offs without lot-size flags.
- Normalized `_csv.Error` parser failures from `csv.DictReader` fieldname access and iteration into deterministic `ValueError` handling, including CLI usage-error output.

### RED Evidence

Focused regressions initially failed for parser normalization, CLI handling, canonical replay ordering, and odd-lot sell flags:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py::test_load_ledger_csv_normalizes_parser_field_limit_error tests/advisor/test_ledger.py::test_cli_reports_csv_parser_errors_as_usage_errors tests/advisor/test_ledger.py::test_replay_sort_key_and_import_use_canonical_transaction_id_order_with_same_day_allowance tests/advisor/test_ledger.py::test_odd_lot_sells_do_not_emit_lot_size_quality_flags -q
FFFF                                                                     [100%]
4 failed in 0.23s
```

### GREEN Evidence

Focused regressions after the fix:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py::test_load_ledger_csv_normalizes_parser_field_limit_error tests/advisor/test_ledger.py::test_cli_reports_csv_parser_errors_as_usage_errors tests/advisor/test_ledger.py::test_replay_sort_key_and_import_use_canonical_transaction_id_order_with_same_day_allowance tests/advisor/test_ledger.py::test_odd_lot_sells_do_not_emit_lot_size_quality_flags -q
....                                                                     [100%]
4 passed in 0.23s
```

Ledger import suite:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py -q
...............................                                          [100%]
31 passed in 0.47s
```

### Verification

Required focused matrix:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py tests/advisor/test_web_api.py tests/advisor/test_coordinator.py tests/advisor/test_quality_gate.py -q
........................................................................ [ 37%]
........................................................................ [ 75%]
................................................                         [100%]
192 passed in 5.61s
```

Complete advisor suite:

```text
.venv311/bin/python -m pytest tests/advisor -q
........................................................................ [ 15%]
........................................................................ [ 31%]
........................................................................ [ 47%]
........................................................................ [ 63%]
........................................................................ [ 79%]
........................................................................ [ 95%]
....................                                                     [100%]
452 passed in 7.92s
```

Offline collector self-test:

```text
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
tests 133
pass 133
fail 0
cancelled 0
skipped 0
todo 0
duration_ms 526.058334
```

Static verification:

```text
git diff --check
<no output; exit 0>
```

No live smoke test, browser screenshot QA, MX page operation, collector start, RID change, broker action, or order action was performed.

### Changed Files

- `advisor/ledger/model.py`
- `advisor/ledger/importer.py`
- `tests/advisor/test_ledger.py`
- `.superpowers/sdd/task-17-report.md`

### Concerns

Pre-existing untracked `.venv311` and `__pycache__` paths remain untouched and are not included in the final replay-fix commits.
