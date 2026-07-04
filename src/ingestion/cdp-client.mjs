const connectionClosedError = () => new Error("CDP connection closed");

export class CdpClient {
  constructor(url, {
    webSocketFactory = (value) => new WebSocket(value),
    openTimeoutMs,
    commandTimeoutMs,
    signal,
  } = {}) {
    this.socket = webSocketFactory(url);
    this.commandTimeoutMs = commandTimeoutMs;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Set();
    this.didClose = false;
    this.closeRequested = false;
    this.closed = new Promise((resolve) => {
      this.socket.addEventListener("close", resolve, { once: true });
    });

    this.ready = new Promise((resolve, reject) => {
      let settled = false;
      let openTimer;
      const finish = (action, value) => {
        if (settled) return;
        settled = true;
        clearTimeout(openTimer);
        action(value);
      };
      this.socket.addEventListener("open", () => finish(resolve), { once: true });
      this.socket.addEventListener(
        "error",
        () => finish(reject, new Error("CDP connection failed")),
        { once: true },
      );
      this.socket.addEventListener(
        "close",
        () => finish(reject, connectionClosedError()),
        { once: true },
      );
      if (openTimeoutMs != null) {
        openTimer = setTimeout(() => {
          finish(reject, new Error("CDP WebSocket open timeout"));
          this.close();
        }, openTimeoutMs);
      }
      if (signal) {
        const abort = () => {
          finish(reject, signal.reason ?? new Error("CDP operation aborted"));
          this.close();
        };
        if (signal.aborted) abort();
        else signal.addEventListener("abort", abort, { once: true });
        this.closed.finally(() => signal.removeEventListener("abort", abort));
      }
    });

    this.socket.addEventListener("message", ({ data }) => {
      this.#receive(String(data));
    });
    this.socket.addEventListener("close", () => {
      this.didClose = true;
      this.closeRequested = true;
      for (const { reject, timer } of this.pending.values()) {
        clearTimeout(timer);
        reject(connectionClosedError());
      }
      this.pending.clear();
    });
  }

  async send(method, params = {}) {
    await this.ready;
    if (this.didClose) throw connectionClosedError();

    const id = this.nextId++;
    const response = new Promise((resolve, reject) => {
      let timer;
      if (this.commandTimeoutMs != null) {
        timer = setTimeout(() => {
          if (!this.pending.delete(id)) return;
          reject(new Error(`CDP command timeout: ${method}`));
        }, this.commandTimeoutMs);
      }
      this.pending.set(id, { resolve, reject, timer });
    });
    this.socket.send(JSON.stringify({ id, method, params }));
    return response;
  }

  onEvent(listener) {
    this.listeners.add(listener);
    let registered = true;
    return () => {
      if (!registered) return false;
      registered = false;
      return this.listeners.delete(listener);
    };
  }

  close() {
    if (this.closeRequested) return;
    this.closeRequested = true;
    this.socket.close();
  }

  #receive(data) {
    const message = JSON.parse(data);
    if (message.id != null) {
      const pending = this.pending.get(message.id);
      if (!pending) return;

      this.pending.delete(message.id);
      clearTimeout(pending.timer);
      if (message.error) {
        pending.reject(new Error(message.error.message));
      } else {
        pending.resolve(message.result);
      }
      return;
    }

    for (const listener of this.listeners) listener(message);
  }
}
