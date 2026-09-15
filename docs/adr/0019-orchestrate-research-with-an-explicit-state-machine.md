---
status: accepted
---

# Orchestrate research with an explicit state machine

The new Research Engine will use an explicit A Hunter-owned Research Cycle State Machine rather than LangGraph. It materializes and seals the shared Research Data Snapshot, runs required Research Agents with bounded Python concurrency, executes the common Decision Pipeline independently for each eligible Team, and persists publication artifacts at explicit stage boundaries. SQLite holds durable lifecycle state, while restart resumes only from completed idempotent boundaries and never attempts to resume a live Codex process. TradingAgents graph and state-routing code will not be used; useful specialist instructions, domain rules, and data contracts may be incorporated into A Hunter-owned definitions.
