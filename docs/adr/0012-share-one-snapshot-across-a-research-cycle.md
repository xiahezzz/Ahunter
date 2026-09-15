---
status: accepted
---

# Share one sealed data snapshot across a research cycle

All selected Research Teams evaluating the same subject and time boundary will run within one Research Cycle. The engine materializes the union of their declared Data Products once, seals a Research Data Snapshot, and gives each Research Invocation only its declared subset; teams may be blocked independently by missing products, but overlapping products cannot differ between teams. Per-team live fetching and in-place snapshot refresh were rejected because they would confound investment-style differences with timing and source drift.
