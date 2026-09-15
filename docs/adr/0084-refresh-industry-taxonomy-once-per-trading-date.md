---
status: accepted
---

# Refresh industry taxonomy once per trading date

When the first Market Research requiring sector membership begins on a Shanghai-Shenzhen trading date, the Research Service will attempt to seal a new Industry Sector Taxonomy version. Every later Research Record on that trading date reuses and pins the successfully sealed version rather than refetching membership per run. This bounds external requests and prevents sector composition from changing between same-day reports; the separate taxonomy-readiness policy determines what happens when the refresh cannot be sealed.
