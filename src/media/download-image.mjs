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

const sha256 = (value) => createHash("sha256").update(value).digest("hex");

function isPrivateIpv4(hostname) {
  const [a, b] = hostname.split(".").map(Number);
  return (
    a === 10 ||
    a === 127 ||
    (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && b === 168) ||
    (a === 169 && b === 254)
  );
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

function isPrivateIpv6(hostname) {
  const words = ipv6Words(hostname);
  if (!words || words.some(Number.isNaN)) return true;
  const loopback =
    words.slice(0, 7).every((word) => word === 0) && words[7] === 1;
  const uniqueLocal = (words[0] & 0xfe00) === 0xfc00;
  const linkLocal = (words[0] & 0xffc0) === 0xfe80;
  const ipv4Mapped =
    words.slice(0, 5).every((word) => word === 0) && words[5] === 0xffff;
  if (ipv4Mapped) {
    const mapped =
      `${words[6] >> 8}.${words[6] & 0xff}.` +
      `${words[7] >> 8}.${words[7] & 0xff}`;
    return isPrivateIpv4(mapped);
  }
  return loopback || uniqueLocal || linkLocal;
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

  const hostname = url.hostname.toLowerCase().replace(/^\[|\]$/g, "");
  const localName = hostname === "localhost" || hostname.endsWith(".localhost");
  const version = isIP(hostname);
  const localAddress =
    (version === 4 && isPrivateIpv4(hostname)) ||
    (version === 6 && isPrivateIpv6(hostname));
  if (localName || localAddress) throw new Error(`${context} host is not allowed`);
  return url;
}

async function fetchImage(source, fetchImpl) {
  let current = source;
  for (let redirects = 0; redirects <= MAX_REDIRECTS; redirects += 1) {
    const response = await fetchImpl(current, { redirect: "manual" });
    if (response.status >= 300 && response.status < 400) {
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

export async function downloadImage({ url, mediaRoot, fetchImpl = fetch }) {
  const source = validatedUrl(url, "Image URL");
  const { response, finalUrl } = await fetchImage(source, fetchImpl);
  if (!response.ok) throw new Error(`Image request failed: ${response.status}`);
  validatedUrl(finalUrl, "Image redirect URL");

  const contentType = response.headers
    .get("content-type")
    ?.split(";")[0]
    .trim()
    .toLowerCase();
  const extension = EXTENSIONS.get(contentType);
  if (!extension) throw new Error(`Unsupported image content type: ${contentType || "missing"}`);

  const declared = Number(response.headers.get("content-length") || 0);
  if (declared > LIMIT) throw new Error("Image exceeds 10 MiB");
  if (!response.body) throw new Error("Image response has no body");

  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > LIMIT) {
      await reader.cancel().catch(() => {});
      throw new Error("Image exceeds 10 MiB");
    }
    chunks.push(value);
  }

  const bytes = Buffer.concat(chunks.map((chunk) => Buffer.from(chunk)));
  const contentHash = sha256(bytes);
  const directory = path.join(mediaRoot, contentHash.slice(0, 2));
  const localPath = path.join(directory, `${contentHash}.${extension}`);
  const temporary = `${localPath}.${randomUUID()}.tmp`;
  await mkdir(directory, { recursive: true, mode: 0o700 });
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
    contentType,
    localPath,
  };
}
