---
status: accepted
---

# Extend through research agents, not arbitrary workflow nodes

New research capabilities will enter the Research Engine as independent Research Agents with a common finding contract, while synthesis, trading proposals, risk review, and final adjudication remain engine-owned Decision Stages. This keeps the extension seam narrow and makes adding a specialist independent of workflow routing, persistence, and publication policy; exposing arbitrary graph nodes was rejected because it would force every extension to understand shared state and orchestration rules.
