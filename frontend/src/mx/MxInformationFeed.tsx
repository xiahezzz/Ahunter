import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, Filter, RefreshCw } from "lucide-react";
import MxEventDetail from "./MxEventDetail";
import { boundedApiError, MxEventDetail as MxEventDetailPayload, MxEventPage, MxEventSummary, parseMxEventDetail, parseMxEventPage } from "./contracts";

type AuthorizationFilter = "" | "current" | "revoked";
type MediaFilter = "" | "true" | "false";
type Filters = {
  rids: number[];
  authorization: AuthorizationFilter;
  startAt: string;
  endAt: string;
  hasMedia: MediaFilter;
  query: string;
};

type LocationState = { filters: Filters; eventId: string | null; invalid: boolean };

const RID = /^[1-9][0-9]{0,15}$/;
const OPAQUE = /^[A-Za-z0-9_-]{8,2048}\.[A-Za-z0-9_-]{16,128}$/;
const ISO_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/;
const EMPTY_FILTERS: Filters = { rids: [], authorization: "", startAt: "", endAt: "", hasMedia: "", query: "" };

class PageRequestError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

function parseRidList(value: string): number[] | null {
  if (!value.trim()) return [];
  const parts = value.split(",").map((item) => item.trim());
  if (parts.length > 100 || parts.some((item) => !RID.test(item))) return null;
  const rids = parts.map(Number);
  if (rids.some((rid) => !Number.isSafeInteger(rid) || rid > 2 ** 53 - 1) || new Set(rids).size !== rids.length) return null;
  return rids.sort((left, right) => left - right);
}

function datetimeInput(value: Date): string {
  const number = (part: number) => String(part).padStart(2, "0");
  return `${value.getFullYear()}-${number(value.getMonth() + 1)}-${number(value.getDate())}T${number(value.getHours())}:${number(value.getMinutes())}`;
}

function parseUrlTimestamp(value: string | null): string | null {
  if (!value || !ISO_TIMESTAMP.test(value)) return null;
  const parsed = new Date(value);
  return Number.isFinite(parsed.getTime()) ? datetimeInput(parsed) : null;
}

function apiTimestamp(value: string): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isFinite(parsed.getTime()) ? parsed.toISOString() : null;
}

function readLocation(): LocationState {
  const params = new URLSearchParams(window.location.search);
  const allowed = new Set(["rid", "authorization", "start_at", "end_at", "has_media", "q", "event"]);
  let invalid = [...params.keys()].some((key) => !allowed.has(key));
  const rids = parseRidList(params.getAll("rid").join(","));
  if (rids === null) invalid = true;
  const authorization = params.get("authorization") ?? "";
  if (authorization !== "" && authorization !== "current" && authorization !== "revoked") invalid = true;
  const hasMedia = params.get("has_media") ?? "";
  if (hasMedia !== "" && hasMedia !== "true" && hasMedia !== "false") invalid = true;
  const startAt = parseUrlTimestamp(params.get("start_at"));
  const endAt = parseUrlTimestamp(params.get("end_at"));
  if ((params.has("start_at") && startAt === null) || (params.has("end_at") && endAt === null)) invalid = true;
  if (startAt && endAt && new Date(startAt) > new Date(endAt)) invalid = true;
  const query = params.get("q") ?? "";
  if (query.length > 160 || /https?:\/\//i.test(query)) invalid = true;
  const rawEvent = params.get("event");
  if (rawEvent !== null && !OPAQUE.test(rawEvent)) invalid = true;
  if (invalid) return { filters: EMPTY_FILTERS, eventId: null, invalid: true };
  return {
    filters: { rids: rids ?? [], authorization: authorization as AuthorizationFilter, startAt: startAt ?? "", endAt: endAt ?? "", hasMedia: hasMedia as MediaFilter, query },
    eventId: rawEvent,
    invalid: false,
  };
}

function filterKey(filters: Filters): string {
  return JSON.stringify(filters);
}

function searchFor(filters: Filters, eventId: string | null = null): string {
  const params = new URLSearchParams();
  for (const rid of filters.rids) params.append("rid", String(rid));
  if (filters.authorization) params.set("authorization", filters.authorization);
  const start = apiTimestamp(filters.startAt);
  const end = apiTimestamp(filters.endAt);
  if (start) params.set("start_at", start);
  if (end) params.set("end_at", end);
  if (filters.hasMedia) params.set("has_media", filters.hasMedia);
  if (filters.query) params.set("q", filters.query);
  if (eventId) params.set("event", eventId);
  const query = params.toString();
  return `/mx${query ? `?${query}` : ""}`;
}

function writeLocation(filters: Filters, eventId: string | null, replace = false): void {
  window.history[replace ? "replaceState" : "pushState"]({}, "", searchFor(filters, eventId));
}

async function requestPage(filters: Filters, cursor?: string, signal?: AbortSignal): Promise<MxEventPage> {
  const params = new URLSearchParams(searchFor(filters).slice(4));
  params.set("limit", "50");
  if (cursor) params.set("cursor", cursor);
  const response = await fetch(`/api/mx/events?${params.toString()}`, { signal });
  if (!response.ok) {
    const fallback = response.status === 409 ? "MX 资讯游标已失效，请刷新列表" : "MX 资讯暂不可读取";
    throw new PageRequestError(response.status, await boundedApiError(response, fallback));
  }
  return parseMxEventPage(await response.json());
}

function timestamp(value: string | null): string {
  return value ? value.replace("T", " ").replace(/([+-]\d{2}:\d{2}|Z)$/, "") : "暂无";
}

function displayError(error: unknown, fallback: string): string {
  return error instanceof Error && /[\u3400-\u9fff]/.test(error.message) ? error.message.slice(0, 120) : fallback;
}

export default function MxInformationFeed() {
  const initial = useRef(readLocation()).current;
  const [filters, setFilters] = useState<Filters>(initial.filters);
  const [ridInput, setRidInput] = useState(initial.filters.rids.join(","));
  const [draftAuthorization, setDraftAuthorization] = useState<AuthorizationFilter>(initial.filters.authorization);
  const [draftStartAt, setDraftStartAt] = useState(initial.filters.startAt);
  const [draftEndAt, setDraftEndAt] = useState(initial.filters.endAt);
  const [draftHasMedia, setDraftHasMedia] = useState<MediaFilter>(initial.filters.hasMedia);
  const [draftQuery, setDraftQuery] = useState(initial.filters.query);
  const [formError, setFormError] = useState<string | null>(null);
  const [urlNotice, setUrlNotice] = useState(initial.invalid ? "URL 筛选条件无效，已安全重置" : null);
  const [events, setEvents] = useState<MxEventSummary[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [appendLoading, setAppendLoading] = useState(false);
  const [appendError, setAppendError] = useState<string | null>(null);
  const [firstLoaded, setFirstLoaded] = useState(false);
  const [loadedOlder, setLoadedOlder] = useState(false);
  const [newCount, setNewCount] = useState(0);
  const [headError, setHeadError] = useState<string | null>(null);
  const [selectedEventId, setSelectedEventId] = useState<string | null>(initial.eventId);
  const [detail, setDetail] = useState<MxEventDetailPayload | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [failedMedia, setFailedMedia] = useState<Set<string>>(new Set());
  const mounted = useRef(true);
  const requestNumber = useRef(0);
  const currentFilterKey = filterKey(filters);

  const loadFirst = useCallback(async (nextFilters: Filters, signal?: AbortSignal): Promise<boolean> => {
    const requestId = ++requestNumber.current;
    setLoading(true);
    setLoadError(null);
    setAppendError(null);
    try {
      const page = await requestPage(nextFilters, undefined, signal);
      if (!mounted.current || requestId !== requestNumber.current) return false;
      setEvents(page.events);
      setNextCursor(page.next_cursor);
      setFirstLoaded(true);
      setLoadedOlder(false);
      setNewCount(0);
      setHeadError(null);
      return true;
    } catch (caught) {
      if (caught instanceof DOMException && caught.name === "AbortError") return false;
      if (!mounted.current || requestId !== requestNumber.current) return false;
      setEvents([]);
      setNextCursor(null);
      setFirstLoaded(true);
      setLoadError(displayError(caught, "MX 资讯暂不可读取"));
      return false;
    } finally {
      if (mounted.current && requestId === requestNumber.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    if (initial.invalid) writeLocation(initial.filters, null, true);
    return () => { mounted.current = false; };
  }, [initial]);

  useEffect(() => {
    setRidInput(filters.rids.join(","));
    setDraftAuthorization(filters.authorization);
    setDraftStartAt(filters.startAt);
    setDraftEndAt(filters.endAt);
    setDraftHasMedia(filters.hasMedia);
    setDraftQuery(filters.query);
  }, [currentFilterKey]);

  useEffect(() => {
    const controller = new AbortController();
    void loadFirst(filters, controller.signal);
    return () => controller.abort();
  }, [currentFilterKey, filters, loadFirst]);

  useEffect(() => {
    const onPopState = () => {
      const next = readLocation();
      setFilters(next.filters);
      setSelectedEventId(next.eventId);
      setUrlNotice(next.invalid ? "URL 筛选条件无效，已安全重置" : null);
      if (next.invalid) writeLocation(next.filters, null, true);
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const eventIds = useMemo(() => new Set(events.map((event) => event.event_id)), [events]);
  useEffect(() => {
    if (!firstLoaded || loadedOlder || loading) return;
    let cancelled = false;
    const checkHead = async () => {
      if (document.visibilityState !== "visible") return;
      try {
        const page = await requestPage(filters);
        if (cancelled) return;
        const count = page.events.filter((event) => !eventIds.has(event.event_id)).length;
        setNewCount(count);
        setHeadError(null);
      } catch (caught) {
        if (!cancelled && !(caught instanceof DOMException && caught.name === "AbortError")) {
          setHeadError("检查新资讯失败，不会改变当前阅读列表");
        }
      }
    };
    const onVisibilityChange = () => { if (document.visibilityState === "visible") void checkHead(); };
    const interval = window.setInterval(() => void checkHead(), 5_000);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [currentFilterKey, eventIds, filters, firstLoaded, loadedOlder, loading]);

  useEffect(() => {
    if (!selectedEventId) {
      setDetail(null);
      setDetailError(null);
      setFailedMedia(new Set());
      return;
    }
    const controller = new AbortController();
    setDetail(null);
    setDetailError(null);
    setDetailLoading(true);
    setFailedMedia(new Set());
    void (async () => {
      try {
        const response = await fetch(`/api/mx/events/${encodeURIComponent(selectedEventId)}`, { signal: controller.signal });
        if (!response.ok) throw new Error(await boundedApiError(response, "MX 资讯详情暂不可读取"));
        const payload = parseMxEventDetail(await response.json());
        if (mounted.current) setDetail(payload);
      } catch (caught) {
        if (!(caught instanceof DOMException && caught.name === "AbortError") && mounted.current) {
          setDetailError(displayError(caught, "MX 资讯详情暂不可读取"));
        }
      } finally {
        if (mounted.current) setDetailLoading(false);
      }
    })();
    return () => controller.abort();
  }, [selectedEventId]);

  function applyFilters(event: FormEvent) {
    event.preventDefault();
    const rids = parseRidList(ridInput);
    if (rids === null) {
      setFormError("RID 筛选必须是最多 100 个、以逗号分隔的正整数");
      return;
    }
    if ((draftStartAt && !apiTimestamp(draftStartAt)) || (draftEndAt && !apiTimestamp(draftEndAt)) || (draftStartAt && draftEndAt && new Date(draftStartAt) > new Date(draftEndAt))) {
      setFormError("接收时间范围无效");
      return;
    }
    if (draftQuery.length > 160 || /https?:\/\//i.test(draftQuery)) {
      setFormError("关键词最多 160 个字符，且不能是链接");
      return;
    }
    const next: Filters = { rids, authorization: draftAuthorization, startAt: draftStartAt, endAt: draftEndAt, hasMedia: draftHasMedia, query: draftQuery.trim() };
    setFormError(null);
    setFilters(next);
    setSelectedEventId(null);
    setUrlNotice(null);
    writeLocation(next, null);
  }

  function clearFilters() {
    setFilters(EMPTY_FILTERS);
    setSelectedEventId(null);
    setFormError(null);
    setUrlNotice(null);
    writeLocation(EMPTY_FILTERS, null);
  }

  async function loadMore() {
    if (!nextCursor || appendLoading) return;
    setAppendLoading(true);
    setAppendError(null);
    try {
      const page = await requestPage(filters, nextCursor);
      setEvents((current) => {
        const seen = new Set(current.map((event) => event.event_id));
        return [...current, ...page.events.filter((event) => !seen.has(event.event_id))];
      });
      setNextCursor(page.next_cursor);
      setLoadedOlder(true);
    } catch (caught) {
      setAppendError(displayError(caught, "加载更早资讯失败"));
    } finally {
      setAppendLoading(false);
    }
  }

  function openEvent(eventId: string) {
    setSelectedEventId(eventId);
    writeLocation(filters, eventId);
  }

  function closeDetail() {
    setSelectedEventId(null);
    writeLocation(filters, null);
  }

  return <section className="mx-information-section" aria-label="MX 历史资讯">
    <div className="mx-information-heading"><div><p className="eyebrow">只读、规范化的本地 Accepted Events</p><h2>MX 历史资讯</h2></div><button type="button" className="secondary-button" onClick={() => void loadFirst(filters)} disabled={loading}><RefreshCw className={loading ? "spin" : undefined} size={16} />刷新列表</button></div>
    <form className="mx-filter-form" onSubmit={applyFilters} aria-label="MX 资讯筛选">
      <label>RID（逗号分隔）<input aria-label="RID 筛选" value={ridInput} onChange={(event) => setRidInput(event.target.value)} inputMode="numeric" maxLength={1700} placeholder="例如：20025, 23200" /></label>
      <label>授权状态<select aria-label="授权状态" value={draftAuthorization} onChange={(event) => setDraftAuthorization(event.target.value as AuthorizationFilter)}><option value="">全部</option><option value="current">当前 RID</option><option value="revoked">历史 RID</option></select></label>
      <label>接收时间开始<input aria-label="接收时间开始" type="datetime-local" value={draftStartAt} onChange={(event) => setDraftStartAt(event.target.value)} /></label>
      <label>接收时间结束<input aria-label="接收时间结束" type="datetime-local" value={draftEndAt} onChange={(event) => setDraftEndAt(event.target.value)} /></label>
      <label>图片<select aria-label="图片筛选" value={draftHasMedia} onChange={(event) => setDraftHasMedia(event.target.value as MediaFilter)}><option value="">全部</option><option value="true">有图片</option><option value="false">无图片</option></select></label>
      <label>规范化正文关键词<input aria-label="正文关键词" value={draftQuery} onChange={(event) => setDraftQuery(event.target.value)} maxLength={160} /></label>
      <div className="mx-filter-actions"><button type="submit"><Filter size={16} />应用筛选</button><button type="button" className="secondary-button" onClick={clearFilters}>清除筛选</button></div>
      {formError ? <p role="alert" className="mx-inline-error">{formError}</p> : null}
    </form>
    {urlNotice ? <p className="mx-inline-error" role="status">{urlNotice}</p> : null}
    <div className={selectedEventId ? "mx-information-layout with-detail" : "mx-information-layout"}>
      <div className="mx-event-list-column">
        {newCount > 0 ? <button type="button" className="mx-new-events" onClick={() => void loadFirst(filters)}>发现 {newCount} 条新资讯，点击刷新到最新</button> : null}
        {headError ? <p className="mx-inline-error" role="status">{headError}</p> : null}
        {loading ? <p className="unavailable-message">正在读取 MX 资讯…</p> : null}
        {loadError ? <div className="mx-load-error" role="alert"><p>{loadError}</p><button type="button" onClick={() => void loadFirst(filters)}>重试读取</button></div> : null}
        {!loading && !loadError && events.length === 0 ? <p className="empty">没有符合当前筛选的资讯。</p> : null}
        <div className="mx-event-cards">
          {events.map((event) => <button type="button" className="mx-event-card" key={event.event_id} onClick={() => openEvent(event.event_id)}>
            <div className="mx-event-card-heading"><strong>RID {event.rid}</strong><span className={event.authorization === "current" ? "mx-current-rid" : "mx-revoked-rid"}>{event.authorization === "current" ? "当前 RID" : "历史 RID"}</span></div>
            <p>{event.summary || (event.media.has_media ? "图片资讯" : "暂无规范化正文")}</p>
            <div className="mx-event-card-meta"><span>来源 {timestamp(event.source_created_at)}</span><span>接收 {timestamp(event.received_at)}</span><span>{event.media.available_count > 0 ? `${event.media.available_count} 张图片` : event.media.pending_count > 0 ? "图片处理中" : "无图片"}</span></div>
          </button>)}
        </div>
        {appendError ? <p className="mx-inline-error" role="alert">{appendError}</p> : null}
        {nextCursor && !loadError ? <button type="button" className="secondary-button mx-load-more" onClick={() => void loadMore()} disabled={appendLoading}>{appendLoading ? "正在加载" : <><ChevronDown size={16} />加载更早资讯</>}</button> : null}
      </div>
      {selectedEventId ? <MxEventDetail detail={detail} loading={detailLoading} error={detailError} failedMedia={failedMedia} onMediaError={(mediaId) => setFailedMedia((current) => new Set(current).add(mediaId))} onClose={closeDetail} /> : null}
    </div>
  </section>;
}
