import fs from "node:fs";
import path from "node:path";
import { loadAllowedRids } from "./load-allowed-rids.mjs";

export function watchAllowedRids(filename, {
  onError = () => {},
  onReload = () => {},
  watch = true,
  pollIntervalMs = 1_000,
} = {}) {
  const state = { current: new Set() };
  let lastError;
  function reload() {
    try {
      state.current = loadAllowedRids(filename);
      lastError = undefined;
      onReload(state.current);
    } catch {
      state.current = new Set();
      if (lastError !== "config_invalid") onError("config_invalid");
      lastError = "config_invalid";
    }
    return state.current;
  }
  reload();
  let watcher;
  let reloadTimer;
  let pollTimer;
  if (watch) {
    const basename = path.basename(filename);
    watcher = fs.watch(path.dirname(filename), { persistent: false }, (_event, changed) => {
      if (changed == null || String(changed) === basename) {
        clearTimeout(reloadTimer);
        reloadTimer = setTimeout(reload, 5);
        reloadTimer.unref?.();
      }
    });
    watcher.on("error", () => {
      state.current = new Set();
      if (lastError !== "config_unreadable") onError("config_unreadable");
      lastError = "config_unreadable";
    });
    pollTimer = setInterval(reload, pollIntervalMs);
    pollTimer.unref?.();
  }
  return {
    get current() { return state.current; },
    reload,
    close() { clearTimeout(reloadTimer); clearInterval(pollTimer); watcher?.close(); },
  };
}
