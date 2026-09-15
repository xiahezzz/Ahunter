---
status: accepted
---

# Use only public A-share data providers

The initial Provider Adapter catalog will contain only repository-owned adapters for public A-share endpoints and existing A Hunter-normalized evidence that require no project-specific access configuration. The catalog excludes Alpha Vantage and the old provider-specific model clients and data routes. Provider configuration is limited to operational settings such as endpoints, rate limits, freshness, fallback order, and retention. A future source that does not fit this boundary requires a separate architecture decision rather than expanding the current provider contract.
