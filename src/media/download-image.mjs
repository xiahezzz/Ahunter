import { createHash, randomUUID } from "node:crypto";
import { isIP } from "node:net";
import { chmod, link, mkdir, unlink, writeFile } from "node:fs/promises";
import path from "node:path";

const LIMIT = 10 * 1024 * 1024;
const MAX_REDIRECTS = 5;
const EXTENSIONS = new Map([
  ["image/jpeg", "jpg"],
  ["image/png", "png"],
  ["image/webp", "webp"],
  ["image/gif", "gif"],
  ["image/avif", "avif"],
]);
const BEIJING_DATE_FORMATTER = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

const sha256 = (value) => createHash("sha256").update(value).digest("hex");

function beijingEventDate(eventReceivedAt) {
  if (!Number.isFinite(eventReceivedAt)) {
    throw new TypeError("eventReceivedAt must be a finite Unix timestamp in milliseconds");
  }
  const date = new Date(eventReceivedAt);
  if (Number.isNaN(date.getTime())) {
    throw new TypeError("eventReceivedAt must be a valid Unix timestamp in milliseconds");
  }
  return BEIJING_DATE_FORMATTER.format(date);
}

function isNonGlobalIpv4(hostname) {
  const octets = hostname.split(".").map(Number);
  const value = octets.reduce((total, octet) => (total * 256) + octet, 0) >>> 0;
  const inCidr = (address, bits) => {
    const base = address.split(".").map(Number).reduce((total, octet) => (total * 256) + octet, 0) >>> 0;
    const size = 2 ** (32 - bits);
    return Math.floor(value / size) === Math.floor(base / size);
  };
  if (hostname === "192.0.0.9" || hostname === "192.0.0.10") return false;
  return [
    ["0.0.0.0", 8], ["10.0.0.0", 8], ["100.64.0.0", 10], ["127.0.0.0", 8],
    ["169.254.0.0", 16], ["172.16.0.0", 12], ["192.0.0.0", 24],
    ["192.0.2.0", 24], ["192.88.99.0", 24], ["192.168.0.0", 16],
    ["198.18.0.0", 15], ["198.51.100.0", 24], ["203.0.113.0", 24],
    ["224.0.0.0", 4], ["240.0.0.0", 4],
  ].some(([address, bits]) => inCidr(address, bits));
}

function ipv6Words(hostname) {
  const halves = hostname.split("::");
  if (halves.length > 2) return null;
  const left = halves[0] ? halves[0].split(":") : [];
  const right = halves[1] ? halves[1].split(":") : [];
  const missing = 8 - left.length - right.length;
  if (missing < 0 || (halves.length === 1 && missing !== 0)) return null;
  return [...left, ...Array(missing).fill("0"), ...right].map((word) =>
    Number.parseInt(word, 16),
  );
}

function isNonGlobalIpv6(hostname) {
  const words = ipv6Words(hostname);
  if (!words || words.some(Number.isNaN)) return true;
  const value = words.reduce((total, word) => (total << 16n) | BigInt(word), 0n);
  const inCidr = (address, bits) => {
    const base = ipv6Words(address)
      .reduce((total, word) => (total << 16n) | BigInt(word), 0n);
    return (value >> BigInt(128 - bits)) === (base >> BigInt(128 - bits));
  };
  const ipv4Mapped =
    words.slice(0, 5).every((word) => word === 0) && words[5] === 0xffff;
  if (ipv4Mapped) return true;

  const globalIetfExceptions = [
    ["2001:1::1", 128], ["2001:1::2", 128], ["2001:1::3", 128],
    ["2001:3::", 32], ["2001:4:112::", 48], ["2001:20::", 28],
    ["2001:30::", 28],
  ];
  if (globalIetfExceptions.some(([address, bits]) => inCidr(address, bits))) {
    return false;
  }

  return [
    ["::", 128], ["::1", 128], ["64:ff9b:1::", 48], ["100::", 64],
    ["100:0:0:1::", 64], ["2001::", 23], ["2001:db8::", 32],
    ["2002::", 16], ["3fff::", 20], ["5f00::", 16], ["fc00::", 7],
    ["fe80::", 10], ["ff00::", 8],
  ].some(([address, bits]) => inCidr(address, bits));
}

function validatedUrl(value, context) {
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`${context} is invalid`);
  }
  if (url.protocol !== "https:") throw new Error(`${context} must use HTTPS`);
  if (url.username || url.password) {
    throw new Error(`${context} credentials are not allowed`);
  }

  const hostname = url.hostname
    .toLowerCase()
    .replace(/^\[|\]$/g, "")
    .replace(/\.+$/, "");
  const version = isIP(hostname);
  if (version !== 6) url.hostname = hostname;
  const localName = hostname === "localhost" || hostname.endsWith(".localhost");
  const localAddress =
    (version === 4 && isNonGlobalIpv4(hostname)) ||
    (version === 6 && isNonGlobalIpv6(hostname));
  if (localName || localAddress) throw new Error(`${context} host is not allowed`);
  return url;
}

function timeoutSignal(milliseconds, parent, label) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new Error(`${label} timeout`)), milliseconds);
  timer.unref?.();
  const abort = () => controller.abort(parent.reason ?? new Error("shutdown aborted"));
  parent?.addEventListener("abort", abort, { once: true });
  return { signal: controller.signal, close() { clearTimeout(timer); parent?.removeEventListener("abort", abort); } };
}

function readWithAbort(reader, signal) {
  if (signal.aborted) return Promise.reject(signal.reason);
  return new Promise((resolve, reject) => {
    const abort = () => reject(signal.reason);
    signal.addEventListener("abort", abort, { once: true });
    reader.read().then(resolve, reject).finally(() => signal.removeEventListener("abort", abort));
  });
}

async function fetchImage(source, fetchImpl, { connectTimeoutMs, signal }) {
  let current = source;
  for (let redirects = 0; redirects <= MAX_REDIRECTS; redirects += 1) {
    const timeout = timeoutSignal(connectTimeoutMs, signal, "connect");
    let response;
    try { response = await fetchImpl(current, { redirect: "manual", signal: timeout.signal }); }
    finally { timeout.close(); }
    if (response.status >= 300 && response.status < 400) {
      await response.body?.cancel().catch(() => {});
      const location = response.headers.get("location");
      if (!location) throw new Error("Image redirect is missing a location");
      if (redirects === MAX_REDIRECTS) throw new Error("Image has too many redirects");
      current = validatedUrl(
        new URL(location, current),
        "Image redirect URL",
      );
      continue;
    }

    const finalUrl = response.url
      ? validatedUrl(response.url, "Image redirect URL")
      : current;
    return { response, finalUrl };
  }
  throw new Error("Image has too many redirects");
}

function detectedImageType(bytes) {
  if (
    bytes.length >= 3 &&
    bytes[0] === 0xff &&
    bytes[1] === 0xd8 &&
    bytes[2] === 0xff
  ) return "image/jpeg";
  if (
    bytes.length >= 8 &&
    bytes.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))
  ) return "image/png";
  if (
    bytes.length >= 6 &&
    (bytes.subarray(0, 6).equals(Buffer.from("GIF87a")) ||
      bytes.subarray(0, 6).equals(Buffer.from("GIF89a")))
  ) return "image/gif";
  if (
    bytes.length >= 12 &&
    bytes.subarray(0, 4).equals(Buffer.from("RIFF")) &&
    bytes.subarray(8, 12).equals(Buffer.from("WEBP"))
  ) return "image/webp";
  if (bytes.length >= 16 && bytes.subarray(4, 8).equals(Buffer.from("ftyp"))) {
    const boxSize = bytes.readUInt32BE(0);
    if (boxSize < 16) return null;
    const boxEnd = Math.min(boxSize, bytes.length);
    const majorBrand = bytes.subarray(8, 12).toString("ascii");
    if (majorBrand === "avif" || majorBrand === "avis") return "image/avif";
    for (let offset = 16; offset + 4 <= boxEnd; offset += 4) {
      const brand = bytes.subarray(offset, offset + 4).toString("ascii");
      if (brand === "avif" || brand === "avis") return "image/avif";
    }
  }
  return null;
}

export async function downloadImage({
  url, mediaRoot, eventReceivedAt, fetchImpl = fetch, connectTimeoutMs = 10_000,
  bodyTimeoutMs = 30_000, signal,
}) {
  const eventDate = beijingEventDate(eventReceivedAt);
  const source = validatedUrl(url, "Image URL");
  const { response, finalUrl } = await fetchImage(source, fetchImpl, { connectTimeoutMs, signal });
  if (!response.ok) throw new Error(`Image request failed: ${response.status}`);
  validatedUrl(finalUrl, "Image redirect URL");

  const contentType = response.headers
    .get("content-type")
    ?.split(";")[0]
    .trim()
    .toLowerCase();
  if (!EXTENSIONS.has(contentType)) {
    throw new Error(`Unsupported image content type: ${contentType || "missing"}`);
  }

  const declared = Number(response.headers.get("content-length") || 0);
  if (declared > LIMIT) throw new Error("Image exceeds 10 MiB");
  if (!response.body) throw new Error("Image response has no body");

  const reader = response.body.getReader();
  const bodyTimeout = timeoutSignal(bodyTimeoutMs, signal, "body-read");
  const chunks = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await readWithAbort(reader, bodyTimeout.signal);
      if (done) break;
      size += value.byteLength;
      if (size > LIMIT) {
        await reader.cancel().catch(() => {});
        throw new Error("Image exceeds 10 MiB");
      }
      chunks.push(value);
    }
  } finally {
    if (bodyTimeout.signal.aborted) await reader.cancel(bodyTimeout.signal.reason).catch(() => {});
    bodyTimeout.close();
  }

  const bytes = Buffer.concat(chunks.map((chunk) => Buffer.from(chunk)));
  const detectedType = detectedImageType(bytes);
  if (!detectedType) throw new Error("Unsupported image bytes");
  if (detectedType !== contentType) {
    throw new Error("Image content type does not match image bytes");
  }
  const extension = EXTENSIONS.get(detectedType);
  const contentHash = sha256(bytes);
  const directory = path.join(mediaRoot, eventDate);
  const localPath = path.join(directory, `${contentHash}.${extension}`);
  const temporary = `${localPath}.${randomUUID()}.tmp`;
  await mkdir(directory, { recursive: true, mode: 0o700 });
  await chmod(directory, 0o700);
  await writeFile(temporary, bytes, { flag: "wx", mode: 0o600 });
  try {
    await link(temporary, localPath);
  } catch (error) {
    if (error.code !== "EEXIST") throw error;
  } finally {
    await unlink(temporary).catch(() => {});
  }
  await chmod(localPath, 0o600);

  return {
    sourceUrl: source.href,
    urlHash: sha256(source.href),
    contentHash,
    contentType: detectedType,
    localPath,
  };
}
