# MX Listener Operations Manual Design

## Purpose

Create one copy-and-paste operations manual for running the authorized MX WebSocket collector on this Mac. The manual is for routine operation, not software development.

## Audience and scope

The reader operates the collector from Terminal and may not know its internal implementation. The manual covers:

- first-time prerequisites and RID configuration;
- starting the dedicated Chrome debugging profile on port 9333;
- validating Chrome DevTools and the MX page;
- running the offline self-test;
- starting the collector while allowing the display to turn off;
- checking stored events, counters, media, and process state;
- stopping and restarting safely;
- resolving routine connection, authorization, permissions, and no-data conditions;
- identifying the files that must be backed up.

It excludes development workflows, code modification, real trading, legacy page injection, and legacy offline frame decoding.

## Organization

The manual will follow the operating lifecycle:

1. Safety rules and fixed paths
2. First-time setup
3. Daily startup checklist
4. Long-running and display-off operation
5. Viewing recorded information
6. Safe shutdown and restart
7. Routine maintenance and backup
8. Troubleshooting by observed error
9. Compact command index

## Command conventions

- Commands run from `/Users/mac/Documents/Ahunter/a_hunter` unless explicitly stated otherwise.
- Node commands use `/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node` because the system Node is too old.
- The dedicated Chrome profile uses `$HOME/.chrome-mx-debug-profile` and CDP port 9333.
- Collector commands never use `sudo`.
- Local CDP checks use `curl --noproxy '*'` so shell proxy variables cannot redirect loopback traffic.
- SQLite queries are read-only and safe while the collector is writing in WAL mode.
- Every long-running command states how to stop it and what successful behavior looks like.

## Operational behavior documented

The manual will state that a quiet collector terminal is normal: successful accepted messages are stored rather than printed. Connection and ingestion failures are written to stderr. Accepted data is stored in `data/state/events.sqlite`; downloaded media is stored below `data/media/`.

The troubleshooting section will distinguish:

- `Collector connection failed (Error)`: CDP endpoint unavailable or invalid;
- `authorization_required`: the CDP endpoint is reachable but the dedicated Chrome exposes no exact MX page target;
- collector runs but no events arrive: RID mismatch, no inbound MX frames, wrong Chrome profile, expired login, or expected duplicates/rejections;
- root-owned files: prior use of `sudo`, requiring ownership repair before restarting as the normal user.

## Acceptance criteria

- A user can start from a stopped machine and reach a working collector by following the manual in order.
- Commands use port 9333 consistently and do not rely on the unusable 9222 endpoint observed on this Mac.
- The manual shows how to confirm that `/json/version` exposes `webSocketDebuggerUrl` and `/json/list` contains the MX URL.
- The manual shows how to query recent decoded text, ingestion counters, media status, and total event count.
- No command exposes credentials, cookies, tokens, Socket.IO session IDs, or debugging identifiers.
- No command performs page injection, sends a WebSocket business event, or places a trade.
- The document contains no placeholders or unqualified destructive commands.
