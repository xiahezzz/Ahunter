# Run Market Daily as a long-lived service

The Market Daily Service remains running, persists its state in SQLite, executes due work at 21:00, and resumes incomplete work after restart instead of being created only for each scheduled batch. LaunchAgent integration keeps the process alive and restarts it but owns no timer or ingestion behavior, preserving the same service semantics under another host mechanism while aligning Market Daily operations with the project's existing always-on listener capability.
