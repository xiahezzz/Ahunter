# Keep one canonical daily bar instead of a revision system

Each security and trading date has exactly one Canonical Daily Bar: an identical repeat is an idempotent no-op, while different content is recorded as an ingestion conflict and cannot overwrite the existing fact. Replacement requires an explicit repair operation, adjustment factors evolve separately, and no general bar-revision history is built; this deliberately favors a small, predictable model over machinery for rare provider corrections.
