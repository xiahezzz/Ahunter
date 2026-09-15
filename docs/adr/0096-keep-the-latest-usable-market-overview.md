---
status: superseded by ADR-0099
---

# Keep the latest usable Market Overview on the home page

For each Market Team represented on the home page, the Local Operator Console displays its latest published `passed` or `partial` Team Report and separately displays any newer queued or running Research Progress. A later blocked, failed, or cancelled request is disclosed as the latest unsuccessful attempt but does not replace the last usable conclusion. Ordering is based on the report's Research Boundary rather than request completion time, so delayed work cannot silently make the displayed market view move backward.
