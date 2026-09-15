---
status: accepted
---

# Default pre-Scope manifests to Security Research Scope

Published Agent and Team Manifests that predate the required `scope` declaration will be interpreted permanently as Security Research Scope without rewriting their files; every newly published Manifest must declare Scope explicitly. Inferring legacy Scope from instructions, Data Products, or membership was rejected because those signals are ambiguous, while rewriting already-used Manifests would violate immutable historical definitions and break reproducibility.
