# Observe trading sessions instead of hard-coding holidays

The 21:00 job triggers every calendar day and extends local `trading_sessions` only when primary and fallback benchmark observations agree on a completed Shanghai-Shenzhen session; an unchanged latest session is a successful no-op, while an unavailable source is a failure rather than evidence of a holiday. The cold start materializes the preceding five years of sessions, replacing the bounded 2025–2026 holiday constants with persisted market evidence.
