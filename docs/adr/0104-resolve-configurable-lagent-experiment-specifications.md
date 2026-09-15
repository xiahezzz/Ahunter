---
status: accepted
---

# Resolve configurable LAgent experiment specifications

The initial LAgent case must not turn its capital, dates, five-session duration, decision times, or other numeric settings into platform constants. Each owning module exposes its meaningful numeric settings, defaults, units, and validation through configuration, and experiment resolves them with versioned market rules into an immutable specification before comparable runs begin. This adds configuration validation and provenance work but permits arbitrary supported test periods and parameter choices without code changes, while preserving fixed conditions within each comparison and the single-main-agent mode contract.
