---
status: accepted
---

# Fetch whole-market intraday snapshots in bulk

Each Whole-Market Snapshot Provider must obtain one coherent bulk market observation rather than fan out thousands of per-security quote calls. Provider fallback replaces the complete observation under the same product contract, following ADR-0013; per-security or per-field source splicing is rejected because it would stretch observation time, obscure coverage, and make breadth and sector comparisons internally inconsistent.
