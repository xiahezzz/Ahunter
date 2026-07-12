# Final Calendar Review Fixes

## Scope

Fixed the final re-review gaps for A-share calendar completeness, review action semantics, and composed scheduler failure archiving.

## Fixes

- Added omitted official SSE holiday closures to the local bounded calendar: 2025 Labor Day reopening on 2025-05-06, 2026 Spring Festival reopening on 2026-02-24, and 2026 Labor Day reopening on 2026-05-06.
- Added the final omitted official SSE 2026 New Year closure on 2026-01-02, with the 2026-01-05 reopening boundary covered.
- `latest_expected_session()` now fails closed with a deterministic unsupported-calendar error outside the known 2025-2026 table. Direct `is_trading_session()` checks for unsupported dates return closed.
- The quality gate converts unsupported calendar coverage into a blocking `trading_calendar` check instead of inferring weekday-open sessions.
- Review evaluation now uses explicit action sentiment sets: `buy`, `watch_buy`, and `watch_add` are bullish; `hold` and `watch` are neutral; `watch_reduce`, `watch_exit`, `reduce`, `exit`, and `avoid` are bearish.
- Composed premarket and review schedulers now catch runtime/calendar/archive-linkage failures, write best-effort sanitized failure archives, print controlled JSON with status and exception type only, and return nonzero.
- Failure reports now recognize a sanitized `runtime` failure category without archiving raw exception text, evidence, credentials, or analyst prose.

## RED Observed

- Calendar RED: 2025-05-05, 2026-02-23, 2026-05-04, and 2026-05-05 were incorrectly treated as trading sessions; unsupported 2027 weekday dates did not raise.
- Calendar RED: 2026-01-02 was incorrectly treated as a trading session, and `latest_expected_session(2026-01-02T22:30:00+08:00)` incorrectly returned 2026-01-02 instead of 2025-12-31.
- Review semantics RED: `watch_buy` and `watch_add` declines were incorrectly marked `followed_strength`.
- Scheduler RED: composed review archive-linkage failures and premarket unsupported-calendar failures escaped without sanitized failure archives.

## Verification

- `.venv311/bin/python -m pytest tests/advisor/test_quality_gate.py tests/advisor/test_market_backfill.py tests/advisor/test_coordinator.py tests/advisor/test_reporting.py tests/advisor/test_launchd.py -q` -> `199 passed in 5.67s`
- `.venv311/bin/python -m pytest tests/advisor/test_quality_gate.py -q` -> `52 passed in 0.65s`
- `.venv311/bin/python -m pytest tests/advisor -q` -> `521 passed in 9.03s`
- `/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs` -> passed; Node tests `134 passed`, embedded advisor pytest `521 passed in 8.69s`
- `PATH=/Users/mac/.local/share/chrome-devtools-mcp/node/bin:$PATH /Users/mac/.local/share/chrome-devtools-mcp/node/bin/npm --prefix frontend run build` -> passed
- `git diff --check` -> passed

Browser screenshot QA remained skipped per user instruction.
