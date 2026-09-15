---
status: accepted
---

# Store research artifacts by content

A Hunter will use SQLite as the research control plane for lifecycle state, versions, provenance, quality results, content hashes, and artifact indexes, while a local Research Artifact Store holds immutable, content-addressed Data Products, Run Capsules, Research Findings, and Team Conclusions. Reports reference exact artifacts by content hash rather than duplicating or mutating them. Raw provider material is retained only when the source's terms and versioned retention policy permit it; otherwise A Hunter records the source locator, retrieval time, content hash, and any permitted normalized fields or summary. Expired or unavailable source material remains explicitly marked as unavailable so the system does not claim full reproducibility. Runtime artifacts stay outside Git, and neither store may contain Codex session material.
