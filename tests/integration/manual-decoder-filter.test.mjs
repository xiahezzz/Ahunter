import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { encodeRoomPayload } from "../helpers/encode-room-payload.mjs";

test("manual decoder writes only configured RID", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "a-hunter-cli-"));
  const input = path.join(directory, "frames.json");
  const output = path.join(directory, "decoded.json");
  const config = path.join(directory, "allowed.yaml");
  const make = (rid, msg) =>
    `42/msg,["room_msg",${JSON.stringify(
      encodeRoomPayload({ id: rid, rid, msg }, "2026-07-03"),
    )}]`;
  await writeFile(
    input,
    JSON.stringify([
      make(20025, "allowed-marker"),
      make(23200, "rejected-marker"),
    ]),
  );
  await writeFile(config, "allowed_rids: [20025]\n");

  const result = spawnSync(process.execPath, [
    "scripts/mx-websocket/decode-room-msg.mjs",
    "--input",
    input,
    "--output",
    output,
    "--config",
    config,
    "--date",
    "2026-07-03",
  ], { encoding: "utf8" });

  assert.equal(result.status, 0, result.stderr);
  const text = await readFile(output, "utf8");
  assert.equal(JSON.parse(text).messages.length, 1);
  assert.equal(text.includes("allowed-marker"), true);
  assert.equal(text.includes("rejected-marker"), false);
});
