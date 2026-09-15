# Keep one file as the RID authorization authority

`config/allowed-rids.yaml` remains the sole authority for the RID Authorization Set consumed by both the MX Listener and Research Engine. The WebUI edits that file only through a narrow backend interface that validates a bounded unique set of positive integers, requires the caller's observed configuration version, and atomically replaces the file; it neither discovers RIDs nor mirrors authorization into the Advisor database, preventing divergent authorization truth across the Node and Python runtimes.
