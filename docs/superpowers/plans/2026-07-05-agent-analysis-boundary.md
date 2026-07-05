# Agent Analysis Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clarify that collector and extraction-skill restrictions do not prohibit the primary agent from separately analyzing successfully extracted data.

**Architecture:** Keep `agents.md` as the single repository policy source. Separate collector behavior, extraction-skill behavior, and post-extraction primary-agent behavior while retaining every operational safety constraint.

**Tech Stack:** Markdown, Git, ripgrep

## Global Constraints

- Only the user may supply or authorize a RID.
- Collection remains passive and read-only.
- Never request, record, or expose credentials or debugging identifiers.
- Never perform real trading or submit orders.
- Data-quality failures block analytical conclusions until resolved.

---

### Task 1: Clarify repository agent boundaries

**Files:**
- Modify: `agents.md`
- Reference: `docs/superpowers/specs/2026-07-05-agent-analysis-boundary-design.md`

**Interfaces:**
- Consumes: the approved responsibility boundaries in the design specification.
- Produces: repository instructions that permit separate post-extraction analysis without changing collector or extraction-skill safety.

- [ ] **Step 1: Inspect existing conflicting language**

Run:

```bash
rg -n "portfolio advice|reports|data-quality|real trading|analyze-a-hunter-data" agents.md
```

Expected: the opening paragraph contains a blanket portfolio/report prohibition; safety rules contain quality and real-trading restrictions.

- [ ] **Step 2: Modify the opening scope and add the analysis boundary**

Replace the opening paragraph with collector-specific scope. Add an `Agent analysis boundary` section stating:

```markdown
## Agent analysis boundary

The `analyze-a-hunter-data` skill is extraction-only. While that skill is active, follow its boundary and return stored data without analysis or investment judgment.

After extraction is complete, the primary agent may independently analyze successfully extracted data, supplement it with lawful external research, generate analytical reports and simulated scenarios, and provide stock research opinions or recommendations. Do not attribute that downstream work to the extraction skill. Research opinions never authorize real trading or order submission.

If a required data-quality check fails, do not produce downstream analytical conclusions or stock recommendations until the failure is resolved.
```

- [ ] **Step 3: Verify safety and remove conflicts**

Run:

```bash
git diff --check -- agents.md
rg -n "Agent analysis boundary|extraction-only|real trading|Only the user may supply|must never navigate|data-quality" agents.md
```

Expected: no whitespace errors; all retained safety constraints and the new boundary are present; no blanket prohibition prevents separate post-extraction analysis.

- [ ] **Step 4: Run the required offline self-test**

Run:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
```

Expected: exit status 0 and all offline tests pass.

- [ ] **Step 5: Commit the policy change**

```bash
git add agents.md docs/superpowers/plans/2026-07-05-agent-analysis-boundary.md
git commit -m "docs: allow post-extraction agent analysis"
```

Expected: the commit includes only `agents.md` and this implementation plan.
