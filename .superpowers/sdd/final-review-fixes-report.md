# Final Whole-Branch Review Fixes

## Scope

Fixed all Critical and Important findings from `.superpowers/sdd/review-56e4849..e37838f.diff`, plus the practical Minor launchd log-directory fix.

## Fixes

- Portfolio-manager publication now requires one structured decision contract: `payload.decision.action` in `buy/watch/hold/reduce/exit/avoid` and numeric `confidence` in `[0, 1]`. Missing, malformed, duplicate, or ambiguous portfolio decisions fail closed and publish only a failure archive.
- Coordinator no longer defaults unparseable analyst decisions to `watch`/`0.5`; it consumes only the validated portfolio-manager decision contract.
- Market refresh now records trusted bounded `local_trading_calendar` proofs through `record_trading_calendar_proof`, and premarket quality can satisfy the calendar check without test-only proof seeding.
- Added `advisor.scheduler.premarket`, a composed scheduled command that reads the passive collector snapshot, expands candidates, refreshes free market data, records calendar proof through backfill, then runs premarket advice.
- Launchd premarket template now invokes the composed scheduler command; launchd rendering creates output and log directories.
- Dev dependency typo fixed from `httpx2` to `httpx`.
- TradingAgents runtime configuration now supports validated `trading_agents.repository_path`, `upstream_provider`, and `upstream_model`, preserving `/Users/mac/Documents/TradingAgents-astock` as the default.
- Premarket/review CLI blocked archives now return exit `0`; runtime and malformed failures remain nonzero.

## RED Observed

Initial focused RED run failed as expected on portfolio decision validation, TradingAgents config, market refresh calendar proof, scheduled premarket module, CLI blocked exit status, launchd render directories, launchd premarket command, and `httpx` dependency spelling.

## Verification

- `.venv311/bin/python -m pytest tests/advisor/test_astock_adapter.py tests/advisor/test_config.py tests/advisor/test_market_backfill.py tests/advisor/test_reporting.py tests/advisor/test_launchd.py tests/advisor/test_web_api.py -q` -> `226 passed in 4.70s`
- `.venv311/bin/python -m pytest tests/advisor -q` -> `493 passed in 8.44s`
- `/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs` -> passed; Node tests `134 passed`, embedded advisor pytest `493 passed`
- `PATH=/Users/mac/.local/share/chrome-devtools-mcp/node/bin:$PATH /Users/mac/.local/share/chrome-devtools-mcp/node/bin/npm --prefix frontend run build` -> passed
- `git diff --check` -> passed

Browser screenshot QA remained skipped per user instruction.
