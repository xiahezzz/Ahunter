# Task 17 Report: Durable Ledger Snapshots and Profile Linkage

## Status

DONE

Implementation commit: `8f61f771957f22809111504db1870d2028b79649`

Required commit subject: `feat: add durable ledger snapshots`

## Summary

- Added reusable ledger transaction validation for transaction IDs, dates, types, stock codes, signs, quantities, prices, fees, and trade arithmetic.
- Added an atomic CSV and transaction import API that resolves the operational database from `config/advisor.yaml` by default and supports explicit database, config, account, source, and timezone-aware `as_of` options.
- Added full-account replay before commit, global duplicate-ID rejection, derived position replacement, and deterministic portfolio snapshot upserts.
- Valued snapshots from the latest `quality_status='passed'` close on or before the snapshot date. Missing prices contribute zero and are explicitly marked `missing_price` in exposure JSON.
- Added an exposure-by-code helper and refreshed `stock_profiles.ledger_exposure_json` during premarket and review profile projection.
- Preserved existing review linkage to same-day durable ledger transactions through their persisted `trade_date` and `created_at` values.

## TDD Evidence

Initial importer RED:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py -q
ImportError: cannot import name 'import_ledger_csv' from 'advisor.ledger.importer'
1 error in 0.13s
```

Importer GREEN:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py -q
10 passed in 0.33s
```

Coordinator exposure RED:

```text
.venv311/bin/python -m pytest tests/advisor/test_coordinator.py::test_premarket_happy_path_persists_complete_projection -q
AssertionError: assert {} == {'cost_basis': 1000.0, ...}
1 failed in 1.32s
```

Coordinator and ledger GREEN:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py tests/advisor/test_coordinator.py -q
38 passed in 2.93s
```

## Verification

Required focused matrix:

```text
.venv311/bin/python -m pytest tests/advisor/test_ledger.py tests/advisor/test_coordinator.py tests/advisor/test_web_api.py tests/advisor/test_quality_gate.py -q
152 passed in 4.98s
```

Complete advisor suite:

```text
.venv311/bin/python -m pytest tests/advisor -q
412 passed in 7.16s
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
duration_ms 519.316458
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
- `advisor/coordinator.py`
- `tests/advisor/test_ledger.py`
- `tests/advisor/test_coordinator.py`
- `.superpowers/sdd/task-17-report.md`

## Concerns

None. Pre-existing untracked `.venv311` and `__pycache__` paths were left untouched and are not part of either Task 17 commit.
