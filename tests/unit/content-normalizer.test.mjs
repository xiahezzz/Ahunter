import assert from "node:assert/strict";
import test from "node:test";
import {
  isMxDisclaimer,
  MX_DISCLAIMER,
  normalizeMxContent,
} from "../../src/ingestion/content-normalizer.mjs";

test("recognizes only exact disclaimer strings with optional surrounding whitespace", () => {
  assert.equal(isMxDisclaimer(MX_DISCLAIMER), true);
  assert.equal(isMxDisclaimer(`  ${MX_DISCLAIMER}\n`), true);
  assert.equal(isMxDisclaimer(`正文 ${MX_DISCLAIMER}`), false);
});

test("removes disclaimer array entries while preserving longer text", () => {
  assert.deepEqual(
    normalizeMxContent([
      { type: "text", msg: "正文" },
      { type: "text", msg: MX_DISCLAIMER },
      MX_DISCLAIMER,
      `正文引用：${MX_DISCLAIMER}`,
    ]),
    [
      { type: "text", msg: "正文" },
      `正文引用：${MX_DISCLAIMER}`,
    ],
  );
});

test("removes disclaimer text objects without removing images or unrelated properties", () => {
  assert.deepEqual(
    normalizeMxContent({
      items: [
        { type: "pic", url: "https://example.com/a.png" },
        { type: "text", msg: MX_DISCLAIMER },
      ],
      attribution: MX_DISCLAIMER,
    }),
    {
      items: [{ type: "pic", url: "https://example.com/a.png" }],
      attribution: MX_DISCLAIMER,
    },
  );
});

test("drops an exact disclaimer msg while preserving sibling text-object properties", () => {
  assert.deepEqual(
    normalizeMxContent({ type: "text", msg: MX_DISCLAIMER, extra: "keep" }),
    { type: "text", extra: "keep" },
  );
});

test("does not mutate its input", () => {
  const input = {
    type: "container",
    items: [
      { type: "text", msg: MX_DISCLAIMER, extra: "keep" },
      { type: "pic", url: "https://example.com/a.png" },
    ],
  };
  const before = structuredClone(input);

  normalizeMxContent(input);

  assert.deepEqual(input, before);
});

test("normalizes a root-only disclaimer to null", () => {
  assert.equal(normalizeMxContent(MX_DISCLAIMER), null);
});
