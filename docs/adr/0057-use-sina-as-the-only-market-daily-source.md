---
status: accepted
supersedes: [0032, 0045]
---

# Use Sina as the only Market Daily source

The live Market Daily path uses one repository-owned Sina adapter for the current Shanghai and Shenzhen A-share universe, raw daily bars, forward-adjustment factors, Shanghai and Shenzhen benchmark sessions, and evidence of missing bars. Beijing Stock Exchange securities, CDRs, Eastmoney, TDX, exchange quote adapters, provider fallback, and data-source credentials are outside this path.

All Sina requests share one process-wide fixed-spacing limiter set to two requests per second. Connection failures, HTTP 429, and HTTP 5xx responses have at most three total attempts; `Retry-After` is honored up to thirty seconds. Exhaustion records `source_missing` and leaves the run partial for a later catch-up instead of switching sources.

Sina's historical daily-bar response does not provide turnover amount, so `amount` is nullable and must never be fabricated as zero or filled from another source. Stock-list pages are fetched in explicit 100-row pages after reading the exact node count so Sina's silent page-size cap cannot truncate the universe.
