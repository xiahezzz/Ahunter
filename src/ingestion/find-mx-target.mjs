const MX_ORIGIN = "https://mx.2026.naaifu.cn";

export async function findMxTarget(baseUrl, fetchImpl = fetch) {
  const response = await fetchImpl(new URL("/json/list", baseUrl).href);
  if (!response.ok) {
    throw new Error(`CDP target list failed: ${response.status}`);
  }

  const targets = await response.json();
  const target = targets.find((item) => {
    if (!item.webSocketDebuggerUrl) return false;
    try {
      return new URL(item.url).origin === MX_ORIGIN;
    } catch {
      return false;
    }
  });
  if (!target) {
    throw new Error("MX Chrome target is not open or not logged in");
  }
  return target.webSocketDebuggerUrl;
}
