---
status: accepted
---

# Keep the operator console local-only

A Hunter's WebUI and its API will remain bound to loopback and will reject cross-origin mutation requests; they will never be exposed as a LAN or Internet service. Remote access and an authentication system are deliberately excluded, trading remote convenience for keeping repository-backed research configuration and operational controls inside the machine owner's boundary.
