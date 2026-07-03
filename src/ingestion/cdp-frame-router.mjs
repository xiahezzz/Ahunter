export function createMxFrameRouter({
  onFrame,
  socketUrlPattern = /\/business-api\/5\/socket\.io\//,
  now = () => Date.now(),
}) {
  const matchingRequestIds = new Set();

  return (event) => {
    if (event.method === "Network.webSocketCreated") {
      if (socketUrlPattern.test(event.params.url)) {
        matchingRequestIds.add(event.params.requestId);
      }
      return;
    }

    if (event.method !== "Network.webSocketFrameReceived") return;
    if (event.params.response.opcode !== 1) return;

    const { payloadData } = event.params.response;
    const isPreexistingRoomSocket = /^42\/msg,\["room_msg",/.test(payloadData);
    if (
      !matchingRequestIds.has(event.params.requestId) &&
      !isPreexistingRoomSocket
    ) {
      return;
    }

    onFrame({ payloadData, receivedAt: now() });
  };
}
