---
status: accepted
---

# Isolate research agents from data providers

Research Agents will declare and consume Data Products through the Research Engine instead of calling networks, vendor SDKs, or provider-specific tools directly. The engine owns provider selection, normalization, time-bound validation, caching, provenance, rate limits, and failure handling; exploratory access is permitted only through declared Data Product capabilities and remains recorded in the run's Research Data Snapshot. Direct provider access was rejected because it makes agent behavior non-reproducible and couples every specialist to vendor formats and operations.
