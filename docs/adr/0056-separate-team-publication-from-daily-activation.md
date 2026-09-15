---
status: accepted
---

# Separate Team publication from daily activation

Publishing a Team only adds an immutable version to the local catalog and never schedules it automatically. The WebUI provides a separate `每日启用` action that changes the exact published Team versions used by scheduled Daily Research Batches, preventing a catalog change from unexpectedly adding local Codex work. The Daily Team Set contains at most one version of each stable Team ID, so enabling a newer version atomically replaces its older version without deleting history. The set may be empty; in that state the scheduled batch records an explicit skip without materializing data or invoking Codex, while services and manual research remain available.
