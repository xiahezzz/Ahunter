const connectionClosedError = () => new Error("CDP connection closed");

export class CdpClient {
  constructor(url, { webSocketFactory = (value) => new WebSocket(value) } = {}) {
    this.socket = webSocketFactory(url);
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Set();
    this.didClose = false;

    this.ready = new Promise((resolve, reject) => {
      this.socket.addEventListener("open", resolve, { once: true });
      this.socket.addEventListener(
        "error",
        () => reject(new Error("CDP connection failed")),
        { once: true },
      );
      this.socket.addEventListener(
        "close",
        () => reject(connectionClosedError()),
        { once: true },
      );
    });

    this.socket.addEventListener("message", ({ data }) => {
      this.#receive(String(data));
    });
    this.socket.addEventListener("close", () => {
      this.didClose = true;
      for (const { reject } of this.pending.values()) {
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
      this.pending.set(id, { resolve, reject });
    });
    this.socket.send(JSON.stringify({ id, method, params }));
    return response;
  }

  onEvent(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  close() {
    this.socket.close();
  }

  #receive(data) {
    const message = JSON.parse(data);
    if (message.id != null) {
      const pending = this.pending.get(message.id);
      if (!pending) return;

      this.pending.delete(message.id);
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
