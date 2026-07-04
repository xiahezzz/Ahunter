import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { chmod, mkdir, mkdtemp, readdir, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { downloadImage } from "../../src/media/download-image.mjs";

const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xd9]);
const png = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const gif = new TextEncoder().encode("GIF89a");
const webp = new Uint8Array([
  0x52, 0x49, 0x46, 0x46, 0x04, 0x00, 0x00, 0x00,
  0x57, 0x45, 0x42, 0x50,
]);
const avif = new Uint8Array([
  0x00, 0x00, 0x00, 0x18, 0x66, 0x74, 0x79, 0x70,
  0x61, 0x76, 0x69, 0x66, 0x00, 0x00, 0x00, 0x00,
  0x61, 0x76, 0x69, 0x66, 0x6d, 0x69, 0x66, 0x31,
]);
const eventReceivedAt = Date.parse("2026-07-03T01:00:00Z");

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
    downloadImage({ url: "http://example.com/a.jpg", mediaRoot: root, eventReceivedAt }),
    /HTTPS/,
  );
  await assert.rejects(
    downloadImage({
      url: "https://example.com/a.txt",
      mediaRoot: root,
      eventReceivedAt,
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
    "https://localhost./a.jpg",
    "https://sub.localhost./a.jpg",
    "https://LOCALHOST.../a.jpg",
    "https://Sub.LoCaLhOsT../a.jpg",
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
          eventReceivedAt,
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

test("cancels a redirect body before fetching its target", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "a-hunter-media-"));
  const actions = [];
  const redirectBody = new ReadableStream({
    cancel() {
      actions.push("cancel");
    },
  });

  const result = await downloadImage({
    url: "https://example.com/a.jpg",
    mediaRoot: root,
    eventReceivedAt,
    fetchImpl: async () => {
      actions.push("fetch");
      if (actions.filter((action) => action === "fetch").length === 1) {
        return new Response(redirectBody, {
          status: 302,
          headers: { location: "https://cdn.example.com/a.jpg" },
        });
      }
      return response(jpeg);
    },
  });

  assert.deepEqual(actions, ["fetch", "cancel", "fetch"]);
  assert.equal(result.contentType, "image/jpeg");
});

test("detects supported image types from bytes and derives their extensions", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "a-hunter-media-"));
  const cases = [
    ["image/jpeg", "jpg", jpeg],
    ["image/png", "png", png],
    ["image/gif", "gif", gif],
    ["image/webp", "webp", webp],
    ["image/avif", "avif", avif],
  ];

  for (const [type, extension, bytes] of cases) {
    await t.test(type, async () => {
      const result = await downloadImage({
        url: `https://example.com/image.${extension === "jpg" ? "png" : "jpg"}`,
        mediaRoot: root,
        eventReceivedAt,
        fetchImpl: async () => response(bytes, type),
      });
      assert.equal(result.contentType, type);
      assert.equal(path.extname(result.localPath), `.${extension}`);
    });
  }
});

test("rejects a supported MIME header that contradicts the image bytes", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "a-hunter-media-"));
  await assert.rejects(
    downloadImage({
      url: "https://example.com/mislabeled.png",
      mediaRoot: root,
      eventReceivedAt,
      fetchImpl: async () => response(jpeg, "image/png"),
    }),
    /does not match image bytes/,
  );
});

test("revalidates the final redirect URL", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "a-hunter-media-"));
  await assert.rejects(
    downloadImage({
      url: "https://example.com/a.jpg",
      mediaRoot: root,
      eventReceivedAt,
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
    eventReceivedAt,
    fetchImpl,
  });
  const second = await downloadImage({
    url: "https://example.com/b.jpg",
    mediaRoot: root,
    eventReceivedAt,
    fetchImpl,
  });

  assert.equal(first.localPath, second.localPath);
  assert.equal((await stat(path.dirname(first.localPath))).mode & 0o777, 0o700);
  assert.equal((await stat(first.localPath)).mode & 0o777, 0o600);
});

test("deduplicates by detected bytes independently of source filename", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "a-hunter-media-"));
  const first = await downloadImage({
    url: "https://example.com/a.gif",
    mediaRoot: root,
    eventReceivedAt,
    fetchImpl: async () => response(png, "image/png; charset=binary"),
  });
  const second = await downloadImage({
    url: "https://example.com/b.jpg",
    mediaRoot: root,
    eventReceivedAt,
    fetchImpl: async () => response(png, "IMAGE/PNG"),
  });

  assert.equal(first.localPath, second.localPath);
  assert.equal(path.extname(first.localPath), ".png");
});

test("tightens permissions when reusing an existing media directory", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "a-hunter-media-"));
  const directory = path.join(root, "2026-07-03");
  await mkdir(directory, { mode: 0o700 });
  await chmod(directory, 0o755);
  assert.equal((await stat(directory)).mode & 0o777, 0o755);

  await downloadImage({
    url: "https://example.com/existing-directory.jpg",
    mediaRoot: root,
    eventReceivedAt,
    fetchImpl: async () => response(jpeg),
  });

  assert.equal((await stat(directory)).mode & 0o777, 0o700);
});

test("tightens permissions when reusing an existing content-addressed file", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "a-hunter-media-"));
  const hash = createHash("sha256").update(jpeg).digest("hex");
  const directory = path.join(root, "2026-07-03");
  const localPath = path.join(directory, `${hash}.jpg`);
  await mkdir(directory, { mode: 0o700 });
  await writeFile(localPath, jpeg, { mode: 0o644 });
  assert.equal((await stat(localPath)).mode & 0o777, 0o644);

  await downloadImage({
    url: "https://example.com/existing.jpg",
    mediaRoot: root,
    eventReceivedAt,
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
      eventReceivedAt,
      fetchImpl: async () => response(bytes),
    }),
    /10 MiB/,
  );
});

test("stores images under the event's Beijing calendar date", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "a-hunter-media-"));
  const beforeMidnight = await downloadImage({
    url: "https://example.com/before.jpg",
    mediaRoot: root,
    eventReceivedAt: Date.parse("2026-07-03T15:59:59.999Z"),
    fetchImpl: async () => response(jpeg),
  });
  const afterMidnight = await downloadImage({
    url: "https://example.com/after.jpg",
    mediaRoot: root,
    eventReceivedAt: Date.parse("2026-07-03T16:00:00.000Z"),
    fetchImpl: async () => response(jpeg),
  });

  assert.equal(path.basename(path.dirname(beforeMidnight.localPath)), "2026-07-03");
  assert.equal(path.basename(path.dirname(afterMidnight.localPath)), "2026-07-04");
  assert.notEqual(beforeMidnight.localPath, afterMidnight.localPath);
});

test("rejects missing or invalid event timestamps before fetching or writing", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "a-hunter-media-"));
  for (const invalidEventReceivedAt of [undefined, Number.NaN]) {
    await t.test(String(invalidEventReceivedAt), async () => {
      let fetched = false;
      await assert.rejects(downloadImage({
        url: "https://example.com/a.jpg",
        mediaRoot: root,
        eventReceivedAt: invalidEventReceivedAt,
        fetchImpl: async () => {
          fetched = true;
          return response(jpeg);
        },
      }), /eventReceivedAt/);
      assert.equal(fetched, false);
    });
  }
  assert.deepEqual(await readdir(root), []);
});
