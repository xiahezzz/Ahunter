import fs from "node:fs";
import YAML from "yaml";

export function loadAllowedRids(filename) {
  const document = YAML.parse(fs.readFileSync(filename, "utf8"));
  const values = document?.allowed_rids;
  if (!Array.isArray(values)) throw new TypeError("allowed_rids must be an array");
  for (const value of values) {
    if (!Number.isSafeInteger(value) || value <= 0) {
      throw new TypeError("allowed_rids must contain positive integers");
    }
  }
  return new Set(values);
}
