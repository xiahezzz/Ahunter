---
status: accepted
---

# Share Team-agnostic Agent findings within a Cycle

Within one Research Cycle, the Research Engine will execute each unique Research Invocation once and share its immutable Research Finding with every Team that includes that Agent. Invocation identity is derived from the exact Agent version, subject, time boundary, declared input artifact hashes, and Codex Execution Policy version. Agents receive no Team identity or style context, so Team differences remain attributable only to Agent membership rather than generative resampling. Failure blocks every Team dependent on that Invocation while unrelated Teams continue. A genuinely different specialist perspective must be represented by a distinct Agent identity and version, not by running the same Agent differently for each Team.
