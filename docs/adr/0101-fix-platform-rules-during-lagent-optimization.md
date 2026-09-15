---
status: accepted
---

# Fix platform rules during LAgent optimization

LAgent optimization must distinguish improvement in the agent from changes to the experiment that measures it. The outer optimizer may change only the inner LAgent implementation and explicitly tunable configuration; platform rules, including budgets, model constraints, data visibility, execution isolation, simulated fills, scoring, and audit requirements, remain read-only to both loops within an experiment. This limits autonomous repair of the evaluation environment but preserves meaningful comparisons; an operator changing those rules creates a new experiment version and reruns its baseline.
