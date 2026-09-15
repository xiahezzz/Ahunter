---
status: accepted
---

# Fall back between providers without silently merging data

Data Products may define ordered provider fallbacks, and every attempt and selected source will be recorded before the snapshot is sealed. All providers must satisfy the same product contract; materially conflicting observations block the product unless a product-specific, versioned reconciliation policy exists. Silent averaging, field-level source splicing, and LLM source selection were rejected because they obscure provenance and make research results irreproducible.
