# Use six-digit codes as Market Security identity

Because Market Daily explicitly covers only Shanghai and Shenzhen common stocks, the exchange-assigned six-digit code is the canonical Market Security identifier and remains the foreign key used by `market_daily`. A small `securities` table records exchange, name, listing interval, status, source, and fetch time; surrogate IDs, symbol aliases, and code-mapping tables are intentionally omitted.
