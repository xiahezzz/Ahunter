import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { loadAllowedRids } from "../../src/config/load-allowed-rids.mjs";

async function configFile(contents) {
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-config-"));
  const filename = path.join(directory, "allowed-rids.yaml");
  await writeFile(filename, contents);
  return filename;
}

test("empty allowlist is valid and fail-closed", async () => {
  assert.deepEqual([...loadAllowedRids(await configFile("allowed_rids: []\n"))], []);
});

test("deduplicates positive integer RIDs", async () => {
  const result = loadAllowedRids(
    await configFile("allowed_rids:\n  - 20025\n  - 23200\n  - 20025\n"),
  );
  assert.deepEqual([...result], [20025, 23200]);
});

for (const invalid of ["allowed_rids: 20025\n", "allowed_rids: [0]\n", "allowed_rids: [\"20025\"]\n"]) {
  test(`rejects invalid config: ${JSON.stringify(invalid)}`, async () => {
    const filename = await configFile(invalid);
    assert.throws(() => loadAllowedRids(filename), /allowed_rids/);
  });
}
