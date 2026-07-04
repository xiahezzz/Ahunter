const MX_ORIGIN = "https://mx.2026.naaifu.cn";

export class AuthorizationRequiredError extends Error {
  constructor() {
    super("authorization_required");
    this.name = "AuthorizationRequiredError";
    this.code = "authorization_required";
  }
}

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
    throw new AuthorizationRequiredError();
  }
  return target.webSocketDebuggerUrl;
}
