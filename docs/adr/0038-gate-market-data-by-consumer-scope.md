# Gate Market Daily readiness by consumer scope

The cold start and every whole-universe scan require a `complete` Market Daily Ingestion Run, but a single-security Research Run may proceed whenever that security's own required window has complete Market Session Coverage. A daily run with unresolved securities remains visibly `partial` without blocking unrelated single-security research, preventing one source failure from disabling the entire advisor while preserving strict completeness for rankings and market-wide selection.
