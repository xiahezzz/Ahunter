---
status: accepted
---

# Run one Research Cycle at a time

The Research Service will run at most one Research Cycle across the system at a time, including Cycles belonging to different requests or one Daily Research Batch, while Agent Invocations inside the active Cycle retain the bounded parallelism defined by the Codex Execution Policy. An active Cycle is never preempted, but at each Cycle boundary pending manual work runs in submission order before the remaining scheduled Cycles. Parallel or mid-Cycle-preemptible execution was rejected for the local runtime because Codex, Provider, SQLite, and artifact-store contention would make progress and failure recovery less predictable.
