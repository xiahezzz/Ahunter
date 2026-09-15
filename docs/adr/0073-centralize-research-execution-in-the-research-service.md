---
status: accepted
---

# Centralize all research execution in the Research Service

The WebUI, 08:30 scheduler, and CLI will act only as Research Trigger Adapters that submit durable, origin-labelled Research Requests; the Research Service will be the only process allowed to execute Research Cycles and Daily Research Batches. Keeping direct scheduler or CLI execution was rejected because separate executors could race for Codex and local storage, bypass one another's queue, and fragment lifecycle status and Research History.
