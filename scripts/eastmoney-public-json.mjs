#!/usr/bin/env node

// Fixed-host, credential-free transport used by Research providers.  It is
// intentionally not a generic URL fetcher.
const ENDPOINTS = new Set([
  "https://push2delay.eastmoney.com/api/qt/clist/get",
]);
const MAX_INPUT_BYTES = 512 * 1024;
const MAX_RESPONSE_BYTES = 4 * 1024 * 1024;
const MAX_OUTPUT_BYTES = 64 * 1024 * 1024;

function fail(code) {
  process.stderr.write(`${code}\n`);
  process.exitCode = 1;
}

function boundedInteger(value, minimum, maximum, fallback) {
  return Number.isInteger(value) && value >= minimum && value <= maximum ? value : fallback;
}

function validateParams(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("invalid_params");
  }
  const entries = Object.entries(value);
  if (entries.length < 1 || entries.length > 30) {
    throw new Error("invalid_params");
  }
  const result = {};
  for (const [key, item] of entries) {
    if (!key || key.length > 80) {
      throw new Error("invalid_params");
    }
    if (
      !(
        typeof item === "string" && item.length <= 2000 ||
        typeof item === "boolean" ||
        typeof item === "number" && Number.isFinite(item)
      )
    ) {
      throw new Error("invalid_params");
    }
    result[key] = String(item);
  }
  return result;
}

function retryAfterMilliseconds(response) {
  const raw = response.headers.get("retry-after");
  if (!raw) return 0;
  const seconds = Number(raw);
  if (Number.isFinite(seconds) && seconds >= 0) return Math.min(5000, seconds * 1000);
  const instant = Date.parse(raw);
  return Number.isFinite(instant) ? Math.min(5000, Math.max(0, instant - Date.now())) : 0;
}

async function main() {
  let input = "";
  process.stdin.setEncoding("utf8");
  for await (const chunk of process.stdin) {
    input += chunk;
    if (Buffer.byteLength(input, "utf8") > MAX_INPUT_BYTES) throw new Error("input_too_large");
  }
  const envelope = JSON.parse(input);
  if (envelope === null || typeof envelope !== "object" || Array.isArray(envelope)) {
    throw new Error("invalid_envelope");
  }
  if (!Array.isArray(envelope.requests) || envelope.requests.length < 1 || envelope.requests.length > 200) {
    throw new Error("invalid_batch");
  }
  const timeoutMs = boundedInteger(envelope.timeout_ms, 1, 30000, 10000);
  const maxAttempts = boundedInteger(envelope.max_attempts, 1, 3, 3);
  const minimumIntervalMs = boundedInteger(envelope.minimum_interval_ms, 500, 5000, 500);
  const requests = envelope.requests.map((request) => validateParams(request?.params));
  if (typeof envelope.endpoint !== "string" || !ENDPOINTS.has(envelope.endpoint)) {
    throw new Error("invalid_endpoint");
  }
  const responses = [];
  let lastStartedAt = 0;
  for (const params of requests) {
    let accepted;
    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      const intervalWait = Math.max(0, minimumIntervalMs - (Date.now() - lastStartedAt));
      if (intervalWait) await new Promise((resolve) => setTimeout(resolve, intervalWait));
      lastStartedAt = Date.now();
      const url = new URL(envelope.endpoint);
      for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);
      try {
        const response = await fetch(url, {
          method: "GET",
          redirect: "error",
          signal: AbortSignal.timeout(timeoutMs),
          headers: {
            Accept: "application/json, text/plain, */*",
            Referer: "https://data.eastmoney.com/bkzj/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
          },
        });
        const retryable = response.status === 429 || response.status >= 500;
        if (!response.ok) {
          if (!retryable || attempt === maxAttempts) throw new Error("http_unavailable");
          const wait = Math.max(retryAfterMilliseconds(response), Math.min(2000, 200 * (2 ** (attempt - 1))));
          await response.body?.cancel();
          await new Promise((resolve) => setTimeout(resolve, wait));
          continue;
        }
        const body = await response.text();
        if (Buffer.byteLength(body, "utf8") > MAX_RESPONSE_BYTES) throw new Error("response_too_large");
        accepted = JSON.parse(body);
        break;
      } catch (error) {
        if (attempt === maxAttempts || error?.message === "http_unavailable" || error?.message === "response_too_large") {
          throw error;
        }
        await new Promise((resolve) => setTimeout(resolve, Math.min(2000, 200 * (2 ** (attempt - 1)))));
      }
    }
    if (accepted === undefined) throw new Error("missing_response");
    responses.push(accepted);
  }
  const output = JSON.stringify({ responses });
  if (Buffer.byteLength(output, "utf8") > MAX_OUTPUT_BYTES) throw new Error("output_too_large");
  process.stdout.write(output);
}

try {
  await main();
} catch (error) {
  fail(error instanceof SyntaxError ? "invalid_json" : "eastmoney_unavailable");
}
