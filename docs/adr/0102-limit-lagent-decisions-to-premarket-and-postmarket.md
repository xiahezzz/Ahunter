---
status: accepted
---

# Limit LAgent decisions to premarket and postmarket windows

The requested experiment measures premarket planning, standing-order execution, and postmarket learning rather than intraday agent reactions. Its current preset permits the main agent and all subagents to research and decide only before 09:30 or after 23:00 in Asia/Shanghai time; those times are configurable experiment values, while the platform executes already submitted simulated instructions between windows without event wake-ups, agent-selected observation times, or new model decisions. This gives up intraday discretionary adaptation to keep the selected decision regime fixed and comparable, while preserving state across windows and requiring executable instructions and evidence-based fills from the environment.
