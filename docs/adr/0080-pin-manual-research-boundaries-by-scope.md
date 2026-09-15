---
status: accepted
---

# Pin manual research boundaries according to Scope

A Manual Security Research Request fixes its Research Boundary when the backend accepts it because its time-bounded evidence can be reconstructed later. A Manual Market Research Request records acceptance separately, then fixes its Boundary when the Research Service begins execution and seals a one-shot Whole-Market Intraday Snapshot. This supersedes ADR-0071's universal acceptance-time rule: without a continuous intraday collector or a historical intraday source, a queued Market request cannot reconstruct the accepted-time market, while browser-supplied or undisclosed moving boundaries remain rejected.
