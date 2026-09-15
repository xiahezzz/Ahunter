---
status: accepted
---

# Own the research engine inside A Hunter

A Hunter will own its research workflow, Agent definitions, contracts, quality controls, research-history artifacts, and Data Product interfaces. It may continue using general-purpose libraries for schemas, HTTP, persistence, and rendering, while generative execution goes through the local Codex CLI and orchestration remains in the A Hunter-owned state machine. Embedding the external TradingAgents package was rejected because it would preserve its fixed graph and global data-routing coupling.
