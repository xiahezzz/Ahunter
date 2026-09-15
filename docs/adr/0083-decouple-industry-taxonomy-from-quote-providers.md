---
status: accepted
---

# Decouple industry taxonomy from quote providers

Sector-strength research will use a repository-owned, versioned snapshot of Eastmoney first-level industry membership. Each Research Record pins the taxonomy version independently of the Whole-Market Intraday Snapshot and its selected provider. Sina and Eastmoney quote adapters supply observations but do not determine sector membership, so whole-observation fallback cannot silently change sector composition or historical interpretation. The existing unowned `securities.industry` field and provider-specific labels are not authoritative taxonomy sources.
