---
status: accepted
---

# Declare research scope explicitly in Agent and Team manifests

Every Agent Manifest and Team Manifest will pin whether it has Market Research Scope or Security Research Scope, and that Scope remains invariant across every version of one stable Agent or Team ID; changing Scope requires a new stable identity. Every Team member must have exactly the Team's Scope. Cross-version Scope changes and mixed-Scope membership were rejected because latest-version resolution or one Team would otherwise combine incompatible research subjects, snapshots, findings, and conclusion contracts. Inferring Scope from instructions, Data Products, or Team membership was also rejected because market-wide inputs may support security-specific analysis and therefore do not reliably identify what the Agent or Team studies.
