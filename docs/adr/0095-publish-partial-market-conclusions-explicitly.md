---
status: accepted
---

# Publish partial Market Conclusions explicitly

A Market Research Run publishes a `partial` Team Report when at least one contracted Market Insight passes and at least one is blocked by unavailable or insufficient evidence or by an isolated Agent timeout or schema-invalid result. The report identifies every blocked Insight and its bounded cause class rather than silently omitting it or exposing internal logs. A Run is `passed` only when all contracted Insights pass; it is `blocked` without a report when none is publishable or the overall publication-quality gate fails. The `failed` status is reserved for orchestration, persistence, or publication failure that prevents a coherent result from being safely completed. This supersedes ADR-0008's universal all-or-nothing rule: that completeness rule remains applicable to indivisible Security Team Conclusions, while the typed composition of a Market Team Conclusion permits explicit partial validity.
