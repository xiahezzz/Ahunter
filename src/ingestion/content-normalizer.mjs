export const MX_DISCLAIMER =
  "免责声明：信息来源于官方媒体/网络新闻等，仅信息分享，不作为投资建议！";

export function isMxDisclaimer(value) {
  return typeof value === "string" && value.trim() === MX_DISCLAIMER;
}

const DROP = Symbol("drop-disclaimer");

function clean(value, location) {
  if (typeof value === "string") {
    if (isMxDisclaimer(value) && ["root", "array", "msg"].includes(location)) {
      return DROP;
    }
    return value;
  }
  if (Array.isArray(value)) {
    return value.flatMap((child) => {
      const cleaned = clean(child, "array");
      return cleaned === DROP ? [] : [cleaned];
    });
  }
  if (!value || typeof value !== "object") return value;

  const entries = Object.entries(value);
  const type = String(value.type || "").toLowerCase();
  const onlyTextFields = entries.every(([key]) => ["type", "msg"].includes(key));
  if (type === "text" && onlyTextFields && isMxDisclaimer(value.msg)) return DROP;

  const result = {};
  for (const [key, child] of entries) {
    const cleaned = clean(child, key === "msg" ? "msg" : "property");
    if (cleaned !== DROP) result[key] = cleaned;
  }
  return result;
}

export function normalizeMxContent(value) {
  const cleaned = clean(value, "root");
  return cleaned === DROP ? null : cleaned;
}
