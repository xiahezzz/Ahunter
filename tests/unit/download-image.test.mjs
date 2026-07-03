import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { downloadImage } from "../../src/media/download-image.mjs";

const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xd9]);

function response(bytes, type = "image/jpeg", url = "") {
  const value = new Response(bytes, {
    status: 200,
    headers: { "content-type": type, "content-length": String(bytes.byteLength) },
  });
  if (url) Object.defineProperty(value, "url", { value: url });
  return value;
}

test("requires HTTPS and an image MIME type", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "a-hunter-media-"));
  await assert.rejects(
    downloadImage({ url: "http://example.com/a.jpg", mediaRoot: root }),
    /HTTPS/,
  );
  await assert.rejects(
    downloadImage({
      url: "https://example.com/a.txt",
      mediaRoot: root,
      fetchImpl: async () => response(jpeg, "text/plain"),
    }),
    /content type/,
  );
});

test("rejects credentials and local address literals before fetching", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "a-hunter-media-"));
  const urls = [
    "https://user:secret@example.com/a.jpg",
    "https://localhost/a.jpg",
    "https://assets.localhost/a.jpg",
    "https://127.0.0.1/a.jpg",
    "https://10.2.3.4/a.jpg",
    "https://172.16.4.5/a.jpg",
    "https://192.168.1.2/a.jpg",
    "https://169.254.2.3/a.jpg",
    "https://[::1]/a.jpg",
    "https://[fd00::1]/a.jpg",
    "https://[fe80::1]/a.jpg",
    "https://[::ffff:127.0.0.1]/a.jpg",
  ];

  for (const url of urls) {
    await t.test(new URL(url).hostname, async () => {
      let fetched = false;
      await assert.rejects(
        downloadImage({
          url,
          mediaRoot: root,
          fetchImpl: async () => {
            fetched = true;
            return response(jpeg);
          },
        }),
        /not allowed/,
      );
      assert.equal(fetched, false);
    });
  }
});

test("revalidates the final redirect URL", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "a-hunter-media-"));
  await assert.rejects(
    downloadImage({
      url: "https://example.com/a.jpg",
      mediaRoot: root,
      fetchImpl: async () => response(jpeg, "image/jpeg", "https://127.0.0.1/a.jpg"),
    }),
    /not allowed/,
  );
});

test("deduplicates identical image bytes and uses owner-only permissions", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "a-hunter-media-"));
  const fetchImpl = async () => response(jpeg);
  const first = await downloadImage({
    url: "https://example.com/a.jpg",
    mediaRoot: root,
    fetchImpl,
  });
  const second = await downloadImage({
    url: "https://example.com/b.jpg",
    mediaRoot: root,
    fetchImpl,
  });

  assert.equal(first.localPath, second.localPath);
  assert.equal((await stat(path.dirname(first.localPath))).mode & 0o777, 0o700);
  assert.equal((await stat(first.localPath)).mode & 0o777, 0o600);
});

test("tightens permissions when reusing an existing content-addressed file", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "a-hunter-media-"));
  const hash = createHash("sha256").update(jpeg).digest("hex");
  const directory = path.join(root, hash.slice(0, 2));
  const localPath = path.join(directory, `${hash}.jpg`);
  await mkdir(directory, { mode: 0o700 });
  await writeFile(localPath, jpeg, { mode: 0o644 });
  assert.equal((await stat(localPath)).mode & 0o777, 0o644);

  await downloadImage({
    url: "https://example.com/existing.jpg",
    mediaRoot: root,
    fetchImpl: async () => response(jpeg),
  });

  assert.equal((await stat(localPath)).mode & 0o777, 0o600);
});

test("rejects bodies larger than 10 MiB", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "a-hunter-media-"));
  const bytes = new Uint8Array(10 * 1024 * 1024 + 1);
  await assert.rejects(
    downloadImage({
      url: "https://example.com/large.jpg",
      mediaRoot: root,
      fetchImpl: async () => response(bytes),
    }),
    /10 MiB/,
  );
});
