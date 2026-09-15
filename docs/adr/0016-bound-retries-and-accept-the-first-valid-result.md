---
status: accepted
---

# Bound retries and accept the first valid result

A Research Invocation may automatically retry once for a technical failure such as a timeout, process failure, or schema-invalid output. Every Invocation Attempt is retained and uses the same immutable Run Capsule and Codex Execution Policy; the first contract-valid output is accepted immediately, and no additional candidates are generated or ranked. A semantically weak, low-quality, or undesired but contract-valid result is not retryable. If both attempts fail, every dependent Research Team is blocked. Retry limits are shared across Teams and versioned in the Codex Execution Policy. Re-running completed research creates a new Research Cycle rather than replacing or selectively resampling an existing result.
