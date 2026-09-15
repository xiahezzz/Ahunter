---
status: superseded by ADR-0076
---

# Publish structured advice without executing trades

Each Team Conclusion will expose a five-level Decision Stance (`watch_buy`, `watch_add`, `hold`, `watch_reduce`, or `watch_exit`), a justified qualitative Decision Conviction (`low`, `medium`, or `high`), deterministic evidence quality, an evidence-linked thesis, key risks, invalidation conditions, time horizon, and optional price-range and position-limit guidance. A Hunter will remove the current fixed mapping from rating strength to pseudo-precise numeric confidence because direction is not probability and model self-assessment is not data quality. Team Conclusions feed reports and later evaluation only: they cannot create orders or mutate the ledger. Any future automated execution requires a separate system and explicit approval boundary.
