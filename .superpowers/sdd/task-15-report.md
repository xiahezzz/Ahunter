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

## Reviewer Fix: MX Quality Gate Hardening

### Status

DONE

Implementation commit: `1e80a6eec04d3d984b5075d6786f8df725fa9efa`

Required commit subject: `fix: harden mx quality gate`

### Fix Summary

- Enforced run `as_of` against snapshot, received, source-created, and media-download timestamps in evidence persistence; the quality gate independently detects forged future collector boundaries.
- Expanded redaction for complete Authorization/Bearer, Cookie, standalone bearer, JWT-like, prefixed, and assignment secrets at returned-object and persistence boundaries.
- Replaced each run's complete persisted quality-check set atomically with transaction/savepoint rollback behavior.
- Required persisted `latest_expected_session` source metadata and exact candidate coverage of that session for the trading-calendar check.
- Pinned SQLite reads to a private hard link verified against the held database descriptor, including descriptor-verified WAL/SHM sidecars, while retaining `mode=ro` and `query_only`.

### RED Evidence

The nine focused reviewer regressions initially produced:

```text
9 failed in 0.38s
```

Failures covered incomplete secret redaction, pathname reopening, future source/media persistence, future snapshot acceptance, future collector leakage, stale latest-session coverage, missing expected-session details, and stale persisted optional-source checks. After correcting a test-wrapper argument collision, the descriptor path-swap regression failed on the existing pathname identity check with:

```text
ValueError: collector database changed while opening
1 failed in 0.15s
```

### GREEN Evidence

The same nine focused reviewer regressions after implementation:

```text
9 passed in 0.14s
```

Required focused Task 15 and reporting suite:

```text
.venv311/bin/python -m pytest tests/advisor/test_mx_evidence_quality.py tests/advisor/test_quality_gate.py tests/advisor/test_reporting.py -q
116 passed in 0.64s
```

Complete advisor suite:

```text
.venv311/bin/python -m pytest tests/advisor -q
250 passed in 3.95s
```

Offline collector self-test:

```text
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
tests 133
pass 133
fail 0
duration_ms 507.752416
```

Static verification:

```text
git diff --check
<no output; exit 0>
```

A direct read-only inspection also confirmed that the pinned connection sees an uncheckpointed live WAL row and reports `PRAGMA query_only = 1`.

### Concerns

None. No collector implementation, RID values, Chrome session, market provider/backfill, reporting implementation, frontend, broker, or order behavior was changed or invoked.

## Second Reviewer Fix: MX Safety Gap Closure

### Status

DONE

Implementation commit: `a6976c6d122d7722facb3f2920453cfc241538e7`

Required commit subject: `fix: close mx safety gaps`

### Fix Summary

- Centralized sensitive-text redaction for snapshot DTOs, persistence references, and failure reports, including full Authorization/Cookie values, Basic/Bearer credentials, JWT-like values, and token/session/debug assignments.
- Rejected unsafe opaque collector IDs at adapter and persistence boundaries and replaced invalid DTO IDs with a redacted sentinel.
- Required normalized relative media paths under `data/events/media`, with independent persistence validation against URLs, absolute paths, traversal, control characters, secret assignments, and oversized values.
- Moved descriptor pin artifacts to owner-private temporary scratch outside the collector directory while retaining same-filesystem hard links, SQLite `mode=ro`, `query_only`, and WAL sidecar visibility.
- Added authoritative `historical_market_fetch` calendar proof to successful production backfill attempts and bound quality-gate proof to candidate code, selected source, and that source's latest persisted session. Missing, stale, invalid, conflicting, and unrelated claims now fail closed or are ignored as appropriate.

### RED Evidence

The initial focused reviewer regressions produced:

```text
9 failed, 3 passed, 130 deselected in 0.65s
```

Failures covered URL and absolute media paths, unsafe source IDs, forged DTO secret exposure, writes required in a read-only collector directory, forged source-ID persistence, unrelated calendar proof acceptance, conflicting calendar claims, and missing production calendar metadata. The existing traversal and bounded failure-report cases already passed.

Self-review then added two further RED cycles:

```text
test_authoritative_proof_for_unrelated_code_is_ignored
1 failed in 0.15s

test_persistence_rejects_forged_unsafe_media_path
3 failed in 0.12s
```

These exposed unrelated production proof rows incorrectly blocking a candidate and forged media DTOs bypassing persistence validation.

### GREEN Evidence

The initial focused regressions after implementation:

```text
12 passed, 130 deselected in 0.42s
```

The two self-review regressions after correction:

```text
1 passed in 0.09s
4 passed in 0.05s
```

Required focused suite:

```text
.venv311/bin/python -m pytest tests/advisor/test_mx_evidence_quality.py tests/advisor/test_quality_gate.py tests/advisor/test_reporting.py tests/advisor/test_market_backfill.py -q
146 passed in 2.07s
```

Complete advisor suite:

```text
.venv311/bin/python -m pytest tests/advisor -q
266 passed in 4.12s
```

Offline collector self-test:

```text
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
tests 133
pass 133
fail 0
duration_ms 521.531375
```

Static verification:

```text
git diff --check
<no output; exit 0>
```

### Concerns

None. No collector implementation, RID configuration, live smoke test, Chrome operation, frontend, broker, or order behavior was changed or invoked.

## Fourth Reviewer Fix: Stable MX Quality Proofs

### Status

DONE

Implementation commit: `00167d496e6ea64ed4d0def563a058a0c5a14591`

Required commit subject: `fix: stabilize mx quality proofs`

### Fix Summary

- Calendar proof selection now classifies each trading-calendar proof by its proof `as_of` before validating or aggregating claims. Historical proofs are ignored when a current proof exists; only current applicable claims can conflict.
- A stale proof blocks only when no current claim exists. Missing current coverage remains unavailable rather than being mislabeled as stale.
- Evidence persistence and quality requests now share the reporting-contract run-ID shape: `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`. Unsafe IDs are rejected before evidence writes, and invalid quality IDs produce a blocked result without creating `data_quality_checks` rows.

### RED Evidence

The first focused run exposed a missing `pytest` import in the newly parametrized quality test:

```text
NameError: name 'pytest' is not defined
```

After correcting that test setup, the intended RED command was:

```text
.venv311/bin/python -m pytest tests/advisor/test_mx_evidence_quality.py::test_persist_evidence_rejects_unsafe_run_id_before_writing tests/advisor/test_quality_gate.py::test_historical_calendar_proof_is_ignored_when_current_proof_is_available tests/advisor/test_quality_gate.py::test_conflicting_current_calendar_claims_block tests/advisor/test_quality_gate.py::test_unsafe_quality_request_run_id_blocks_without_persisting_checks -q
```

Output:

```text
7 failed, 1 passed in 0.28s
```

The seven failures were the three unsafe evidence run IDs being accepted, the current proof being blocked by a historical proof, and the three unsafe quality run IDs persisting seven checks each. The current-current conflict regression already passed, confirming the RED case isolated historical proof handling.

### GREEN Evidence

The same focused command after implementation:

```text
8 passed in 0.15s
```

### Verification

Required focused suite:

```text
.venv311/bin/python -m pytest tests/advisor/test_mx_evidence_quality.py tests/advisor/test_quality_gate.py tests/advisor/test_reporting.py tests/advisor/test_market_backfill.py -q
176 passed in 2.14s
```

Complete advisor suite:

```text
.venv311/bin/python -m pytest tests/advisor -q
296 passed in 4.02s
```

Offline collector self-test:

```text
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
tests 133
pass 133
fail 0
duration_ms 490.065667
```

Static verification:

```text
git diff --check
<no output; exit 0>
```

### Concerns

None. No collector implementation, RID configuration, live smoke test, Chrome operation, frontend, broker, or order behavior was changed or invoked.

## Third Reviewer Fix: Authoritative MX Quality Proof

### Status

DONE

Implementation commit: `e93ef78`

Required commit subject: `fix: make mx quality proof authoritative`

### Fix Summary

- Replaced bar-derived calendar self-proof with an independent `trading_calendar` contract containing a bounded calendar source, proof `as_of`, latest expected session, and explicit candidate coverage or A-share scope. Current applicable claims must agree, and candidate rows must cover the independently expected session.
- Kept successful backfill metadata non-authoritative by recording `historical_market_fetch` with `actual_latest_session`; production fetches no longer write `latest_expected_session` or calendar-source claims derived from their own bars.
- Required optional-source alternates to prove candidate-specific requested interval coverage, persisted three-year boundaries, and the current authoritative expected session. One recent row and unrelated source/code metadata no longer qualify.
- Added complete pre-transaction validation for every `MxEvidence` identity, timestamp, summary, and media field, including recomputation of the domain-separated evidence ID. Forged DTOs fail without entering a transaction or writing rows.
- Enforced safe media-path serialization, expanded compound secret-name redaction, and replaced failure-report caller prose with fixed reason-code names and descriptions.

### RED Evidence

Calendar authority, alternate coverage, and backfill metadata regressions initially produced:

```text
.venv311/bin/python -m pytest tests/advisor/test_quality_gate.py tests/advisor/test_market_backfill.py -q
12 failed, 28 passed in 1.83s
```

DTO validation, media serialization, compound secret names, and failure-report regressions initially produced:

```text
.venv311/bin/python -m pytest tests/advisor/test_mx_evidence_quality.py tests/advisor/test_reporting.py -q
18 failed, 111 passed in 0.88s
```

The failures were for the intended missing behaviors: independent calendar proof was unavailable, historical fetches self-authorized, single-row/unrelated alternates were accepted, forged DTOs persisted or were silently excluded, unsafe paths and compound secrets serialized, and caller advisory prose reached failure archives.

### GREEN Evidence

Required focused suite:

```text
.venv311/bin/python -m pytest tests/advisor/test_mx_evidence_quality.py tests/advisor/test_quality_gate.py tests/advisor/test_reporting.py tests/advisor/test_market_backfill.py -q
169 passed in 2.15s
```

Complete advisor suite:

```text
.venv311/bin/python -m pytest tests/advisor -q
289 passed in 4.11s
```

Offline collector self-test:

```text
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
tests 133
pass 133
fail 0
duration_ms 486.681291
```

Static verification:

```text
git diff --check
<no output; exit 0>

.venv311/bin/python -m compileall -q advisor/evidence advisor/quality.py advisor/reporting advisor/db/repository.py
<no output; exit 0>
```

### Concerns

None. No collector implementation, RID configuration, live smoke test, Chrome operation, frontend, broker, or order behavior was changed or invoked.
