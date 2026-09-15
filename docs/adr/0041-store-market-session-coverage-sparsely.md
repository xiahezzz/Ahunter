# Store Market Session Coverage sparsely

A Canonical Daily Bar implies `traded`, and Market Security listing dates imply `not_yet_listed` or `delisted`; these normal outcomes are not duplicated in a coverage table. Only evidenced suspensions are persisted in `market_daily_absences`, while unresolved `source_missing` state lives in the active ingestion item until repaired, preserving complete semantics without adding a second row for every market bar.
