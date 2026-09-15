import { FormEvent, useEffect, useRef, useState } from "react";
import { newSubmissionIdentity, parseResearchRequestResponse, ResearchRequest, researchApiError, validResearchSecurityCode } from "./contracts";

type AgentConfig = { instructions: string; model: string | null; reasoning_effort: string | null; timeout_seconds: number; max_retries: number };
type Config = { main: AgentConfig; subagent: AgentConfig; allowed_products: string[] | null;
  mx_rids: number[] | null;
  prefetch_intraday: boolean;
  max_steps_per_agent: number | null; max_subagents: number | null; max_queries: number | null;
  max_result_rows: number | null; max_result_bytes: number | null; max_run_seconds: number | null };
type Settings = { version: number; config: Config };
type TraceEvent = { sequence: number; agent_id: string; parent_id: string | null; kind: string; status: string; created_at: string; detail: Record<string, unknown> };
type Trace = { request: ResearchRequest; task: string; config: Config; config_version: number; products: string[]; events: TraceEvent[]; next_after: number };
type RecordItem = { request_id: string; record_id: string; status: string; team_ref: string; mode?: string };

async function api(path: string, init?: RequestInit) {
  const response = await fetch(path, init);
  if (!response.ok) throw new Error(await researchApiError(response, "LAgent 操作失败"));
  return response.json();
}
const limits: { key: keyof Omit<Config, "main" | "subagent" | "allowed_products" | "mx_rids" | "prefetch_intraday">; label: string; min: number }[] = [
  { key: "max_steps_per_agent", label: "每个 agent 最大轮数", min: 1 },
  { key: "max_subagents", label: "最大委托数", min: 0 },
  { key: "max_queries", label: "总查询次数", min: 0 },
  { key: "max_result_rows", label: "每次查询最大行数", min: 1 },
  { key: "max_result_bytes", label: "每次查询最大字节数", min: 1 },
  { key: "max_run_seconds", label: "研究总时限（秒）", min: 10 },
];

export default function LAgentPanel({ refreshToken = 0, onSubmitted }: { refreshToken?: number; onSubmitted: (request: ResearchRequest) => void }) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [config, setConfig] = useState<Config | null>(null);
  const [scope, setScope] = useState("market");
  const [code, setCode] = useState("");
  const [task, setTask] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [requestId, setRequestId] = useState("");
  const [trace, setTrace] = useState<Trace | null>(null);
  const [records, setRecords] = useState<RecordItem[]>([]);
  const [report, setReport] = useState("");
  const dirty = useRef(false);
  const [productText, setProductText] = useState("");
  const [restricted, setRestricted] = useState(false);

  async function loadSettings() {
    try {
      const next: Settings = await api("/api/research/lagent/settings");
      if (!next.config?.main || !next.config?.subagent || typeof next.version !== "number") throw new Error("LAgent 配置格式无效");
      if (!dirty.current) {
        setSettings(next);
        setConfig(next.config); setRestricted(next.config.allowed_products !== null);
        setProductText(next.config.allowed_products?.join("\n") ?? "");
      }
    } catch (error) { setMessage(String(error)); }
  }
  useEffect(() => { void loadSettings(); }, [refreshToken]);
  useEffect(() => {
    let active = true;
    void api("/api/research/records?team_id=lagent&status=passed&status=partial&status=blocked&status=failed&status=cancelled&limit=100")
      .then((value) => { if (active) setRecords(value.records); }).catch(() => {});
    return () => { active = false; };
  }, [refreshToken, trace?.request.status]);
  useEffect(() => {
    if (!requestId) { setTrace(null); return; }
    let active = true;
    let cursor = 0;
    let events: TraceEvent[] = [];
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const next: Trace = await api(`/api/research/lagent/requests/${encodeURIComponent(requestId)}/trace?after=${cursor}&limit=100`);
        if (!active) return;
        cursor = next.next_after; events = [...events, ...next.events];
        setTrace({ ...next, events });
        if (next.events.length === 100 || ["queued", "running"].includes(next.request.status)) timer = setTimeout(() => void poll(), next.events.length === 100 ? 0 : 2000);
      } catch (error) { if (active) setMessage(String(error)); }
    }
    setTrace(null); setReport(""); void poll();
    return () => { active = false; clearTimeout(timer); };
  }, [requestId]);

  function change(next: Config) { dirty.current = true; setConfig(next); }
  async function save(event: FormEvent) {
    event.preventDefault(); if (!settings || !config) return;
    setBusy(true); setMessage("");
    try {
      const next = { ...config, allowed_products: restricted ? productText.split(/[\s,]+/).filter(Boolean) : null };
      const saved: Settings = await api("/api/research/lagent/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ expected_version: settings.version, config: next }) });
      dirty.current = false; setSettings(saved); setConfig(saved.config); setMessage(`LAgent 配置已发布：版本 ${saved.version}`);
    } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  }
  async function submit(event: FormEvent) {
    event.preventDefault(); if (!settings) return;
    if (dirty.current) { setMessage("请先发布配置，再提交研究。"); return; }
    if (scope === "security" && !validResearchSecurityCode(code)) { setMessage("请输入合法的六位沪深 A 股代码"); return; }
    setBusy(true); setMessage("");
    try {
      const value = await api("/api/research/lagent/requests", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope, code: scope === "security" ? code : null, task, expected_version: settings.version, submission_identity: newSubmissionIdentity("lagent") }) });
      const request = parseResearchRequestResponse(value); setRequestId(request.request_id); onSubmitted(request);
      setMessage(`已提交 LAgent 研究：${request.request_id}`);
    } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  }
  async function control(action: "cancel" | "rerun") {
    setBusy(true);
    try {
      const value = await api(`/api/research/requests/${encodeURIComponent(requestId)}/${action}`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(action === "rerun" ? { submission_identity: newSubmissionIdentity("lagent") } : {}) });
      const request = parseResearchRequestResponse(value);
      if (action === "rerun") { setRequestId(request.request_id); onSubmitted(request); }
      else setTrace((current) => current ? { ...current, request } : current);
    } catch (error) { setMessage(String(error)); } finally { setBusy(false); }
  }
  async function loadReport() {
    try {
      const recordId = `record-${requestId.replace(/^request-/, "")}`;
      const value = await api(`/api/research/records/${recordId}`); setReport(value.report?.markdown ?? "该研究没有可发布报告");
    } catch (error) { setMessage(String(error)); }
  }
  return <section className="panel lagent-panel" aria-label="LAgent 模式">
    <h2>LAgent 模式 <small>自由研究</small></h2>
    <p className="research-note">一个主 agent 自主调用数据产品、创建临时 subagent 并审查结果。配置按请求固定，所有调用及委托均可追溯。</p>
    {message && <p role="status">{message}</p>}
    {config && settings && <details><summary>配置 LAgent · 已发布版本 {settings.version}{dirty.current ? " · 有未发布修改" : ""}</summary>
      <form onSubmit={(event) => void save(event)}>
        <div className="research-control-grid">{(["main", "subagent"] as const).map((role) => <fieldset key={role} disabled={busy}>
          <legend>{role === "main" ? "主 agent" : "Subagent 默认配置"}</legend>
          <label>研究指令<textarea aria-label={`${role} 研究指令`} required maxLength={16000} rows={5} value={config[role].instructions} onChange={(event) => change({ ...config, [role]: { ...config[role], instructions: event.target.value } })} /></label>
          {(["model", "reasoning_effort"] as const).map((key) => <label key={key}>{key === "model" ? "模型（留空继承执行设置）" : "推理强度（留空继承）"}<input aria-label={`${role} ${key}`} value={config[role][key] ?? ""} onChange={(event) => change({ ...config, [role]: { ...config[role], [key]: event.target.value || null } })} /></label>)}
          <label>单轮超时（秒）<input type="number" min={10} max={3600} required value={config[role].timeout_seconds} onChange={(event) => change({ ...config, [role]: { ...config[role], timeout_seconds: Number(event.target.value) } })} /></label>
          <label>失败重试次数<select value={config[role].max_retries} onChange={(event) => change({ ...config, [role]: { ...config[role], max_retries: Number(event.target.value) } })}><option value={0}>0</option><option value={1}>1</option></select></label>
        </fieldset>)}</div>
        <fieldset disabled={busy}><legend>数据与执行限制</legend>
          <label><input type="checkbox" checked={config.prefetch_intraday} onChange={(event) => change({ ...config, prefetch_intraday: event.target.checked })} />开始时封存盘中快照，供后续按需使用</label>
          <label><input type="checkbox" checked={restricted} onChange={(event) => { setRestricted(event.target.checked); dirty.current = true; }} />限制可选数据产品（默认可调用全部产品）</label>
          {restricted && <label>产品版本，每行一个；空列表禁用数据访问<textarea aria-label="LAgent 数据产品" value={productText} rows={4} onChange={(event) => { setProductText(event.target.value); dirty.current = true; }} placeholder="whole_market_daily_history@1" /></label>}
          <p>以下限制留空表示不限。委托按主 agent 的选择依次执行，子 agent 的数量和职责无预设名单。</p>
          <label>MX RID 范围（留空继承提交时的已授权集合）<input aria-label="LAgent MX RID 范围" value={config.mx_rids?.join(",") ?? ""} onChange={(event) => change({ ...config, mx_rids: event.target.value.trim() ? event.target.value.split(/[\s,]+/).filter(Boolean).map(Number) : null })} /></label>
          <label><input type="checkbox" checked={Array.isArray(config.mx_rids) && config.mx_rids.length === 0} onChange={(event) => change({ ...config, mx_rids: event.target.checked ? [] : null })} />禁用 MX Feed 访问</label>
          <div className="lagent-limits">{limits.map(({ key, label, min }) => <label key={key}>{label}<input type="number" min={min} value={config[key] ?? ""} placeholder="不限" onChange={(event) => change({ ...config, [key]: event.target.value === "" ? null : Number(event.target.value) })} /></label>)}</div>
        </fieldset>
        <button type="submit" disabled={busy}>发布 LAgent 配置</button>
        <button type="button" disabled={busy} onClick={() => { if (!dirty.current || window.confirm("放弃未发布修改并重新读取配置？")) { dirty.current = false; void loadSettings(); } }}>重新读取配置</button>
      </form>
    </details>}
    <form onSubmit={(event) => void submit(event)}>
      <label>研究任务<textarea aria-label="LAgent 研究任务" required maxLength={16000} rows={3} value={task} onChange={(event) => setTask(event.target.value)} placeholder="描述希望解决的研究问题" /></label>
      <label>研究范围<select aria-label="LAgent 研究范围" value={scope} onChange={(event) => setScope(event.target.value)}><option value="market">全市场</option><option value="security">单只证券</option></select></label>
      {scope === "security" && <label>证券代码<input aria-label="LAgent 证券代码" value={code} onChange={(event) => setCode(event.target.value)} maxLength={6} required /></label>}
      <button type="submit" disabled={busy || !settings}>提交 LAgent 研究</button>
    </form>
    <details open={Boolean(trace)}><summary>运行观测与历史</summary>
      <label>请求编号<input aria-label="LAgent 请求编号" value={requestId} onChange={(event) => setRequestId(event.target.value)} placeholder="request-…" /></label>
      <label>历史研究<select aria-label="LAgent 历史研究" value={records.some((item) => item.request_id === requestId) ? requestId : ""} onChange={(event) => setRequestId(event.target.value)}><option value="">选择历史请求</option>{records.map((item) => <option key={item.request_id} value={item.request_id}>{item.request_id} · {item.status}</option>)}</select></label>
      {trace && <><p>状态：{trace.request.status} · 阶段：{trace.request.phase} · 配置版本：{trace.config_version} · 已记录事件：{trace.events.length}</p><p>{trace.task}</p>
        {trace.request.can_cancel && <button disabled={busy} onClick={() => void control("cancel")}>取消 LAgent 研究</button>}
        {trace.request.can_rerun && <button disabled={busy} onClick={() => void control("rerun")}>使用固定配置重跑</button>}
        {["passed", "partial"].includes(trace.request.status) && <button onClick={() => void loadReport()}>查看 LAgent 报告</button>}
        <details><summary>本次配置与可用产品</summary><pre>{JSON.stringify({ config: trace.config, products: trace.products }, null, 2)}</pre></details>
        <ol className="lagent-events">{trace.events.map((event) => <li key={event.sequence}>
          <details><summary>{event.parent_id ? "↳ " : ""}{event.agent_id} · {event.kind} · {event.status} · {event.created_at}</summary><pre>{JSON.stringify(event.detail, null, 2)}</pre>
            {[event.detail.artifact_hash, event.detail.capsule_hash].filter((hash): hash is string => typeof hash === "string" && /^[0-9a-f]{64}$/.test(hash)).map((hash) => <a key={hash} href={`/api/research/lagent/requests/${encodeURIComponent(requestId)}/artifacts/${hash}`} download>下载完整调用输入／结果</a>)}
          </details>
        </li>)}</ol>
      </>}
      {report && <article aria-label="LAgent 研究报告" className="lagent-report">{report}</article>}
    </details>
  </section>;
}
