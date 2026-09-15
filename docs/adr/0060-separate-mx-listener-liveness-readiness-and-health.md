# Separate MX Listener liveness, readiness, and health

The shared service surface reports MX Listener liveness, readiness, and health independently instead of collapsing them into one status: hosting and heartbeat establish liveness, passive CDP attachment establishes readiness, and event-storage, media, and maintenance faults determine health. Activity timestamps and counters remain observations only, so a quiet MX channel never becomes a fabricated disconnect, authorization failure, or health incident.
