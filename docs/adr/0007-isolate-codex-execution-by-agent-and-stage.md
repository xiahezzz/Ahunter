---
status: accepted
---

# Isolate Codex execution by agent and decision stage

Each Research Invocation and each Decision Stage will execute as its own ephemeral Codex task. Research Invocations may run with bounded parallelism, while Decision Stages run sequentially; a single prompt that simulates an entire team was rejected because it prevents independent validation, retry, attribution, and context isolation, and a persistent Codex app-server is deferred while that surface remains experimental.
