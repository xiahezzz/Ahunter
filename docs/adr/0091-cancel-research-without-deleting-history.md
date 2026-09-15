---
status: accepted
---

# Cancel research without deleting history

The Local Operator Console may cancel a queued or running Research Request. A queued Request enters the terminal `cancelled` state immediately; a running Request records cancellation intent and the Research Service stops it at a safe execution boundary, including bounded cancellation of an active Codex Invocation where supported. Cancellation never deletes the Research Record or already persisted audit artifacts, and passed, blocked, failed, or already cancelled records cannot be cancelled again. Deletion was rejected because it would make queue activity and consumed work disappear from Research History.
