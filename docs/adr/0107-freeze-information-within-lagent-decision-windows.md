---
status: accepted
---

# Freeze information within LAgent decision windows

Historical LAgent comparisons use a fixed information snapshot for each scheduled decision phase, with actual model latency, retries, and resource usage recorded separately rather than advancing simulated market time. The premarket window now contains a pre-auction phase for submitting orders using earlier information and a post-auction phase that includes the final result only after verified publication plus confirmed, usable sale proceeds; all plans reach the simulated platform before 09:30, and post-auction orders cannot participate retroactively in the completed auction. This makes information conditions comparable across candidate versions but does not establish that their research could finish within the corresponding live-market interval, and all concrete times remain configurable experiment values.
