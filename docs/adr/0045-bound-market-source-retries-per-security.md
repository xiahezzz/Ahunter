---
status: superseded
superseded_by: 0057
---

# Bound market-source retries per security

This historical primary/fallback retry topology is superseded by ADR-0057. Bounded retries remain, but only against Sina and without fallback.

For each Market Security, an ingestion attempt calls the primary provider at most twice and then the fallback provider once; if all calls fail, the item becomes `source_missing` and the run continues with other securities. The run finishes as `partial` rather than retrying indefinitely, and Market Daily Catch-up retries the unresolved interval on the next explicit or 21:00 execution without refetching completed work.
