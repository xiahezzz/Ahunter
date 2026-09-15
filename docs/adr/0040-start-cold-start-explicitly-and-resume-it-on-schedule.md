# Start the cold start explicitly and resume it on schedule

The first Market Daily Cold Start is created only by an explicit operator intent. Initially that intent was submitted by `advisor-market-daily cold-start`; ADR-0052 additionally permits the local WebUI control to submit the same idempotent intent. API and WebUI startup never initiate a whole-market fetch. Once created, rerunning either control adapter or reaching the 21:00 schedule resumes the same incomplete run, and the scheduler automatically performs normal Market Daily Catch-up after the cold start is sealed.
