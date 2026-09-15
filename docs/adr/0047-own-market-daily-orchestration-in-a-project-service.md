# Own Market Daily orchestration in a project service

Security refresh, cold start, gap-aware catch-up, resumability, quality state, and progress belong to one repository-owned Market Daily Service. CLI, Web API, and LaunchAgent integration are thin trigger adapters over that service, with LaunchAgent installation and scheduling treated as one optional host capability rather than the ingestion architecture; no trigger adapter may reimplement provider routing, run transitions, or persistence rules.
