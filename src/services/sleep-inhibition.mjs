import { spawn } from "node:child_process";

/**
 * Hold only an idle-sleep assertion while a Listener is actually listening.
 * This deliberately does not prevent display sleep, explicit sleep, or lid
 * closure, and it is never used as a LaunchAgent wrapper.
 */
export class MacIdleSleepInhibitor {
  constructor({ spawnProcess = spawn } = {}) {
    this._spawn = spawnProcess;
    this._child = null;
  }

  get active() {
    return this._child !== null;
  }

  acquire() {
    if (this._child) return true;
    try {
      const child = this._spawn("/usr/bin/caffeinate", ["-i"], {
        stdio: "ignore",
        windowsHide: true,
      });
      this._child = child;
      child.once?.("exit", () => {
        if (this._child === child) this._child = null;
      });
      child.once?.("error", () => {
        if (this._child === child) this._child = null;
      });
      return true;
    } catch {
      this._child = null;
      return false;
    }
  }

  release() {
    const child = this._child;
    this._child = null;
    if (!child) return false;
    try {
      child.kill?.("SIGTERM");
    } catch {
      // The assertion has already been dropped from the service's state.
    }
    return true;
  }
}
