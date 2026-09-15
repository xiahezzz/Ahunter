import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { Activity, Chrome, Pause, Play, RefreshCw, Save } from "lucide-react";
import MxInformationFeed from "./MxInformationFeed";
import {
  boundedApiError,
  MxListenerStatus,
  parseMxListenerStatus,
  parseRidAuthorization,
  RidAuthorization,
} from "./contracts";

type Feedback = { kind: "success" | "error"; text: string };

const RID_INPUT = /^[1-9][0-9]{0,15}$/;

function parseDraftRid(value: string): number | null {
  if (!RID_INPUT.test(value)) return null;
  const rid = Number(value);
  return Number.isSafeInteger(rid) && rid > 0 && rid <= 2 ** 53 - 1 ? rid : null;
}

function timestamp(value: string | null): string {
  return value ? value.replace("T", " ").replace(/([+-]\d{2}:\d{2}|Z)$/, "") : "暂无";
}

function readinessMessage(readiness: MxListenerStatus["readiness"]): string | null {
  if (readiness === "waiting_for_chrome") {
    return "点击“启动专用 Chrome”后，请在隔离窗口中自行登录并打开 MX 页面；系统不会代替你导航、登录或操作页面。";
  }
  if (readiness === "waiting_for_authorization") {
    return "请由用户在已打开的 Chrome 中手动登录并进入 MX 页面；本页面不会刷新、点击、输入或注入页面。";
  }
  return null;
}

function statusLabel(value: string): string {
  return {
    live: "活动",
    offline: "离线",
    healthy: "健康",
    degraded: "降级",
    failed: "失败",
    starting: "启动中",
    waiting_for_chrome: "等待 Chrome",
    waiting_for_authorization: "等待授权页面",
    connecting: "连接中",
    listening: "被动监听中",
    stopping: "停止中",
  }[value] ?? "未知";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseActionResult(value: unknown): { changed: boolean; service: MxListenerStatus } {
  if (!isRecord(value) || typeof value.changed !== "boolean" || typeof value.message !== "string") {
    throw new Error("MX Listener 操作响应格式无效");
  }
  return { changed: value.changed, service: parseMxListenerStatus(value.service) };
}

function parseChromeStartResult(value: unknown): { changed: boolean; ready: boolean; message: string } {
  if (
    !isRecord(value)
    || typeof value.changed !== "boolean"
    || typeof value.ready !== "boolean"
    || typeof value.message !== "string"
    || value.message.length > 120
  ) {
    throw new Error("专用 Chrome 启动响应格式无效");
  }
  return { changed: value.changed, ready: value.ready, message: value.message };
}

function parseRidSaveResult(value: unknown): RidAuthorization {
  if (!isRecord(value) || typeof value.message !== "string") throw new Error("RID 保存响应格式无效");
  return parseRidAuthorization({
    rids: value.rids,
    version: value.version,
    collection_enabled: value.collection_enabled,
  });
}

export default function MxOperationsPage() {
  const [listener, setListener] = useState<MxListenerStatus | null>(null);
  const [authorization, setAuthorization] = useState<RidAuthorization | null>(null);
  const [listenerError, setListenerError] = useState<string | null>(null);
  const [authorizationError, setAuthorizationError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [operation, setOperation] = useState<"chrome" | "start" | "stop" | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [draftRids, setDraftRids] = useState<number[]>([]);
  const [newRid, setNewRid] = useState("");
  const [draftDirty, setDraftDirty] = useState(false);
  const [savingRids, setSavingRids] = useState(false);
  const mounted = useRef(true);
  const draftDirtyRef = useRef(false);

  useEffect(() => { draftDirtyRef.current = draftDirty; }, [draftDirty]);

  const refresh = useCallback(async (signal?: AbortSignal, preserveDraft = true): Promise<boolean> => {
    setLoading(true);
    const [listenerResult, authorizationResult] = await Promise.allSettled([
      fetch("/api/mx/listener/status", { signal }),
      fetch("/api/mx/rids", { signal }),
    ]);
    let fresh = true;
    if (listenerResult.status === "fulfilled") {
      try {
        if (!listenerResult.value.ok) throw new Error(await boundedApiError(listenerResult.value, "MX Listener 状态暂不可读取"));
        const next = parseMxListenerStatus(await listenerResult.value.json());
        if (mounted.current) {
          setListener(next);
          setListenerError(null);
        }
      } catch {
        fresh = false;
        if (mounted.current) {
          setListener(null);
          setListenerError("MX Listener 状态暂不可读取");
        }
      }
    } else if (!(listenerResult.reason instanceof DOMException && listenerResult.reason.name === "AbortError")) {
      fresh = false;
      if (mounted.current) {
        setListener(null);
        setListenerError("MX Listener 状态暂不可读取");
      }
    } else {
      return false;
    }
    if (authorizationResult.status === "fulfilled") {
      try {
        if (!authorizationResult.value.ok) throw new Error(await boundedApiError(authorizationResult.value, "RID 授权配置暂不可读取"));
        const next = parseRidAuthorization(await authorizationResult.value.json());
        if (mounted.current) {
          setAuthorization(next);
          setAuthorizationError(null);
          if (!preserveDraft || !draftDirtyRef.current) setDraftRids(next.rids);
        }
      } catch {
        fresh = false;
        if (mounted.current) {
          setAuthorization(null);
          setAuthorizationError("RID 授权配置暂不可读取");
        }
      }
    } else if (!(authorizationResult.reason instanceof DOMException && authorizationResult.reason.name === "AbortError")) {
      fresh = false;
      if (mounted.current) {
        setAuthorization(null);
        setAuthorizationError("RID 授权配置暂不可读取");
      }
    } else {
      return false;
    }
    if (mounted.current) setLoading(false);
    return fresh;
  }, []);

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    void refresh(controller.signal, false);
    const interval = window.setInterval(() => void refresh(undefined, true), 5_000);
    return () => {
      mounted.current = false;
      controller.abort();
      window.clearInterval(interval);
    };
  }, [refresh]);

  async function runOperation(kind: "start" | "stop") {
    setFeedback(null);
    setOperation(kind);
    try {
      const response = await fetch(`/api/mx/listener/${kind}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      if (!response.ok) throw new Error(await boundedApiError(response, "MX Listener 操作失败"));
      const result = parseActionResult(await response.json());
      setListener(result.service);
      const refreshed = await refresh(undefined, true);
      setFeedback({
        kind: "success",
        text: refreshed
          ? (kind === "start" ? "已请求启动 MX Listener；状态已刷新" : "已请求停止 MX Listener；Chrome、RID 和历史资讯未改变")
          : (kind === "start" ? "已请求启动 MX Listener，但状态刷新失败，请刷新确认" : "已请求停止 MX Listener，但状态刷新失败，请刷新确认"),
      });
    } catch (caught) {
      const text = caught instanceof Error && /[\u3400-\u9fff]/.test(caught.message)
        ? caught.message.slice(0, 120)
        : "MX Listener 操作失败";
      setFeedback({ kind: "error", text });
    } finally {
      setOperation(null);
    }
  }

  async function startDedicatedChrome() {
    setFeedback(null);
    setOperation("chrome");
    try {
      const response = await fetch("/api/mx/chrome/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      if (!response.ok) throw new Error(await boundedApiError(response, "专用 Chrome 暂不可启动"));
      const result = parseChromeStartResult(await response.json());
      await refresh(undefined, true);
      setFeedback({ kind: "success", text: result.message });
    } catch (caught) {
      const text = caught instanceof Error && /[\u3400-\u9fff]/.test(caught.message)
        ? caught.message.slice(0, 120)
        : "专用 Chrome 暂不可启动";
      setFeedback({ kind: "error", text });
    } finally {
      setOperation(null);
    }
  }

  function addRid() {
    const parsed = parseDraftRid(newRid.trim());
    if (parsed === null) {
      setFeedback({ kind: "error", text: "RID 必须为正整数" });
      return;
    }
    if (draftRids.includes(parsed)) {
      setFeedback({ kind: "error", text: "该 RID 已在授权集合中" });
      return;
    }
    setDraftRids((current) => [...current, parsed].sort((left, right) => left - right));
    setNewRid("");
    draftDirtyRef.current = true;
    setDraftDirty(true);
    setFeedback(null);
  }

  function removeRid(rid: number) {
    setDraftRids((current) => current.filter((value) => value !== rid));
    draftDirtyRef.current = true;
    setDraftDirty(true);
    setFeedback(null);
  }

  async function saveRids(event: FormEvent) {
    event.preventDefault();
    if (!authorization) {
      setFeedback({ kind: "error", text: "RID 授权配置暂不可读取，不能提交" });
      return;
    }
    setSavingRids(true);
    setFeedback(null);
    try {
      const response = await fetch("/api/mx/rids", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rids: draftRids, version: authorization.version }),
      });
      if (response.status === 409) {
        throw new Error("RID 配置已更新，请刷新后比较并重新提交");
      }
      if (!response.ok) throw new Error(await boundedApiError(response, "RID 授权保存失败"));
      const saved = parseRidSaveResult(await response.json());
      setAuthorization(saved);
      setDraftRids(saved.rids);
      draftDirtyRef.current = false;
      setDraftDirty(false);
      setFeedback({ kind: "success", text: "RID 授权已保存，Listener 会在下一次配置轮询时热加载" });
    } catch (caught) {
      const text = caught instanceof Error && /[\u3400-\u9fff]/.test(caught.message)
        ? caught.message.slice(0, 120)
        : "RID 授权保存失败";
      setFeedback({ kind: "error", text });
    } finally {
      setSavingRids(false);
    }
  }

  return <div className="mx-page">
    <section className="mx-page-heading" aria-label="MX 监听">
      <div><p className="eyebrow">被动采集与本地资讯</p><h2><Activity size={18} />MX 监听</h2></div>
      <button type="button" className="icon-button" aria-label="刷新 MX 状态" title="刷新 MX 状态" onClick={() => void refresh(undefined, true)} disabled={loading || operation !== null || savingRids}>
        <RefreshCw className={loading ? "spin" : undefined} size={17} />
      </button>
    </section>

    <section className="mx-operations-grid">
      <article className="panel mx-listener-panel" aria-label="MX Listener 状态">
        <div className="panel-heading"><h2>MX Listener 状态</h2><span>{listener ? listener.status : "暂不可用"}</span></div>
        {listenerError ? <p className="unavailable-message" role="alert">{listenerError}</p> : null}
        {!listener && !listenerError ? <p className="unavailable-message">正在读取 MX Listener 状态…</p> : null}
        {listener ? <>
          <dl className="mx-status-grid">
            <div><dt>LaunchAgent</dt><dd>{listener.launchagent_loaded ? "已加载" : "未加载"}</dd></div>
            <div><dt>Liveness</dt><dd>{statusLabel(listener.liveness)}</dd></div>
            <div><dt>Readiness</dt><dd>{statusLabel(listener.readiness)}</dd></div>
            <div><dt>Health</dt><dd>{statusLabel(listener.health)}</dd></div>
            <div><dt>当前连接</dt><dd>{timestamp(listener.connected_at)}</dd></div>
            <div><dt>最后 frame</dt><dd>{timestamp(listener.last_frame_at)}</dd></div>
            <div><dt>最后 accepted event</dt><dd>{timestamp(listener.last_accepted_event_at)}</dd></div>
            <div><dt>租约到期</dt><dd>{timestamp(listener.lease_expires_at)}</dd></div>
            <div><dt>RID 数量</dt><dd>{listener.rid_count}</dd></div>
            <div><dt>采集</dt><dd>{listener.collection_enabled ? "已启用" : "已停用"}</dd></div>
          </dl>
          <p className="mx-status-detail">{listener.details}</p>
          {readinessMessage(listener.readiness) ? <p className="mx-manual-step">{readinessMessage(listener.readiness)}</p> : null}
        </> : null}
        <div className="mx-actions">
          <button type="button" className="secondary-button" onClick={() => void startDedicatedChrome()} disabled={operation !== null || savingRids}><Chrome size={16} />{operation === "chrome" ? "正在启动专用 Chrome" : "启动专用 Chrome"}</button>
          <button type="button" onClick={() => void runOperation("start")} disabled={operation !== null || savingRids}><Play size={16} />{operation === "start" ? "正在启动" : "启动 Listener"}</button>
          <button type="button" className="secondary-button" onClick={() => void runOperation("stop")} disabled={operation !== null || savingRids}><Pause size={16} />{operation === "stop" ? "正在停止" : "停止 Listener"}</button>
        </div>
      </article>

      <article className="panel mx-rid-panel" aria-label="RID 授权配置">
        <div className="panel-heading"><h2>RID Authorization Set</h2><span>{authorization?.collection_enabled ? "采集已启用" : "空集合会停用采集"}</span></div>
        {authorizationError ? <p className="unavailable-message" role="alert">{authorizationError}</p> : null}
        {!authorization && !authorizationError ? <p className="unavailable-message">正在读取 RID 授权配置…</p> : null}
        {authorization ? <form onSubmit={(event) => void saveRids(event)}>
          <p className="mx-rid-note">移除 RID 只会停止未来采集和研究使用，不会删除历史资讯或媒体。</p>
          <ul className="mx-rid-list" aria-label="已授权 RID">
            {draftRids.length === 0 ? <li className="empty">当前为空集合，采集会保持停用。</li> : draftRids.map((rid) => <li key={rid}><span>{rid}</span><button type="button" className="text-button" onClick={() => removeRid(rid)} disabled={savingRids || operation !== null}>移除</button></li>)}
          </ul>
          <div className="mx-add-rid"><label>新增 RID<input aria-label="新增 RID" inputMode="numeric" value={newRid} maxLength={16} onChange={(event) => setNewRid(event.target.value)} disabled={savingRids || operation !== null} /></label><button type="button" className="secondary-button" onClick={addRid} disabled={savingRids || operation !== null}>加入</button></div>
          <div className="mx-actions"><button type="submit" disabled={savingRids || operation !== null || !draftDirty}><Save size={16} />{savingRids ? "正在保存" : "保存完整授权集合"}</button><button type="button" className="secondary-button" onClick={() => { setDraftRids(authorization.rids); draftDirtyRef.current = false; setDraftDirty(false); setNewRid(""); setFeedback(null); }} disabled={savingRids || operation !== null || !draftDirty}>放弃草稿</button></div>
        </form> : null}
      </article>
    </section>
    {feedback ? <p className={`mx-feedback ${feedback.kind}`} role={feedback.kind === "error" ? "alert" : "status"}>{feedback.text}</p> : null}
    <MxInformationFeed />
  </div>;
}
