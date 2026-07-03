#!/usr/bin/env node

import { access, mkdir, rename } from "node:fs/promises";
import path from "node:path";

const source = path.resolve("data/mx-websocket/decoded-room-messages.json");
const destination = path.resolve(
  "data/quarantine/decoded-room-messages.pre-rid-filter.json",
);

try {
  await access(source);
} catch {
  console.log("No legacy output to quarantine");
  process.exit(0);
}

try {
  await access(destination);
  throw new Error(`Quarantine destination already exists: ${destination}`);
} catch (error) {
  if (error.code !== "ENOENT") throw error;
}

await mkdir(path.dirname(destination), { recursive: true });
await rename(source, destination);
console.log(`Quarantined legacy output: ${destination}`);
