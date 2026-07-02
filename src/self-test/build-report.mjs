export function buildSelfTestReport({ startedAt, finishedAt, result }) {
  return {
    schemaVersion: 1,
    startedAt,
    finishedAt,
    ok: result.status === 0,
    exitCode: result.status,
  };
}
