---
status: accepted
---

# Use Sina with whole-observation Eastmoney fallback for intraday market data

The Whole-Market Intraday Snapshot will use a repository-owned Sina bulk adapter as its primary public source and a repository-owned Eastmoney bulk adapter as whole-observation fallback. Each adapter must independently satisfy the same universe, field, timestamp, coverage, and quality contract, and a selected snapshot cannot splice securities or fields across them. This source policy is specific to the intraday Data Product and does not alter ADR-0057's Sina-only Market Daily path.
