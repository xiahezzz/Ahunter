# Normalize Market Daily units at provider boundaries

Every Provider Adapter emits Canonical Daily Bars with OHLC prices in CNY per share, volume as an integer number of shares, and turnover amount in CNY, converting provider-specific lot volume by the applicable factor before validation and hashing. Provider units remain provenance metadata and never reach Agents; SQLite keeps prices and amount as `REAL` and volume as `INTEGER` to preserve the existing simple analytical model.
