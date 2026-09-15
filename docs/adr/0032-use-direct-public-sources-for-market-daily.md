---
status: superseded
superseded_by: 0057
---

# Use direct public sources for Market Daily ingestion

This historical decision is superseded by ADR-0057 and is not part of the current runtime.

Shanghai and Shenzhen exchange publications define security membership and listing intervals, Eastmoney HTTP supplies the primary complete Canonical Daily Bar and adjustment-factor observations, and the TDX TCP protocol through direct `tdxpy` usage supplies whole-observation fallback. For factors, the TDX fallback derives one complete series from its own raw bars and corporate-action records; it never mixes fields with Eastmoney. Every selected provider must satisfy the complete product contract independently; field-level source splicing remains forbidden, Sina is removed from the required five-year path, and neither `mootdx` nor TradingAgents participates in ingestion.
