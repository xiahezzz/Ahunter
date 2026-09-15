---
status: accepted
---

# Run Codex tasks in isolated read-only capsules

Every Codex-backed Research Invocation and Decision Stage will run ephemerally against a read-only Run Capsule containing only its declared instructions, inputs, evidence, and output schema. User configuration, plugins, MCP tools, repository-wide access, file writes, and web search are excluded; broader Codex access was rejected because it would let agents bypass Data Products, introduce unequal information between teams, and expose unrelated local state.
