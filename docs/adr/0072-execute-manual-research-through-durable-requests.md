---
status: accepted
---

# Execute manual research through durable requests

The local WebUI will submit a durable Manual Research Request to SQLite and return its identifier immediately; the independently hosted Research Service is the only process that may claim and execute it. Running a Research Cycle synchronously inside the HTTP request or launching an untracked API background process was rejected because browser, API, and machine restarts could otherwise lose work, duplicate long Codex execution, or leave its true state unknowable.
