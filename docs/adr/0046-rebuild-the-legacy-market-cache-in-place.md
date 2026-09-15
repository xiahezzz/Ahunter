# Rebuild the legacy market cache in place

The existing 1,452 Sina-derived bars for two securities, their source attempts, and their calendar proof are disposable bootstrap data that do not satisfy the new amount, coverage, or history contracts. They will be deleted directly without backup before the first new cold start, while ledger, reports, research artifacts, and all other operational data remain untouched; no compatibility migration for the legacy market cache will be built.
