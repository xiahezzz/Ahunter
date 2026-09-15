---
status: accepted
---

# Ban implicit memory and query only sealed data

Research Agents will not retain, update, or automatically receive private long-term memory, prior reflections, or mutable `past_context`. Historical market or research material is available only when materialized as an explicit, versioned, time-bounded Data Product in the current Research Data Snapshot. Within a Run Capsule, an Agent may use a bounded local Research Query Interface to filter, search, or aggregate only the Data Products declared by its Manifest. The Engine records every query, parameters, result hash, and query count; the interface cannot write memory, inspect the unrestricted Research Artifact Store, reach the network, or fetch missing data after the Snapshot is sealed. Missing evidence is reported for a future Cycle. Learning from reviews occurs through reviewed Agent Manifest or Decision Pipeline version changes rather than hidden state mutation.
