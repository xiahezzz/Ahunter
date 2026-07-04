import { downloadImage } from "./download-image.mjs";

export async function processEventMedia({
  event,
  store,
  mediaRoot,
  fetchImpl = fetch,
  now = () => Date.now(),
}) {
  return drainMediaJobs({ store, mediaRoot, fetchImpl, now, eventId: event.eventId });
}

export async function drainMediaJobs({
  store,
  mediaRoot,
  fetchImpl = fetch,
  now = () => Date.now(),
  eventId,
  maxAttempts = 5,
  maxConcurrent = 3,
  batchSize = 100,
  signal,
}) {
  const limit = Math.max(1, Math.trunc(batchSize));
  let processed = 0;
  for (;;) {
    if (signal?.aborted) break;
    const params = [now(), maxAttempts];
    let sql = `SELECT event_id, rid, source_url, url_hash, attempts
      FROM media_jobs
      WHERE status IN ('pending', 'failed') AND next_attempt_at <= ? AND attempts < ?`;
    if (eventId) { sql += " AND event_id = ?"; params.push(eventId); }
    sql += " ORDER BY next_attempt_at, event_id, url_hash LIMIT ?";
    params.push(limit);
    const jobs = store.database.prepare(sql).all(...params);
    if (jobs.length === 0) break;
    let cursor = 0;
    const workers = Array.from({ length: Math.min(maxConcurrent, jobs.length) }, async () => {
      for (;;) {
        const job = jobs[cursor++];
        if (!job || signal?.aborted) return;
        const attemptedAt = now();
        try {
          const media = await downloadImage({ url: job.source_url, mediaRoot, fetchImpl, signal });
          store.completeMediaJob({
            eventId: job.event_id, rid: job.rid, downloadedAt: attemptedAt, ...media,
          });
        } catch {
          const attempts = job.attempts + 1;
          const backoff = Math.min(60_000 * (2 ** Math.max(0, attempts - 1)), 3_600_000);
          store.database.prepare(`UPDATE media_jobs
            SET status = 'failed', attempts = ?, next_attempt_at = ?, error_code = 'download_failed'
            WHERE event_id = ? AND url_hash = ?`).run(
            attempts, attemptedAt + backoff, job.event_id, job.url_hash,
          );
        }
      }
    });
    await Promise.all(workers);
    processed += jobs.length;
  }
  return processed;
}
