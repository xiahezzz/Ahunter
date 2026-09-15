---
status: accepted
---

# Use local Codex for generative execution

A Hunter will invoke the installed Codex CLI under the local user's existing ChatGPT session for all generative Research Agent and Decision Stage work, without constructing provider-specific model clients. Codex remains a network-backed model execution surface rather than an offline model; the Research Engine must fail closed when the local Codex session is unavailable and must never inspect, copy, log, or persist its session material.
