# Materialize scoped feed unions and expose Agent-specific views

For one Research Cycle, the Research Engine materializes the union of exact MX RID Feeds required by all selected Agent versions once, seals every Feed as a separately hashed Snapshot input, and exposes to each Invocation only the subset declared by that Agent's Data Access. A missing or deauthorized Feed blocks only the requiring Agent and its dependent Teams; it does not block Agents using other Feeds, leak the union through the query interface, or cause the same Feed to be materialized differently across Teams.
