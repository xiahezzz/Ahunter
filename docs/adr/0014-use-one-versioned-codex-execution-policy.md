---
status: accepted
---

# Use one versioned Codex execution policy

A Hunter will own one versioned Codex Execution Policy that explicitly maps each Research Invocation and Decision Stage class to its model, reasoning, timeout, concurrency, and invocation settings. The mapping may differ by task class, but it applies identically to every Research Team and cannot be overridden by an Agent Manifest, Team Manifest, or user-default Codex configuration. Every attempt records the policy version, Codex CLI version, model, effective reasoning settings, timing, and usage metadata. An unavailable configured model blocks the affected execution instead of silently falling back; changing a model or another conclusion-affecting execution setting creates a new policy version.
