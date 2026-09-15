import { createHash } from "node:crypto";
import fs from "node:fs";
import YAML, { isMap, isScalar, isSeq } from "yaml";

const MAX_CONFIG_BYTES = 64 * 1024;
const MAX_ALLOWED_RIDS = 1_000;
const POSITIVE_SAFE_INTEGER = /^[1-9][0-9]*$/;

/** A bounded error that deliberately contains no configuration contents or path. */
export class AllowedRidsConfigError extends Error {
  constructor(code = "config_invalid") {
    super(code);
    this.code = code;
  }
}

/**
 * Read and validate the owner-authorized RID configuration.
 *
 * The bytes used for `version` are exactly the bytes read from disk.  This is
 * important because a writer uses the version as an optimistic-concurrency
 * token, rather than deriving it from a parsed representation.
 */
export function readAllowedRidsConfig(filename) {
  const bytes = readRegularFile(filename);
  const rids = parseAllowedRidsYaml(bytes);
  return Object.freeze({
    rids: Object.freeze(rids),
    version: createHash("sha256").update(bytes).digest("hex"),
    enabled: rids.length > 0,
  });
}

/** Preserve the existing collector-facing Set API. */
export function loadAllowedRids(filename) {
  return new Set(readAllowedRidsConfig(filename).rids);
}

/** Exported for cross-language fixture tests without requiring a real file. */
export function parseAllowedRidsYaml(value) {
  const bytes = Buffer.isBuffer(value) ? value : Buffer.from(String(value), "utf8");
  if (bytes.length > MAX_CONFIG_BYTES) throw new AllowedRidsConfigError();

  let document;
  try {
    document = YAML.parseDocument(bytes.toString("utf8"), {
      prettyErrors: false,
      strict: true,
      uniqueKeys: true,
    });
  } catch {
    throw new AllowedRidsConfigError();
  }
  if (document.errors.length > 0 || document.warnings.length > 0) {
    throw new AllowedRidsConfigError();
  }

  const root = document.contents;
  if (!isMap(root) || root.items.length !== 1) throw new AllowedRidsConfigError();
  const entry = root.items[0];
  if (!isScalar(entry.key) || entry.key.value !== "allowed_rids" || !isSeq(entry.value)) {
    throw new AllowedRidsConfigError();
  }
  if (entry.value.items.length > MAX_ALLOWED_RIDS) throw new AllowedRidsConfigError();

  const rids = [];
  const seen = new Set();
  for (const item of entry.value.items) {
    // YAML parses 1.0 and 0x1 as JavaScript numbers as well.  The source
    // token check keeps the contract identical to the canonical YAML written
    // by the local control plane: only decimal positive safe integers.
    if (
      !isScalar(item)
      || item.type !== "PLAIN"
      || typeof item.value !== "number"
      || !Number.isSafeInteger(item.value)
      || item.value <= 0
      || typeof item.source !== "string"
      || !POSITIVE_SAFE_INTEGER.test(item.source)
      || seen.has(item.value)
    ) {
      throw new AllowedRidsConfigError();
    }
    seen.add(item.value);
    rids.push(item.value);
  }
  return rids.sort((left, right) => left - right);
}

function readRegularFile(filename) {
  if (typeof filename !== "string" && !(filename instanceof URL)) {
    throw new AllowedRidsConfigError("config_unreadable");
  }
  let descriptor;
  try {
    const before = fs.lstatSync(filename);
    if (!before.isFile() || before.isSymbolicLink() || before.size > MAX_CONFIG_BYTES) {
      throw new AllowedRidsConfigError();
    }
    descriptor = fs.openSync(filename, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0));
    const opened = fs.fstatSync(descriptor);
    if (!opened.isFile() || opened.size > MAX_CONFIG_BYTES) throw new AllowedRidsConfigError();
    const bytes = fs.readFileSync(descriptor);
    if (bytes.length > MAX_CONFIG_BYTES) throw new AllowedRidsConfigError();
    return bytes;
  } catch (error) {
    if (error instanceof AllowedRidsConfigError) throw error;
    throw new AllowedRidsConfigError("config_unreadable");
  } finally {
    if (descriptor !== undefined) fs.closeSync(descriptor);
  }
}
