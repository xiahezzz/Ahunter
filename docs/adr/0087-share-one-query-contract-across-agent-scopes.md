---
status: accepted
---

# Share one query contract across Agent scopes

Market Agents and Security Agents will use the same Run Capsule, Agent Data Access, Research Query Interface, query-budget, read-only isolation, and query-audit rules. A Market Agent chooses its own filters and aggregations over its declared whole-market Data Products, while the interface executes and records those requests without choosing the analytical method. Scope-appropriate query operations may be added through the versioned shared interface, but Market Agents do not receive a separate unrestricted analytics runtime, live provider access, or relaxed audit policy.
