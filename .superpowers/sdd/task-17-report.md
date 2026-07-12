# Task 17 Report: Unified Ledger Materialization

## Status

DONE

Implementation commit: `5620ab058a81b0e95512e1702ad7bc216b2bf1cd`

Required commit subject: `fix: unify ledger materialization`

## Summary

- Routed API manual and JSON imports plus CLI CSV imports through one bounded, atomic ledger import/replay/materialization path.
- Replayed affected accounts canonically, replaced stale positions, and upserted stable as-of portfolio snapshots with cash, market value, realized/unrealized PnL, exposure, pricing status, and ledger quality flags.
- Added non-blocking A-share lot-size and T+1 consistency flags without rejecting otherwise structurally valid trades.
- Added 10,000-row import/replay caps before unbounded CSV or SQLite loading and removed the candidate-ID placeholder query.
- Materialized relevant account snapshots inside the review transaction and published JSON ledger-impact context linking snapshot IDs, advice IDs, and same-day transaction IDs/types.
- Preserved passive collector behavior and made no schema, RID configuration, broker/order, or frontend visual changes.

## TDD Evidence

Initial reviewer regression RED:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py tests/advisor/test_web_api.py::test_api_import_materializes_positions_snapshot_and_profile_exposure tests/advisor/test_web_api.py::test_api_and_cli_imports_materialize_equivalent_account_state tests/advisor/test_coordinator.py::test_review_creates_snapshot_and_links_advice_to_same_day_transactions -q
..........FF...FFF
5 failed, 13 passed in 1.92s
```

Bounded exposure RED:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py::test_duplicate_transaction_ids_within_csv_fail_before_database_write tests/advisor/test_ledger.py::test_ledger_exposure_code_filter_is_explicitly_bounded tests/advisor/test_web_api.py::test_api_import_materializes_multiple_accounts_atomically tests/advisor/test_web_api.py::test_api_import_materializes_positions_snapshot_and_profile_exposure -q
.F..
1 failed, 3 passed in 0.45s
```

## Verification

Required focused matrix:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py tests/advisor/test_web_api.py tests/advisor/test_coordinator.py tests/advisor/test_quality_gate.py -q
........................................................................ [ 44%]
........................................................................ [ 88%]
...................                                                      [100%]
163 passed in 5.41s
```

Complete advisor suite:

```text
.venv311/bin/python -m pytest tests/advisor -q
........................................................................ [ 17%]
........................................................................ [ 34%]
........................................................................ [ 51%]
........................................................................ [ 68%]
........................................................................ [ 85%]
...............................................................          [100%]
423 passed in 7.66s
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
duration_ms 500.591459
```

Static verification:

```text
git diff --check
<no output; exit 0>
```

No live smoke test, Chrome operation, MX page operation, collector start, RID change, broker action, or order action was performed.

## Changed Files

- `advisor/ledger/importer.py`
- `advisor/web/api.py`
- `advisor/coordinator.py`
- `tests/advisor/test_ledger.py`
- `tests/advisor/test_web_api.py`
- `tests/advisor/test_coordinator.py`
- `.superpowers/sdd/task-17-report.md`

## Concerns

Pre-existing untracked `.venv311` and `__pycache__` paths remain untouched and are not included in either Task 17 fix commit.
