---
status: superseded by ADR-0080
---

# Pin a manual research boundary when its request is accepted

A manually requested Research Cycle will use the backend's request-acceptance time as its immutable Research Boundary, rather than the scheduled 08:30 boundary, a browser-supplied time, or the eventual execution-start time. Later execution and retries must preserve that cutoff so queueing cannot silently change the evidence set or make the research intent irreproducible.
