---
status: accepted
---

# Replace external TradingAgents directly

A Hunter will make its self-contained Research Engine the sole research path in one direct change. It will not run old and new implementations side by side, preserve a compatibility layer, introduce a cutover flag, compare outputs in shadow mode, or fall back to external TradingAgents. The external repository path, `sys.path` injection, dynamic imports, upstream graph configuration, fixed upstream role mapping, and obsolete runtime checks will be removed as part of the same implementation. Acceptance depends on the new Engine's automated tests and its first complete report, not behavioral comparison with the old graph.
