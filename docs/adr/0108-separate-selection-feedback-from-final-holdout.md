---
status: accepted
---

# Separate selection feedback from the final LAgent holdout

LAgent experiments use tuning feedback to propose candidate versions and a predetermined selection-validation score to promote against the current baseline; repeated validation feedback is part of optimization, even when detailed trajectories stay hidden. A final holdout is evaluated only after candidate selection is frozen and cannot promote a candidate within that same selection process, with all exposure retained so renaming cannot restore an unseen status. This costs additional historical tasks and runs but prevents improvements on the original five-day tuning case from being presented as evidence of performance on unseen periods; the configurable policy is specified in [the implementation design](../research/lagent-experiment-final-design.md).
