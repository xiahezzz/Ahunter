---
status: accepted
---

# Discover agents and teams from repository-owned manifests

The Research Engine will discover Agent Manifests and Team Manifests from fixed, version-controlled catalog directories inside A Hunter, validate exact IDs and versions at startup, and reject missing, duplicate, or path-escaping references. Declarative agents require no central Python registration change, code-backed agents may reference only implementations owned by A Hunter, and external entry points or plugins are excluded to preserve self-containment and a reviewable research inventory.
