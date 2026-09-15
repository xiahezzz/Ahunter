---
status: accepted
---

# Publish Teams as repository-owned manifests

The WebUI will publish each Team version as a new immutable YAML file under `config/research/teams/`, using the repository catalog as the only source of Team definitions. SQLite will record Team use and run state but will not hold a second Team-definition registry; changing a published Team therefore creates a new manifest version instead of updating an existing file.
