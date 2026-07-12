# Final Runtime Critical Fixes

## Scope

Fixed the final re-review Critical blockers for production TradingAgents portfolio output and scheduled A-share session quality.

## Fixes

- Production TradingAgents Markdown `final_trade_decision` is parsed through a bounded contract requiring exactly one `Rating`, one non-empty `Executive Summary`, and one non-empty `Investment Thesis`.
- Ratings map to advisor actions and ordinal confidence only: `Buy -> watch_buy/0.80`, `Overweight -> watch_add/0.65`, `Hold -> hold/0.50`, `Underweight -> watch_reduce/0.65`, `Sell -> watch_exit/0.80`; payloads include `confidence_basis: rating_strength`.
- Malformed, missing, duplicate, or ambiguous portfolio decisions fail closed. Coordinator fixtures and assertions now consume the same validated three-field decision contract without watch/confidence defaults.
- `latest_expected_session()` now uses Asia/Shanghai latest-completed A-share session semantics with a bounded local XSHG/XSHE holiday table and weekend handling. Weekday premarket runs expect the previous trading session; post-close review runs can expect the current trading day.
- Market refresh validates that fetched bars include the expected completed session before recording trusted `local_trading_calendar` proofs.
- Premarket and review schedulers refresh market data for the expected completed session window before running advice/review. The launchd review template now invokes `advisor.scheduler.review`.

## RED Observed

Focused RED failed as expected on JSON-only Markdown decision parsing, missing `confidence_basis` validation, weekday morning/holiday session expectations, premarket refresh using `as_of.date()`, missing composed review scheduler, and launchd review still calling `advisor.reporting.review`.

## Verification

- `.venv311/bin/python -m pytest tests/advisor/test_astock_adapter.py tests/advisor/test_quality_gate.py tests/advisor/test_market_backfill.py tests/advisor/test_launchd.py tests/advisor/test_reporting.py -q` -> `177 passed in 3.04s`
- `.venv311/bin/python -m pytest tests/advisor -q` -> `510 passed in 8.50s`
- `/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs` -> passed; Node tests `134 passed`, embedded advisor pytest `510 passed`
- `PATH=/Users/mac/.local/share/chrome-devtools-mcp/node/bin:$PATH /Users/mac/.local/share/chrome-devtools-mcp/node/bin/npm --prefix frontend run build` -> passed

Browser screenshot QA remained skipped per user instruction.
