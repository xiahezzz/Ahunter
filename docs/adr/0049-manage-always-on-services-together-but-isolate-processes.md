# Manage always-on services together but isolate their processes

The Node-based MX Listener Service and Python-based Market Daily Service form one A Hunter Service Set with common install, start, stop, health, log, and WebUI status capabilities, but they remain separate processes with separate databases and restart boundaries. LaunchAgent integration hosts each independently so Chrome or listener failures cannot interrupt market ingestion and market-provider failures cannot interrupt authorized event collection.
