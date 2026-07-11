# Task 11 FastAPI Advisor API Report

## Status

DONE_WITH_CONCERNS

## Files

- Added `advisor/web/__init__.py` and `advisor/web/api.py`.
- Added `tests/advisor/test_web_api.py`.
- Added a narrow verified archive reader to `advisor/reporting/contracts.py`.
- Added focused reader coverage in `tests/advisor/test_reporting.py`.

## TDD Record

RED/GREEN increments completed:

1. Verified archive reader: RED `AttributeError` for missing `read_verified_archive`; GREEN `1 passed`.
2. Health API: RED `ModuleNotFoundError: advisor.web`; GREEN `1 passed`.
3. Empty current-state API: RED `404`; GREEN `2 passed`.
4. Verified report routes: RED `404`; GREEN `1 passed`.
5. Profile routes and malformed JSON rejection: RED `404`; GREEN `1 passed`.
6. Chart containment and symlink rejection: RED `404`; GREEN `1 passed`.
7. Ledger transaction/import routes: RED missing `transactions` response; GREEN `2 passed`.
8. Ledger signing/code validation: RED invalid deposit accepted with `201`; GREEN `1 passed`.
9. Malformed profile exclusion from listing: RED invalid profile listed; GREEN `1 passed`.
10. Blocking quality fail-closed dashboard state: RED advice exposed; GREEN `1 passed`.

## Verification

Exact commands and results:

```text
.venv311/bin/python -m pytest tests/advisor/test_web_api.py -q
9 passed in 0.40s

.venv311/bin/python -m pytest tests/advisor/test_reporting.py -q
55 passed in 0.19s

.venv311/bin/python -m pytest tests/advisor -q
100 passed in 2.67s

/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
133 passed, 0 failed
```

## Self-review

- Read endpoints do not create state. SQLite reads use `mode=ro`; missing files/tables return explicit empty or unknown state.
- Report reads use the existing descriptor/no-follow completion-marker and SHA-256 validation. Readers never repair or publish archives.
- Health output is allowlisted and excludes snapshot raw fields, credentials, tokens, and debug identifiers.
- Profiles reject malformed structured JSON; charts require contained, regular, non-symlink PNG paths.
- Ledger writes use a bounded JSON payload, parameterized SQL, `PRAGMA busy_timeout`, `BEGIN IMMEDIATE`, duplicate checks, account creation, replayed oversell validation, and rollback on every rejected import.
- A blocking data-quality check clears advice and review conclusions from current state.
- No collector, RID configuration, MX page operation, external runtime network call, frontend, or real report data was changed.

## Concern

FastAPI's installed Starlette `TestClient` required `httpx2`, which was absent from the existing `.venv311` and `pyproject.toml`. I installed `httpx2` only into the already-untracked local `.venv311` to execute the mandated API tests. The task ownership restriction excluded `pyproject.toml`, so this test-only dependency is not declared for a clean environment.

## Commit

`feat: add advisor api`
