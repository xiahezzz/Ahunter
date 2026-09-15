# Add observable LAgent free research

Date: 2026-09-08

The owner needs a free research mode in which a single main investigator chooses
data and delegates research without publishing a fixed Team or using its decision
pipeline. Add `lagent` beside the existing `team` request/record mode.

The engine owns a durable logical conversation. Each isolated Codex invocation
returns the next typed action: data, query, delegate, or finish. The host executes
data and queries through the existing product quality and CapsuleQuery interfaces.
Only the main investigator delegates; child roles and tasks are dynamically authored,
with no catalog membership requirement. Children execute sequentially. The main
investigator receives each result, chooses its next action, and owns final publication.

Settings revisions live in SQLite with optimistic concurrency. Each request pins
instructions, role model overrides, data catalog versions, authorized MX RID subset,
prefetch policy and execution limits at acceptance. Inherited execution policy resolves
at start and remains pinned through recovery. Reruns copy the LAgent configuration and
existing model policy while obtaining a new boundary. Limits default to unlimited except
the inherited executor's bounded invocation timeout and retry policy. Limits do not turn
off durable observations or data quality checks.

Append-only events record main/child identity, public tasks and action descriptions,
configuration, capsule/input/result hashes, provider provenance, query parameters,
model settings, durations, attempts and token usage. Raw Codex events and private
reasoning are not persisted. Trace pagination uses a monotonic sequence cursor.
Completed actions and sealed product artifacts replay after recovery. Incomplete
external calls may repeat after a crash. Only the current queue claim may write events
or publish; cancellation and lease loss propagate to model subprocesses.

Live intraday data is optionally sealed before the first model invocation so model
latency cannot silently change its evidence cutoff. An unavailable optional prefetch
blocks only when selected. A selected data or query quality failure blocks publication.
Final reports require known evidence hashes and Chinese prose.

The existing queue's non-null `team_ref` remains a compatibility execution identity
(`lagent@N`) for this mode. Actual mode is determined by the associated LAgent request
row, never inferred from that string. No Team manifest is created or executed.

WebUI and CLI expose settings, launch, traces, cancellation, history and report access.
Observability is mandatory; only research behavior and limits are configurable.
