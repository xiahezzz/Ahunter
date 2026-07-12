# Task 5 Report: MX Evidence Adapter and Quality Gates

## Scope

Implemented the MX evidence adapter and deterministic quality-gate surface in the `event-driven-advisor` worktree, limited to the files named in the task brief and without touching collector code.

## RED

Added `tests/advisor/test_mx_evidence_quality.py` first, before any production code.

Targeted failing command:

```bash
.venv311/bin/python -m pytest tests/advisor/test_mx_evidence_quality.py -q
```

Observed failure:

```text
ModuleNotFoundError: No module named 'advisor.evidence'
```

This was the expected missing-feature failure from the brief.

## GREEN

Added the minimal production code required to satisfy the new tests:

- `advisor/evidence/__init__.py`
- `advisor/evidence/mx_adapter.py`
- `advisor/quality.py`

Key behavior implemented:

- `NormalizedEvent` frozen dataclass for normalized MX evidence rows
- `read_mx_events(...)` reading directly from the collector `events` SQLite table shape used by the tests
- deterministic stock-code extraction from decoded MX text using a bounded regex
- preservation of collector event identity and decoded text as normalized evidence summary
- `QualityResult` frozen dataclass with a `blocking_failure` property
- `evaluate_quality(...)` blocking history check against `market_daily` for required codes
- `has_blocking_failure(...)` convenience helper for caller-side gate decisions

Targeted passing command:

```bash
.venv311/bin/python -m pytest tests/advisor/test_mx_evidence_quality.py -q
```

Result:

```text
2 passed in 0.03s
```

## Regression Check

Broader advisor suite:

```bash
.venv311/bin/python -m pytest tests/advisor -q
```

Result:

```text
10 passed in 0.12s
```

## Changed Files

- `advisor/evidence/__init__.py`
- `advisor/evidence/mx_adapter.py`
- `advisor/quality.py`
- `tests/advisor/test_mx_evidence_quality.py`

## Self-Review

- Kept the implementation constrained to the evidence and quality interfaces in the brief.
- Left collector behavior untouched; the adapter is read-only over the existing `events` table shape used in tests.
- Chose the smallest deterministic quality gate that blocks when required history is absent, matching the test surface and leaving room for later expansion.
- Did not introduce schema, config, or repository changes outside the allowed file set.

## Concerns

- `NormalizedEvent.as_of` currently carries the collector `received_at` integer as a string because the brief’s snippet and tests only require deterministic pass-through, not timestamp normalization.
- `evaluate_quality(...)` currently checks for any `market_daily` history rather than enforcing a true three-year window. That matches the provided brief snippet and tests, but a later task may need a stricter rule once the final data-quality contract is specified.

## Fix After Review

Addressed the controller-approved design requirement that `evaluate_quality(connection, required_codes, as_of)` must block unless each required code has both:

- at least one `market_daily` row with `trade_date <= as_of_date`
- at least one `market_daily` row with `trade_date <= as_of_date - 3 years`

Implementation notes:

- parsed `as_of` with `datetime.fromisoformat(...).date()`
- computed the three-year cutoff with `date.replace(year=year - 3)`
- handled Feb 29 by falling back to Feb 28
- kept zero-row behavior blocking
- made the gate deterministic with explicit existence checks instead of a raw row count

Added coverage for the review gap:

- a single recent row is still a blocking failure
- a row exactly three years before `as_of` plus a recent row passes

RED command and output:

```bash
.venv311/bin/python -m pytest tests/advisor/test_mx_evidence_quality.py -q
```

```text
..F.                                                                     [100%]
FAILED tests/advisor/test_mx_evidence_quality.py::test_quality_blocks_when_only_recent_market_row_exists
1 failed, 3 passed in 0.06s
```

GREEN targeted command and output:

```bash
.venv311/bin/python -m pytest tests/advisor/test_mx_evidence_quality.py -q
```

```text
4 passed in 0.04s
```

Regression command and output:

```bash
.venv311/bin/python -m pytest tests/advisor -q
```

```text
12 passed in 0.17s
```
