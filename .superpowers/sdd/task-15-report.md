# Task 15 Report: Read-Only MX Evidence Handoff and Persisted Quality Gate

## Status

DONE_WITH_CONCERNS

Implementation commit: `40fc4a4b4c4d75eb50362fa9e36d70f8faf07372`

Required commit subject: `feat: add mx evidence quality handoff`

## Summary

- Replaced the synthetic MX adapter with a bounded, allowlist-filtered, read-only collector snapshot adapter using SQLite `mode=ro`, query-only connections, file identity checks, symlink rejection, real collector-schema validation, bounded scans, and fail-closed quality state.
- Added domain-separated evidence IDs, bounded/redacted decoded summaries, bounded local media metadata, and no exposure of raw payloads or source URLs.
- Added idempotent normalized evidence persistence with parameterized SQL, conflict detection, `as_of` filtering, and caller-transaction-preserving savepoints.
- Added a persisted run quality gate for collector state, trading-calendar availability, market staleness, three-year coverage, future leakage, ledger replay, analyst contract readiness, and covered optional-source degradation.
- Added immutable `failure` report publication through the existing claim, hash, marker, and archive verification machinery.

## TDD Evidence

### Initial RED

Command:

```text
.venv311/bin/python -m pytest tests/advisor/test_mx_evidence_quality.py tests/advisor/test_quality_gate.py -q
```

Output:

```text
ERROR tests/advisor/test_mx_evidence_quality.py
ImportError: cannot import name 'read_collector_snapshot'
ERROR tests/advisor/test_quality_gate.py
ImportError: cannot import name 'CollectorSnapshot'
2 errors in 0.14s
```

This failed for the intended reason: the required snapshot and quality-gate interfaces did not exist.

### Initial GREEN

After minimal adapter, persistence, and quality implementation:

```text
25 passed in 0.19s
```

### Refinement RED/GREEN

Transaction ownership, conflicting identity, alternate-source proof, and malformed failure-input tests initially produced:

```text
4 failed in 0.28s
```

After implementing savepoints, identity verification, source binding, and fail-closed report validation:

```text
5 passed in 0.14s
```

Sensitive summary, future fetch timestamp, malformed ledger, and required-source failure tests initially produced:

```text
4 failed in 0.21s
```

After redaction and quality validation:

```text
5 passed in 0.08s
```

## Verification

Focused Task 15 and reporting suite:

```text
.venv311/bin/python -m pytest tests/advisor/test_mx_evidence_quality.py tests/advisor/test_quality_gate.py tests/advisor/test_reporting.py -q
107 passed in 0.52s
```

Complete advisor suite:

```text
.venv311/bin/python -m pytest tests/advisor -q
241 passed in 3.99s
```

Offline collector self-test:

```text
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
tests 133
pass 133
fail 0
duration_ms 499.600875
```

Static checks:

```text
git diff --check
<no output; exit 0>

.venv311/bin/python -m compileall -q advisor/evidence advisor/quality.py advisor/reporting
<no output; exit 0>
```

No collector, live smoke test, MX page operation, RID modification, market provider, broker, or order behavior was invoked or changed.

## Changed Files

- `advisor/evidence/mx_adapter.py`
- `advisor/evidence/service.py`
- `advisor/quality.py`
- `advisor/reporting/failure.py`
- `advisor/reporting/contracts.py`
- `tests/advisor/test_mx_evidence_quality.py`
- `tests/advisor/test_quality_gate.py`
- `tests/advisor/test_reporting.py`
- `.superpowers/sdd/task-15-report.md`

## Concerns

- `analyst_contract_readiness` intentionally requires one persisted output for every `ANALYST_ROLES` role and candidate before the quality gate can pass. Task 16 coordination must evaluate this gate after analyst outputs are staged, while still preventing advice/review publication until the gate passes.
- The collector snapshot blocks when bounded event, counter, media, failure, or source scans exceed their caps. This is intentional fail-closed behavior; operational retention must keep the collector ledger within those review bounds.
- Pre-existing untracked `.venv311` and `__pycache__` paths were left untouched and were not included in the implementation commit.
