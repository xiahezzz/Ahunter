# Task 13 Report

## RED

- `.venv311/bin/python -m pytest tests/advisor/test_launchd.py -q` failed with `ModuleNotFoundError: No module named 'advisor.scheduler'`.
- `/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test tests/integration/self-test-report.test.mjs` failed because self-test returned `0` when a Python fixture failed.

## GREEN

- Added `advisor.scheduler.launchd.validate_launchd_template(path: Path) -> bool`.
- Added launchd templates for API KeepAlive, 08:30 premarket, and 22:30 review.
- Updated self-test to run Node tests first, then Python advisor tests with `.venv311/bin/python` when available.
- Added advisor operations guidance without changing collector workflow rules.

## Verification

- `.venv311/bin/python -m pytest tests/advisor/test_launchd.py -q` -> `3 passed in 0.30s`
- `.venv311/bin/python -m pytest tests/advisor -q` -> `473 passed in 8.49s`
- `/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs` -> Node `134` passed and Python `473` passed
- `PATH=/Users/mac/.local/share/chrome-devtools-mcp/node/bin:$PATH /Users/mac/.local/share/chrome-devtools-mcp/node/bin/npm --prefix frontend run build` -> build passed
- `git diff --check` -> passed
