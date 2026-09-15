---
status: accepted
---

# Separate data products from provider adapters

A Hunter will define each Data Product through a versioned local manifest containing its identity, request and result schemas, time boundary, freshness requirement, conflict tolerance, and retention policy. Agent Manifests depend only on exact Data Product identities and versions. A Hunter-owned Provider Adapters implement a narrow fetch interface and advertise the products they support; ordered adapters may supply the same product without changing its contract or any Agent. Product manifests and adapters are discovered only from fixed repository-owned directories and validated at startup. The Research Engine owns routing and fallback, replacing TradingAgents' global vendor router; repository-external provider plugins are not loaded.
