import { useCallback, useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { CurrentResearch, ResearchRequest, parseCurrentResearch, parseResearchRequestResponse, researchApiError } from "./contracts";

type Props = {
  refreshToken?: number;
  submittedRequests?: readonly ResearchRequest[];
  onTerminal?: (requestId: string) => void;
};

const STATE_LABEL: Record<CurrentResearch["service"]["state"], string> = {
  offline: "服务离线",
  starting: "服务启动中",
  idle: "服务空闲",
  running: "服务执行中",
  stopping: "服务停止中",
  degraded: "服务降级",
  failed: "服务失败",
};

const PHASE_LABEL: Record<ResearchRequest["phase"], string> = {
  queued: "等待队列", preflight: "运行前检查", snapshot: "封存数据快照", agents: "研究 Agent", decision: "形成结论", publishing: "发布报告", complete: "已结束", cancelled: "已取消",
};

function subjectLabel(request: ResearchRequest): string {
  return request.scope === "market" ? "沪深 A 股整体" : request.subject.code ?? "证券代码不可用";
}

export default function CurrentResearchQueue({ refreshToken = 0, submittedRequests = [], onTerminal }: Props) {
  const [current, setCurrent] = useState<CurrentResearch | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [cancelling, setCancelling] = useState<string | null>(null);
  const [terminal, setTerminal] = useState<ResearchRequest | null>(null);
  const trackedRequests = useRef(new Map<string, ResearchRequest>());
  const resolvingTerminals = useRef(new Set<string>());
  const onTerminalRef = useRef(onTerminal);

  useEffect(() => { onTerminalRef.current = onTerminal; }, [onTerminal]);
  useEffect(() => {
    submittedRequests.forEach((request) => trackedRequests.current.set(request.request_id, request));
  }, [submittedRequests]);

  const load = useCallback(async () => {
    try {
      const response = await fetch("/api/research/requests/current");
      if (!response.ok) throw new Error(await researchApiError(response, "当前研究状态暂不可读取"));
      const next = parseCurrentResearch(await response.json());
      setCurrent(next);
      const activeIds = new Set(next.requests.map((request) => request.request_id));
      next.requests.forEach((request) => trackedRequests.current.set(request.request_id, request));
      const completedIds = [...trackedRequests.current.keys()].filter(
        (requestId) => !activeIds.has(requestId) && !resolvingTerminals.current.has(requestId),
      );
      await Promise.all(completedIds.map(async (requestId) => {
        resolvingTerminals.current.add(requestId);
        try {
          const terminalResponse = await fetch(`/api/research/requests/${encodeURIComponent(requestId)}`);
          if (!terminalResponse.ok) return;
          const resolved = parseResearchRequestResponse(await terminalResponse.json());
          if (resolved.status === "queued" || resolved.status === "running") return;
          trackedRequests.current.delete(requestId);
          setTerminal(resolved);
          onTerminalRef.current?.(requestId);
        } finally {
          resolvingTerminals.current.delete(requestId);
        }
      }));
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "当前研究状态暂不可读取");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load, refreshToken]);
  useEffect(() => {
    const timer = window.setInterval(() => void load(), 5_000);
    return () => window.clearInterval(timer);
  }, [load]);

  async function cancel(request: ResearchRequest) {
    setCancelling(request.request_id);
    setMessage(null);
    try {
      const response = await fetch(`/api/research/requests/${encodeURIComponent(request.request_id)}/cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      if (!response.ok) throw new Error(await researchApiError(response, "取消研究请求失败"));
      await load();
      setMessage(request.status === "queued" ? "已取消等待中的研究请求。" : "已发送取消请求，将在安全阶段停止。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "取消研究请求失败");
    } finally {
      setCancelling(null);
    }
  }

  return <section className="panel current-research-queue" aria-label="当前研究">
    <div className="research-panel-heading"><h2>当前研究</h2><button type="button" className="icon-button" aria-label="刷新当前研究" title="刷新当前研究" onClick={() => void load()} disabled={loading || cancelling !== null}><RefreshCw className={loading ? "spin" : undefined} size={17} /></button></div>
    {error ? <p className="research-control-feedback error" role="alert">{error}</p> : null}
    {message ? <p className="research-control-feedback success" role="status">{message}</p> : null}
    {terminal ? <div className={`research-terminal-receipt ${terminal.status}`} role="status">
      <strong>{terminalStatusMessage(terminal)}</strong>
      <span>{terminal.team_ref} · {subjectLabel(terminal)}{terminal.reason_code ? ` · ${reasonLabel(terminal.reason_code)}` : ""}</span>
      {terminal.status === "passed" || terminal.status === "partial"
        ? <small>报告已同步到下方“研究报告”。</small>
        : <small>下方“研究报告”默认只显示已发布报告；请选择“全部终态”查看停止记录或再次研究。</small>}
    </div> : null}
    {current ? <p className={`research-service-state ${current.service.state}`}>{STATE_LABEL[current.service.state]}{current.service.heartbeat_at ? `；最近心跳 ${current.service.heartbeat_at}` : ""}</p> : null}
    {loading && current === null ? <p className="unavailable-message">正在读取当前研究…</p> : null}
    {current && current.requests.length === 0 ? <p className="empty">当前没有正在执行或等待执行的研究请求。</p> : null}
    {current?.requests.length ? <ol className="research-queue-list">{current.requests.map((request) => <li key={request.request_id} className={request.status === "running" ? "running" : "queued"}>
      <div><strong>{request.mode === "lagent" ? "LAgent" : request.team_ref}</strong><small>{request.scope === "market" ? "全市场" : "单只证券"} · {subjectLabel(request)} · 来源：{originLabel(request.origin)}</small></div>
      <div className="research-queue-meta"><span>{PHASE_LABEL[request.phase]}</span><span>{request.agents_total ? `Agent ${request.agents_completed}/${request.agents_total}` : "尚未开始"}</span><span>{request.decision_stage ?? ""}</span><span>{request.boundary_at ? `边界 ${request.boundary_at}` : `提交 ${request.requested_at}`}</span></div>
      {request.can_cancel ? <button type="button" className="secondary-button" onClick={() => void cancel(request)} disabled={cancelling !== null}>{cancelling === request.request_id ? "正在取消" : "取消"}</button> : null}
    </li>)}</ol> : null}
  </section>;
}

function originLabel(origin: ResearchRequest["origin"]): string {
  return { web: "WebUI", cli: "CLI", scheduled: "定时调度", legacy: "历史导入" }[origin];
}

function terminalStatusMessage(request: ResearchRequest): string {
  return {
    passed: "研究已完成，报告已发布。",
    partial: "研究已完成，已发布部分可用报告。",
    blocked: "研究已阻断，未生成报告。",
    failed: "研究失败，未生成报告。",
    cancelled: "研究已取消，未生成报告。",
    queued: "研究仍在等待。",
    running: "研究仍在执行。",
  }[request.status];
}

const REASON_LABEL: Record<string, string> = {
  preflight_failed: "运行前检查失败",
  snapshot_unavailable: "数据快照不可用",
  provider_unavailable: "数据提供方不可用",
  taxonomy_stale: "行业分类已过期",
  information_unavailable: "信息数据不可用",
  agent_timeout: "研究 Agent 超时",
  agent_invalid: "研究 Agent 输出无效",
  quality_blocked: "质量门禁未通过",
  pipeline_blocked: "决策流程已阻断",
  pipeline_failed: "决策流程失败",
  persistence_failed: "结果持久化失败",
  publication_failed: "报告发布失败",
  service_unavailable: "研究服务不可用",
  cancelled: "用户取消",
};

function reasonLabel(reason: string): string {
  return `${REASON_LABEL[reason] ?? "研究未完成"}（${reason}）`;
}
