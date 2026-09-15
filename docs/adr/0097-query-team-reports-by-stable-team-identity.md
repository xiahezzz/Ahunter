---
status: accepted
---

# Query Team Reports by stable Team identity

The Local Operator Console will provide a dedicated Research Report Explorer backed by server-side pagination rather than a fixed recent-result limit. It defaults to published `passed` and `partial` reports, while a status filter can include `blocked`, `failed`, and `cancelled` Research Records that have no report. Its primary Team filter selects a stable Team identity and returns records across every published version of that Team; an optional secondary filter narrows results to one exact Team version, and every row displays the exact version that actually ran. Research Agent membership is not a report-filter dimension because the published artifact is a Team Conclusion rather than an independent Agent report.
