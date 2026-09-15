---
status: accepted
---

# Create three initial Market Agents

The first Market Research capability will publish `market_breadth@1` for market breadth, trading sentiment, and liquidity; `sector_rotation@1` for first-level industry strength and rotation; and `market_macro_policy@1` for macroeconomic, policy, and market information. The initial Team `a_share_market_overview@1` contains exactly those three Market-scoped Agent versions. None may emit Research Candidates or per-security conclusions. Reusing the existing Security-scoped `market@1` or `policy@1` identities was rejected because Research Scope is invariant across a stable Agent identity.
