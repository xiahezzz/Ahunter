---
status: accepted
---

# Use independent reviews in the decision pipeline

The shared Decision Pipeline will replace TradingAgents' alternating conversational debates with bounded independent reviews and a single downstream adjudicator. After deterministic input and Finding quality checks, Bull and Bear stages independently evaluate the same Research Findings in parallel; the Research Manager receives both reviews and produces the investment thesis, followed by the Trader proposal. Aggressive, Neutral, and Conservative risk stages then independently review the same Trader proposal in parallel; the Portfolio Manager receives all three reviews and produces the Team Conclusion, which passes a deterministic publication check. Peer stages do not read or reply to one another, every generative stage remains a separate Codex task with a structured contract, and all Teams use exactly this topology.
