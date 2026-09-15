# Store canonical daily bars unadjusted

`market_daily` stores only immutable unadjusted OHLCV and turnover values, while adjustment factors are stored independently; forward- and backward-adjusted series are derived only when queried. This preserves auditable market facts and prevents later corporate actions from repeatedly rewriting the canonical price history.
