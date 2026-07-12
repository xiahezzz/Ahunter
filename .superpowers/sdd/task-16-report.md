# Task 16 Report: Advisor Run Coordinator

## Status

DONE

Implementation commit: `35045cd`

Required commit subject: `feat: add advisor run coordinator`

## Summary

- Added injectable `run_premarket` and `run_review` coordinator functions that resolve configured storage, migrate/open the state database, and persist `advisor_runs` lifecycle transitions.
- Added a two-stage premarket quality gate: all non-analyst failures block before analyst execution, then staged analyst outputs must satisfy the complete gate before advice publication.
- Persisted authorized MX evidence, all validated TradingAgents-astock role outputs with JSON-safe payloads, conservative `watch` advice, linked reviews, report archives, stock profiles/history, K-line assets, and profile Markdown.
- Added sanitized failure archives and rollback of unpublished analyst/advice content for quality-gate and `DataQualityBlockedError` failures.
- Made missing K-line data non-blocking: the run records a bounded warning and omits the unavailable profile asset.
- Added a public `persist_quality_results` helper for coordinator-adjusted and injected quality outcomes.

## TDD Evidence

Initial coordinator test collection failed for the intended missing interface:

```text
.venv311/bin/python -m pytest tests/advisor/test_coordinator.py -q
ModuleNotFoundError: No module named 'advisor.coordinator'
1 error in 0.17s
```

After the initial implementation, the five tests reached the report-root security contract and failed with:

```text
5 failed in 1.59s
ValueError: output_dir must equal configured reports root
```

The tests were corrected to configure the injected report root rather than weakening report validation. The coordinator scenarios then passed:

```text
.venv311/bin/python -m pytest tests/advisor/test_coordinator.py -q
.....                                                                    [100%]
5 passed in 1.23s
```

A provenance refinement test then failed before implementation:

```text
.venv311/bin/python -m pytest tests/advisor/test_coordinator.py::test_premarket_happy_path_persists_complete_projection -q
F                                                                        [100%]
AssertionError: assert [] == [('passed',)]
1 failed in 1.01s
```

After persisting injected quality results and linking profile history to its run, all five coordinator tests passed again.

## Verification

Required focused suite:

```text
.venv311/bin/python -m pytest tests/advisor/test_coordinator.py tests/advisor/test_reporting.py tests/advisor/test_quality_gate.py tests/advisor/test_astock_adapter.py tests/advisor/test_profiles.py tests/advisor/test_kline_chart.py -q
........................................................................ [ 50%]
......................................................................   [100%]
142 passed in 2.21s
```

Required complete advisor suite:

```text
.venv311/bin/python -m pytest tests/advisor -q
........................................................................ [ 19%]
........................................................................ [ 38%]
........................................................................ [ 57%]
........................................................................ [ 76%]
........................................................................ [ 95%]
..................                                                       [100%]
378 passed in 5.05s
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
duration_ms 687.193375
```

Required static check:

```text
git diff --check
<no output; exit 0>
```

Additional syntax check:

```text
.venv311/bin/python -m py_compile advisor/coordinator.py advisor/quality.py tests/advisor/test_coordinator.py
<no output; exit 0>
```

No collector start, live smoke test, Chrome/MX page operation, RID/config value change, Tushare access, frontend change, broker action, or order behavior was performed.

## Changed Files

- `advisor/coordinator.py`
- `advisor/quality.py`
- `tests/advisor/test_coordinator.py`
- `.superpowers/sdd/task-16-report.md`

## Concerns

- The coordinator exposes runnable premarket/review functions and validates the configured 08:30/22:30 data boundaries supplied by the caller; process scheduling remains the responsibility of the deployment scheduler.
- K-line generation is intentionally best effort after quality passes. Missing market rows omit the chart asset and add a warning without blocking advice/review publication.
- Pre-existing untracked `.venv311` and `__pycache__` paths were left untouched and excluded from both commits.
