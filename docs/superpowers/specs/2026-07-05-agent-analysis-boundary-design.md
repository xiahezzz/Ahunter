# Agent analysis boundary design

## Goal

Clarify that Phase 1 restrictions govern the passive MX collector and the
`analyze-a-hunter-data` extraction skill, not every later action performed by
the primary agent. After a successful read-only extraction, the primary agent
may independently analyze the extracted data, supplement it with other lawful
research, produce reports, and provide stock research opinions.

## Responsibility boundaries

### Collector

The collector remains passive and read-only. It records only user-authorized
RIDs and must not navigate or operate the MX page, infer RIDs, expose secrets,
place trades, or submit orders.

### Extraction skill

The `analyze-a-hunter-data` skill remains an extraction-only capability. While
using that skill, the agent may retrieve, filter, join, count, export, and copy
stored data only as permitted by the skill. The skill does not analyze,
interpret, summarize, classify, OCR, or make investment judgments.

### Primary agent after extraction

Once extraction is complete, the primary agent may use the extracted data in a
separate analysis step. This includes content interpretation, external research,
evidence synthesis, analytical reports, simulated scenarios, and stock research
opinions or recommendations. These actions must not be represented as work done
by the extraction skill.

Research opinions do not authorize real trading. The agent must not connect to
a broker, place or simulate an actual order through a trading system, or claim
that an opinion guarantees returns.

## Safety constraints retained

- Only the user may supply or authorize a RID.
- Credentials, cookies, tokens, Socket.IO session IDs, and Chrome debugging
  identifiers must never be requested, recorded, or exposed.
- Routine collection remains passive and uses Chrome DevTools Network events
  without navigating, reloading, clicking, typing, or injecting into the page.
- Empty RID configuration continues to fail closed.
- Required quarantine, self-test, smoke-test, and recovery procedures remain
  unchanged.
- Real trading and order submission remain prohibited.
- A data-quality failure blocks downstream analytical conclusions and stock
  recommendations until the failure is resolved.

## AGENTS.md changes

1. Rewrite the opening Phase 1 paragraph so its implementation exclusions apply
   to the collector rather than the primary agent's later analysis.
2. Add an `Agent analysis boundary` section that separates extraction-skill
   behavior from post-extraction primary-agent behavior.
3. Retain all collector safety rules and exact operating commands.
4. Keep the data-quality gate, but phrase it as a temporary block on downstream
   conclusions rather than a permanent prohibition on reports.

## Verification

- Inspect the final diff to confirm collector safety rules are unchanged.
- Search `AGENTS.md` for conflicting blanket prohibitions on analysis, reports,
  or portfolio advice.
- Confirm the file still explicitly prohibits real trading, secret exposure,
  inferred RIDs, active page operation, and conclusions after quality failures.
