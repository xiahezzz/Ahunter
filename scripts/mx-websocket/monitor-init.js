(() => {
  globalThis.__mxWsMonitor?.stop?.();

  const NativeWebSocket = globalThis.WebSocket;
  const records = [];
  const attachedListeners = [];
  let active = true;

  const MonitoringWebSocket = new Proxy(NativeWebSocket, {
    construct(Target, args, NewTarget) {
      const socket = Reflect.construct(Target, args, NewTarget);
      const record = {
        url: String(args[0] || ""),
        createdAt: Date.now(),
        openedAt: null,
        closedAt: null,
        closeCode: null,
        incoming: [],
      };
      records.push(record);

      const onOpen = () => {
        if (active) record.openedAt = Date.now();
      };
      const onClose = (event) => {
        if (!active) return;
        record.closedAt = Date.now();
        record.closeCode = event.code;
      };
      const onMessage = (event) => {
        if (!active) return;

        const data =
          typeof event.data === "string"
            ? event.data.length > 200_000
              ? `${event.data.slice(0, 200_000)}…[truncated]`
              : event.data
            : `[${event.data?.constructor?.name || typeof event.data}]`;

        record.incoming.push({ at: Date.now(), data });
        if (record.incoming.length > 2_000) record.incoming.shift();
      };

      socket.addEventListener("open", onOpen);
      socket.addEventListener("close", onClose);
      socket.addEventListener("message", onMessage);
      attachedListeners.push({ socket, onOpen, onClose, onMessage });
      return socket;
    },
    get(Target, property) {
      return Reflect.get(Target, property, Target);
    },
  });

  globalThis.WebSocket = MonitoringWebSocket;
  Object.defineProperty(globalThis, "__mxWsMonitor", {
    configurable: true,
    enumerable: false,
    value: {
      isActive: () => active,
      snapshot: () =>
        records.map((record) => ({
          ...record,
          incoming: record.incoming.slice(),
        })),
      roomMessageFrames: () =>
        records.flatMap((record, connectionIndex) =>
          record.incoming
            .filter(
              ({ data }) =>
                data.startsWith("42/msg,") && data.includes('["room_msg",'),
            )
            .map(({ at, data }, frameIndex) => ({
              connectionIndex,
              frameIndex,
              at,
              data,
            })),
        ),
      stop: () => {
        active = false;
        for (const item of attachedListeners) {
          item.socket.removeEventListener("open", item.onOpen);
          item.socket.removeEventListener("close", item.onClose);
          item.socket.removeEventListener("message", item.onMessage);
        }
        globalThis.WebSocket = NativeWebSocket;
        return true;
      },
    },
  });
})();
