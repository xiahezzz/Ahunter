# Submit Market Daily work through SQLite

CLI and the explicitly controlled local WebUI action submit a durable, idempotent Market Daily Request to `advisor.sqlite` and return its identifier immediately; the long-running Market Daily Service is the only process that claims and executes requests. Status commands and ordinary WebUI reads use the same persisted run state, avoiding a new local port or messaging system while preventing trigger processes from racing the service for ingestion ownership.
