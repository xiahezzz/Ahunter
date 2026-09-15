export type MxListenerStatus = {
  service_id: "mx-listener";
  status: string;
  details: string;
  launchagent_loaded: boolean;
  liveness: "live" | "offline";
  readiness: "starting" | "waiting_for_chrome" | "waiting_for_authorization" | "connecting" | "listening" | "stopping";
  health: "healthy" | "degraded" | "failed";
  reason_code: string;
  connected_at: string | null;
  last_frame_at: string | null;
  last_accepted_event_at: string | null;
  lease_expires_at: string | null;
  rid_count: number;
  collection_enabled: boolean;
  rid_config_valid: boolean;
};

export type RidAuthorization = {
  rids: number[];
  version: string;
  collection_enabled: boolean;
};

export type MxEventMedia = {
  available_count: number;
  pending_count: number;
  has_media: boolean;
};

export type MxEventSummary = {
  event_id: string;
  rid: number;
  authorization: "current" | "revoked";
  received_at: string;
  source_created_at: string | null;
  content_hash: string;
  summary: string;
  media: MxEventMedia;
};

export type MxEventPage = {
  events: MxEventSummary[];
  next_cursor: string | null;
  limit: number;
};

export type MxTextBlock = { type: "text"; text: string };
export type MxMediaBlock = {
  type: "media";
  media_id: string;
  content_hash: string;
  content_type: "image/jpeg" | "image/png" | "image/gif" | "image/webp" | "image/avif";
  href: string;
};
export type MxEventDetail = Omit<MxEventSummary, "summary" | "media"> & { blocks: Array<MxTextBlock | MxMediaBlock> };

const MAX_SAFE_INTEGER = 2 ** 53 - 1;
const HASH = /^[0-9a-f]{64}$/;
const OPAQUE = /^[A-Za-z0-9_-]{8,2048}\.[A-Za-z0-9_-]{16,128}$/;
const ISO_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/;
const DANGEROUS_TEXT = /(https?:\/\/|(?:^|[\\/])(?:Users|tmp|data)(?:[\\/])|raw[_ -]?payload|source[_ -]?url|cookie|127\.0\.0\.1|localhost)/i;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isExactRecord(value: unknown, keys: readonly string[]): value is Record<string, unknown> {
  return isRecord(value) && Object.keys(value).length === keys.length && Object.keys(value).every((key) => keys.includes(key));
}

function boundedText(value: unknown, maximum = 800): value is string {
  return typeof value === "string" && value.length <= maximum && !DANGEROUS_TEXT.test(value);
}

function safeTimestamp(value: unknown): value is string {
  return typeof value === "string" && value.length <= 64 && ISO_TIMESTAMP.test(value) && Number.isFinite(Date.parse(value));
}

function nullableTimestamp(value: unknown): value is string | null {
  return value === null || safeTimestamp(value);
}

function safeRid(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0 && value <= MAX_SAFE_INTEGER;
}

function safeOpaque(value: unknown): value is string {
  return typeof value === "string" && OPAQUE.test(value);
}

function safeHash(value: unknown): value is string {
  return typeof value === "string" && HASH.test(value);
}

export function parseMxListenerStatus(value: unknown): MxListenerStatus {
  const keys = [
    "service_id", "status", "details", "launchagent_loaded", "liveness", "readiness", "health", "reason_code",
    "connected_at", "last_frame_at", "last_accepted_event_at", "lease_expires_at", "rid_count", "collection_enabled", "rid_config_valid",
  ] as const;
  if (
    !isExactRecord(value, keys) ||
    value.service_id !== "mx-listener" ||
    !boundedText(value.status, 80) || !boundedText(value.details, 240) ||
    typeof value.launchagent_loaded !== "boolean" ||
    (value.liveness !== "live" && value.liveness !== "offline") ||
    !["starting", "waiting_for_chrome", "waiting_for_authorization", "connecting", "listening", "stopping"].includes(String(value.readiness)) ||
    !["healthy", "degraded", "failed"].includes(String(value.health)) ||
    !boundedText(value.reason_code, 64) ||
    !nullableTimestamp(value.connected_at) || !nullableTimestamp(value.last_frame_at) ||
    !nullableTimestamp(value.last_accepted_event_at) || !nullableTimestamp(value.lease_expires_at) ||
    !Number.isSafeInteger(value.rid_count) || (value.rid_count as number) < 0 || (value.rid_count as number) > 1000 ||
    typeof value.collection_enabled !== "boolean" || typeof value.rid_config_valid !== "boolean"
  ) {
    throw new Error("MX Listener 状态格式无效");
  }
  return value as MxListenerStatus;
}

export function parseRidAuthorization(value: unknown): RidAuthorization {
  if (
    !isExactRecord(value, ["rids", "version", "collection_enabled"]) ||
    !Array.isArray(value.rids) || value.rids.length > 1000 || !value.rids.every(safeRid) ||
    new Set(value.rids).size !== value.rids.length ||
    !safeHash(value.version) || typeof value.collection_enabled !== "boolean"
  ) {
    throw new Error("RID 授权配置格式无效");
  }
  const rids = [...value.rids].sort((left, right) => left - right);
  if (rids.some((rid, index) => index > 0 && rids[index - 1] >= rid) || value.collection_enabled !== (rids.length > 0)) {
    throw new Error("RID 授权配置格式无效");
  }
  return { rids, version: value.version, collection_enabled: value.collection_enabled };
}

function parseMedia(value: unknown): MxEventMedia {
  if (
    !isExactRecord(value, ["available_count", "pending_count", "has_media"]) ||
    !Number.isSafeInteger(value.available_count) || (value.available_count as number) < 0 || (value.available_count as number) > 20 ||
    !Number.isSafeInteger(value.pending_count) || (value.pending_count as number) < 0 || (value.pending_count as number) > 20 ||
    typeof value.has_media !== "boolean" || value.has_media !== ((value.available_count as number) > 0 || (value.pending_count as number) > 0)
  ) {
    throw new Error("MX 资讯媒体格式无效");
  }
  return value as MxEventMedia;
}

function parseEventCore(value: unknown, includeSummary: boolean): MxEventSummary | Omit<MxEventSummary, "summary" | "media"> {
  const keys = includeSummary
    ? ["event_id", "rid", "authorization", "received_at", "source_created_at", "content_hash", "summary", "media"]
    : ["event_id", "rid", "authorization", "received_at", "source_created_at", "content_hash"];
  if (
    !isExactRecord(value, keys) || !safeOpaque(value.event_id) || !safeRid(value.rid) ||
    (value.authorization !== "current" && value.authorization !== "revoked") ||
    !safeTimestamp(value.received_at) || !nullableTimestamp(value.source_created_at) || !safeHash(value.content_hash)
  ) {
    throw new Error("MX 资讯格式无效");
  }
  if (!includeSummary) return value as Omit<MxEventSummary, "summary" | "media">;
  if (!boundedText(value.summary, 800)) throw new Error("MX 资讯格式无效");
  return { ...value, media: parseMedia(value.media) } as MxEventSummary;
}

export function parseMxEventPage(value: unknown): MxEventPage {
  if (
    !isExactRecord(value, ["events", "next_cursor", "limit"]) ||
    !Array.isArray(value.events) || value.events.length > 100 ||
    !value.events.every((item) => {
      try { parseEventCore(item, true); return true; } catch { return false; }
    }) ||
    (value.next_cursor !== null && !safeOpaque(value.next_cursor)) ||
    !Number.isSafeInteger(value.limit) || (value.limit as number) < 1 || (value.limit as number) > 100
  ) {
    throw new Error("MX 资讯列表格式无效");
  }
  return {
    events: value.events.map((item) => parseEventCore(item, true) as MxEventSummary),
    next_cursor: value.next_cursor as string | null,
    limit: value.limit as number,
  };
}

export function parseMxEventDetail(value: unknown): MxEventDetail {
  if (!isRecord(value) || !Array.isArray(value.blocks) || value.blocks.length > 21) throw new Error("MX 资讯详情格式无效");
  const { blocks, ...core } = value;
  const parsedCore = parseEventCore(core, false);
  const eventId = parsedCore.event_id;
  const parsedBlocks = blocks.map((block): MxTextBlock | MxMediaBlock => {
    if (isExactRecord(block, ["type", "text"]) && block.type === "text" && boundedText(block.text, 12000)) {
      return { type: "text", text: block.text };
    }
    if (
      isExactRecord(block, ["type", "media_id", "content_hash", "content_type", "href"]) && block.type === "media" &&
      safeOpaque(block.media_id) && safeHash(block.content_hash) &&
      ["image/jpeg", "image/png", "image/gif", "image/webp", "image/avif"].includes(String(block.content_type)) &&
      typeof block.href === "string" && block.href === `/api/mx/events/${eventId}/media/${block.media_id}`
    ) {
      return block as MxMediaBlock;
    }
    throw new Error("MX 资讯详情格式无效");
  });
  return { ...parsedCore, blocks: parsedBlocks };
}

export async function boundedApiError(response: Response, fallback: string): Promise<string> {
  try {
    const payload: unknown = await response.json();
    if (isRecord(payload) && boundedText(payload.detail, 160)) return payload.detail;
  } catch {
    // A malformed failure is intentionally reduced to a finite local message.
  }
  return fallback;
}
