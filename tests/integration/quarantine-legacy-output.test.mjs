import assert from "node:assert/strict";
import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import test from "node:test";

const script = path.resolve("scripts/quarantine-legacy-output.mjs");

function run(directory) {
  return spawnSync(process.execPath, [script], {
    cwd: directory,
    encoding: "utf8",
  });
}

test("quarantine reports an absent legacy output without creating files", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-quarantine-"));
  try {
    const result = run(directory);
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /No legacy output to quarantine/);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("quarantine refuses to replace an existing destination", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-quarantine-"));
  const source = path.join(directory, "data/mx-websocket/decoded-room-messages.json");
  const destination = path.join(
    directory,
    "data/quarantine/decoded-room-messages.pre-rid-filter.json",
  );
  try {
    await mkdir(path.dirname(source), { recursive: true });
    await mkdir(path.dirname(destination), { recursive: true });
    await writeFile(source, "source-marker");
    await writeFile(destination, "destination-marker");

    const result = run(directory);
    assert.notEqual(result.status, 0);
    assert.equal(await readFile(source, "utf8"), "source-marker");
    assert.equal(await readFile(destination, "utf8"), "destination-marker");
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("quarantine rethrows source access errors", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-quarantine-"));
  const dataDirectory = path.join(directory, "data");
  try {
    await mkdir(dataDirectory);
    await chmod(dataDirectory, 0o000);

    const result = run(directory);
    assert.notEqual(result.status, 0);
    assert.doesNotMatch(result.stdout, /No legacy output to quarantine/);
  } finally {
    await chmod(dataDirectory, 0o700);
    await rm(directory, { recursive: true, force: true });
  }
});
