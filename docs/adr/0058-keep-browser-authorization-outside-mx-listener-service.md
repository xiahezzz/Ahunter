# Keep browser authorization outside the MX Listener Service

The MX Listener Service remains continuously hosted and automatically resumes passive collection when an operator-managed, logged-in MX page is available, but it never starts Chrome, navigates or reloads MX, or manages login. When those prerequisites are absent it exposes an observable waiting-for-authorization state, trading unattended browser recovery for explicit user authorization and passive read-only collection.
