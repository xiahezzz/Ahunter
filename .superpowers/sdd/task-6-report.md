# Task 6 Report: Investment Ledger

## Scope

Implemented local investment-ledger math and CSV import in the isolated `event-driven-advisor` worktree. No collector files were changed and no broker connectivity or order submission was added.

## RED/GREEN TDD Evidence

### RED

1. Added `tests/advisor/test_ledger.py` first, before any production ledger code.
2. Ran:

```bash
.venv311/bin/python -m pytest tests/advisor/test_ledger.py -q
```

3. Observed the expected failure:

```text
ModuleNotFoundError: No module named 'advisor.ledger'
```

This confirmed the test was exercising missing functionality rather than passing on existing behavior.

### GREEN

1. Added the minimal production code required by the brief:
   - `advisor/ledger/__init__.py`
   - `advisor/ledger/model.py`
   - `advisor/ledger/importer.py`
   - `config/ledger-import.schema.json`
2. Re-ran:

```bash
.venv311/bin/python -m pytest tests/advisor/test_ledger.py -q
```

3. Observed:

```text
2 passed in 0.01s
```

## Changed Files

- `advisor/ledger/__init__.py`
- `advisor/ledger/model.py`
- `advisor/ledger/importer.py`
- `config/ledger-import.schema.json`
- `tests/advisor/test_ledger.py`

## Tests Run

- `.venv311/bin/python -m pytest tests/advisor/test_ledger.py -q`

## Self-Review

- Kept implementation scoped to the exact files allowed by the brief plus this report.
- Matched the required public interfaces:
  - `LedgerTransaction`
  - `apply_transactions(transactions: list[LedgerTransaction]) -> LedgerState`
  - `load_ledger_csv(path: Path) -> list[LedgerTransaction]`
- Preserved the advisor boundary: local ledger math only, no broker integration and no collector changes.
- Followed the brief’s minimal behavior, including proportional cost-basis handling for sells and CSV parsing with typed fields.
- Did not run unrelated tests in this task; only the targeted ledger test required by the RED/GREEN cycle was executed.

## Review Fixes

- Added support for schema-valid `fee` and `tax` transactions in `apply_transactions()`.
- Kept CSV `amount` authoritative for signed cash movement, and only subtract `fees` on `buy` and `sell`.
- Replaced `assert tx.code is not None` with explicit `ValueError` validation before any buy/sell state mutation.

## Additional Regression Tests

- `test_apply_fee_transaction_adjusts_cash`
- `test_apply_tax_transaction_adjusts_cash`
- `test_buy_without_code_raises_value_error`
- `test_sell_without_code_raises_value_error`

## Fix Validation

### RED

Ran before the model fix:

```bash
.venv311/bin/python -m pytest tests/advisor/test_ledger.py -q
```

Observed:

```text
4 failed, 2 passed in 0.06s
```

Failures matched the review findings:
- `fee` and `tax` raised `ValueError: unsupported transaction_type`
- buy/sell without `code` raised `AssertionError` instead of `ValueError`

### GREEN

Ran after the model fix:

```bash
.venv311/bin/python -m pytest tests/advisor/test_ledger.py -q
.venv311/bin/python -m pytest tests/advisor -q
```

Observed:

```text
6 passed in 0.02s
18 passed in 0.17s
```
