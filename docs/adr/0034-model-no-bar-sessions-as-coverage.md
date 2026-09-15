# Model legitimate no-bar sessions as coverage, not synthetic bars

Market Daily ingestion writes a Canonical Daily Bar only when a security actually traded and distinguishes Market Session Coverage for `traded`, `suspended`, `not_yet_listed`, `delisted`, or unresolved `source_missing` outcomes. Coverage is derived from bars and listing intervals where possible and stored explicitly only for exceptional absences or active task failures; copying a prior close into OHLC with zero volume is forbidden because it would contaminate indicators with invented market observations.
