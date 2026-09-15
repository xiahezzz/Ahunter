# Limit the first Service Set rollout to Market Daily

This delivery fully implements and hosts the Market Daily Service and introduces the shared A Hunter service-management and status surface, but integrates the existing MX Listener Service as status-only. Its manual Chrome, login, RID authorization, and collector startup procedure remains unchanged until a separate change defines safe automatic recovery, keeping the Market Daily cold start from expanding into listener lifecycle work.
