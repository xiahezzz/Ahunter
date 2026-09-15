---
status: accepted
---

# Allow unlimited research requests per day

Every intentional WebUI, scheduler, or CLI trigger creates a distinct durable Research Request regardless of whether the same Team and Research Subject have already run on that date. Neither Market nor Security research has a per-day quota or a Team-and-date deduplication key, and every execution remains separately visible in Research History. Transport-level idempotency may collapse retries carrying the same submission identity, but it cannot merge separately initiated research. A scheduler occurrence is only another trigger and does not reserve the Team's sole daily run.
