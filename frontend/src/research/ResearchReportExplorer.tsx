import { useCallback, useEffect, useMemo, useState } from "react";
import { FileSearch, RefreshCw } from "lucide-react";
import {
  ResearchRecord,
  ResearchRecordDetail,
  ResearchRequest,
  ResearchTeamGroup,
  newSubmissionIdentity,
  parseResearchRecordDetail,
  parseResearchRecords,
  parseResearchRequestResponse,
  parseResearchTeamDirectory,
  researchApiError,
} from "./contracts";

type Props = { refreshToken?: number; onRerun?: (request: ResearchRequest) => void };
type FilterStatus = "default" | "all" | "blocked" | "failed" | "cancelled";
const PAGE_SIZE = 20;

function initialExplorerState(): { teamId: string; teamRef: string; status: FilterStatus; offset: number; recordId: string | null } {
  if (typeof window === "undefined") return { teamId: "", teamRef: "", status: "default", offset: 0, recordId: null };
  const query = new URLSearchParams(window.location.search);
  const rawStatus = query.get("research_status");
  const status: FilterStatus = rawStatus === "all" || rawStatus === "blocked" || rawStatus === "failed" || rawStatus === "cancelled" ? rawStatus : "default";
  const rawOffset = query.get("research_offset") ?? "0";
  const offset = /^\d+$/.test(rawOffset) ? Math.min(Number(rawOffset), 1_000_000) : 0;
  return {
    teamId: query.get("research_team_id") ?? "",
    teamRef: query.get("research_team_ref") ?? "",
    status,
    offset: Number.isSafeInteger(offset) ? offset : 0,
    recordId: query.get("research_record_id"),
  };
}

export default function ResearchReportExplorer({ refreshToken = 0, onRerun }: Props) {
  const initial = initialExplorerState();
  const [teams, setTeams] = useState<ResearchTeamGroup[]>([]);
  const [teamId, setTeamId] = useState(initial.teamId);
  const [teamRef, setTeamRef] = useState(initial.teamRef);
  const [status, setStatus] = useState<FilterStatus>(initial.status);
  const [offset, setOffset] = useState(initial.offset);
  const [records, setRecords] = useState<ResearchRecord[]>([]);
  const [detail, setDetail] = useState<ResearchRecordDetail | null>(null);
  const [detailRecordId, setDetailRecordId] = useState<string | null>(initial.recordId);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [rerunning, setRerunning] = useState(false);
  const [rerunMessage, setRerunMessage] = useState<string | null>(null);

  const versions = useMemo(() => teams.find((team) => team.team_id === teamId)?.history ?? [], [teamId, teams]);

  const loadTeams = useCallback(async () => {
    const response = await fetch("/api/research/teams");
    if (!response.ok) throw new Error(await researchApiError(response, "暂时无法读取 Team 筛选项"));
    setTeams(parseResearchTeamDirectory(await response.json()));
  }, []);

  const loadRecords = useCallback(async () => {
    setLoading(true);
    try {
      const query = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
      if (teamId) query.set("team_id", teamId);
      if (teamRef) query.set("team_ref", teamRef);
      const statuses = status === "all" ? ["passed", "partial", "blocked", "failed", "cancelled"] : status === "default" ? [] : [status];
      statuses.forEach((item) => query.append("status", item));
      const response = await fetch(`/api/research/records?${query.toString()}`);
      if (!response.ok) throw new Error(await researchApiError(response, "研究报告列表暂不可读取"));
      setRecords(parseResearchRecords(await response.json()).records);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "研究报告列表暂不可读取");
      setRecords([]);
    } finally {
      setLoading(false);
    }
  }, [offset, status, teamId, teamRef]);

  useEffect(() => { void loadTeams().catch((caught) => setError(caught instanceof Error ? caught.message : "暂时无法读取 Team 筛选项")); }, [loadTeams, refreshToken]);
  useEffect(() => { void loadRecords(); }, [loadRecords, refreshToken]);
  useEffect(() => {
    if (!detailRecordId || detail?.record_id === detailRecordId) return;
    void openDetail(detailRecordId);
  }, [detail?.record_id, detailRecordId]);
  useEffect(() => {
    if (typeof window === "undefined") return;
    const query = new URLSearchParams(window.location.search);
    const values: Array<[string, string | null]> = [
      ["research_team_id", teamId || null],
      ["research_team_ref", teamRef || null],
      ["research_status", status === "default" ? null : status],
      ["research_offset", offset ? String(offset) : null],
      ["research_record_id", detailRecordId],
    ];
    values.forEach(([key, value]) => value ? query.set(key, value) : query.delete(key));
    const next = `${window.location.pathname}${query.size ? `?${query.toString()}` : ""}${window.location.hash}`;
    window.history.replaceState(null, "", next);
  }, [detailRecordId, offset, status, teamId, teamRef]);

  async function openDetail(recordId: string) {
    setDetailRecordId(recordId);
    try {
      const response = await fetch(`/api/research/records/${encodeURIComponent(recordId)}`);
      if (!response.ok) throw new Error(await researchApiError(response, "研究报告详情暂不可读取"));
      setDetail(parseResearchRecordDetail(await response.json()));
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "研究报告详情暂不可读取");
    }
  }

  async function rerun(record: ResearchRecord | ResearchRecordDetail) {
    setRerunning(true);
    setRerunMessage(null);
    try {
      const response = await fetch(`/api/research/requests/${encodeURIComponent(record.request_id)}/rerun`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ submission_identity: newSubmissionIdentity("rerun") }),
      });
      if (!response.ok) throw new Error(await researchApiError(response, "再次研究提交失败"));
      const request = parseResearchRequestResponse(await response.json());
      setError(null);
      setDetail(null);
      setDetailRecordId(null);
      setOffset(0);
      setRerunMessage(`已创建新的研究请求 ${request.request_id}；可在“当前研究”查看进度。`);
      onRerun?.(request);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "再次研究提交失败");
    } finally {
      setRerunning(false);
    }
  }

  function changeTeamId(next: string) {
    setTeamId(next);
    setTeamRef("");
    setOffset(0);
    setDetail(null);
  }

  return <section className="panel research-report-explorer" aria-label="研究报告">
    <div className="research-panel-heading"><h2><FileSearch size={18} />研究报告</h2><button type="button" className="icon-button" aria-label="刷新研究报告" title="刷新研究报告" onClick={() => void loadRecords()} disabled={loading}><RefreshCw className={loading ? "spin" : undefined} size={17} /></button></div>
    <p className="research-note">默认只显示已通过和部分可用的报告；已阻断、失败或取消的研究不会生成报告，请选择“全部终态”查看停止原因。列表按发布时间从新到旧分页读取。</p>
    <div className="research-report-filters">
      <label>Team<select aria-label="按 Team 筛选" value={teamId} onChange={(event) => changeTeamId(event.target.value)}><option value="">全部 Team</option>{teams.map((team) => <option key={team.team_id} value={team.team_id}>{team.latest.title} · {team.team_id}</option>)}</select></label>
      <label>精确版本<select aria-label="按精确版本筛选" value={teamRef} onChange={(event) => { setTeamRef(event.target.value); setOffset(0); setDetail(null); }} disabled={!teamId}><option value="">全部版本</option>{versions.map((version) => <option key={version.team_ref} value={version.team_ref}>{version.team_ref}</option>)}</select></label>
      <label>状态<select aria-label="按状态筛选" value={status} onChange={(event) => { setStatus(event.target.value as FilterStatus); setOffset(0); setDetail(null); }}><option value="default">已通过与部分可用</option><option value="all">全部终态</option><option value="blocked">仅已阻断</option><option value="failed">仅失败</option><option value="cancelled">仅已取消</option></select></label>
    </div>
    {error ? <p className="research-control-feedback error" role="alert">{error}</p> : null}
    {rerunMessage ? <p className="research-control-feedback" role="status">{rerunMessage}</p> : null}
    {loading ? <p className="unavailable-message">正在读取研究报告…</p> : null}
    {!loading && records.length === 0 ? <p className="empty">没有符合条件的研究记录。</p> : null}
    {!loading && records.length ? <div className="research-record-table" role="list">{records.map((record) => <article key={record.record_id} role="listitem">
      <button type="button" className="research-record-open" onClick={() => void openDetail(record.record_id)}><strong>{record.team_ref}</strong><span>{scopeLabel(record)} · {subjectLabel(record)} · {statusLabel(record.status)} · {qualitySummaryLabel(record)}</span><small>来源：{originLabel(record.origin)} · 请求：{record.requested_at} · 边界：{record.boundary_at ?? "未形成"} · 发布：{record.published_at ?? "无报告"}</small>{!record.has_report ? <small>停止：{record.phase} · {record.reason_code ?? "无"}</small> : null}</button>
      <button type="button" className="secondary-button" onClick={() => void rerun(record)} disabled={rerunning}>{rerunning ? "正在提交" : "再次研究"}</button>
    </article>)}</div> : null}
    <div className="research-pagination"><button type="button" className="secondary-button" onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))} disabled={loading || offset === 0}>上一页</button><span>第 {Math.floor(offset / PAGE_SIZE) + 1} 页</span><button type="button" className="secondary-button" onClick={() => setOffset((value) => value + PAGE_SIZE)} disabled={loading || records.length < PAGE_SIZE}>下一页</button></div>
    {detail ? <RecordDetail detail={detail} rerunning={rerunning} onClose={() => { setDetail(null); setDetailRecordId(null); }} onRerun={() => void rerun(detail)} /> : null}
  </section>;
}

function RecordDetail({ detail, rerunning, onClose, onRerun }: { detail: ResearchRecordDetail; rerunning: boolean; onClose: () => void; onRerun: () => void }) {
  return <article className="research-record-detail" aria-label="研究报告详情">
    <div className="research-panel-heading"><h3>{detail.team_ref}</h3><button type="button" className="secondary-button" onClick={onClose}>关闭详情</button></div>
    <p>{scopeLabel(detail)} · {subjectLabel(detail)} · {statusLabel(detail.status)}</p>
    <dl className="research-detail-meta"><div><dt>请求时间</dt><dd>{detail.requested_at}</dd></div><div><dt>受理时间</dt><dd>{detail.accepted_at}</dd></div><div><dt>研究边界</dt><dd>{detail.boundary_at ?? "未形成"}</dd></div><div><dt>发布时间</dt><dd>{detail.published_at ?? "未发布"}</dd></div><div><dt>来源</dt><dd>{originLabel(detail.origin)}</dd></div><div><dt>停止原因</dt><dd>{detail.reason_code ?? "无"}</dd></div></dl>
    {detail.report ? <><SafeReportSummary report={detail.report.json} /><SafeMarkdown markdown={detail.report.markdown} /></> : <p className="unavailable-message">该终态没有可安全展示的 Team Report。</p>}
    <button type="button" onClick={onRerun} disabled={rerunning}>{rerunning ? "正在提交再次研究" : "再次研究"}</button>
  </article>;
}

function SafeReportSummary({ report }: { report: Record<string, unknown> }) {
  const quality = evidenceQuality(report.evidence_quality);
  const risks = safeTextList(report.risks, 20);
  const invalidation = safeTextList(report.invalidation_conditions, 20);
  const blocked = blockedInsights(report.insights);
  const provenance = safeProvenance(report.provenance);
  if (!quality && !risks.length && !invalidation.length && !blocked.length && !provenance.length) return null;
  return <section className="research-report-summary" aria-label="报告结构化摘要">
    <h4>可核验摘要</h4>
    {quality ? <div><strong>证据质量：</strong><span>{quality.status}</span>{quality.checks.length ? <span> · 校验：{quality.checks.join("、")}</span> : null}{quality.limitations.length ? <span> · 限制：{quality.limitations.join("、")}</span> : null}</div> : null}
    {risks.length ? <SummaryList title="风险" values={risks} /> : null}
    {invalidation.length ? <SummaryList title="失效条件" values={invalidation} /> : null}
    {blocked.length ? <div><strong>未发布 Insight：</strong><ul>{blocked.map((item) => <li key={item.insightId}>{item.insightId} · {item.reasonCode}</li>)}</ul></div> : null}
    {provenance.length ? <div><strong>溯源摘要：</strong><ul>{provenance.map((item) => <li key={item}>{item}</li>)}</ul></div> : null}
  </section>;
}

function SummaryList({ title, values }: { title: string; values: string[] }) {
  return <div><strong>{title}：</strong><ul>{values.map((value) => <li key={value}>{value}</li>)}</ul></div>;
}

function SafeMarkdown({ markdown }: { markdown: string }) {
  const lines = markdown.replace(/\r\n/g, "\n").split("\n").slice(0, 10_000);
  return <div className="safe-markdown">{lines.map((line, index) => {
    const text = line.replace(/<[^>]*>/g, "");
    if (text.startsWith("### ")) return <h5 key={index}>{text.slice(4)}</h5>;
    if (text.startsWith("## ")) return <h4 key={index}>{text.slice(3)}</h4>;
    if (text.startsWith("# ")) return <h3 key={index}>{text.slice(2)}</h3>;
    if (text.startsWith("- ")) return <p key={index} className="markdown-list-item">{text.slice(2)}</p>;
    return text ? <p key={index}>{text}</p> : <br key={index} />;
  })}</div>;
}

const SAFE_DETAIL_TEXT = /^(?!.*(?:https?:\/\/|<[^>]*>|(?:^|[\\/])(?:Users|tmp|data|var)(?:[\\/])|(?:endpoint|cookie|raw[_ -]?payload|source[_ -]?url)\b)).{1,600}$/i;
const SAFE_REPORT_REF = /^[a-z][a-z0-9_-]{1,63}@[1-9][0-9]*$/;
const SAFE_REASON = /^[a-z][a-z0-9_]{1,79}$/;
const SAFE_HASH = /^[0-9a-f]{64}$/;
const SAFE_SNAPSHOT_ID = /^snapshot-[a-z0-9-]{1,80}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function safeDetailText(value: unknown): value is string {
  return typeof value === "string" && SAFE_DETAIL_TEXT.test(value.trim());
}

function safeTextList(value: unknown, maximum: number): string[] {
  if (!Array.isArray(value) || value.length > maximum) return [];
  return value.filter(safeDetailText).slice(0, maximum);
}

function evidenceQuality(value: unknown): { status: string; checks: string[]; limitations: string[] } | null {
  if (!isRecord(value) || !["passed", "warning", "blocked"].includes(String(value.status))) return null;
  return {
    status: String(value.status),
    checks: safeTextList(value.checks, 20),
    limitations: safeTextList(value.limitations, 20),
  };
}

function blockedInsights(value: unknown): Array<{ insightId: string; reasonCode: string }> {
  if (!Array.isArray(value) || value.length > 20) return [];
  const ids = new Set(["breadth_sentiment", "sector_rotation", "macro_policy"]);
  return value.flatMap((item) => {
    if (!isRecord(item) || item.status !== "blocked" || typeof item.insight_id !== "string" || !ids.has(item.insight_id) || typeof item.reason_code !== "string" || !SAFE_REASON.test(item.reason_code)) return [];
    return [{ insightId: item.insight_id, reasonCode: item.reason_code }];
  });
}

function safeProvenance(value: unknown): string[] {
  if (!isRecord(value)) return [];
  const entries: string[] = [];
  if (typeof value.pipeline === "string" && SAFE_REPORT_REF.test(value.pipeline)) entries.push(`流程：${value.pipeline}`);
  if (typeof value.snapshot_id === "string" && SAFE_SNAPSHOT_ID.test(value.snapshot_id)) entries.push(`快照：${value.snapshot_id}`);
  if (typeof value.snapshot_hash === "string" && SAFE_HASH.test(value.snapshot_hash)) entries.push(`快照哈希：${shortHash(value.snapshot_hash)}`);
  if (typeof value.insight_hash === "string" && SAFE_HASH.test(value.insight_hash)) entries.push(`Insight 哈希：${shortHash(value.insight_hash)}`);
  if (Array.isArray(value.agents) && value.agents.length <= 20) {
    const agents = value.agents.filter((agent): agent is string => typeof agent === "string" && SAFE_REPORT_REF.test(agent));
    if (agents.length) entries.push(`参与 Agent：${agents.join("、")}`);
  }
  return entries;
}

function shortHash(value: string): string { return `${value.slice(0, 12)}…`; }

function scopeLabel(record: Pick<ResearchRecord, "scope">): string { return record.scope === "market" ? "全市场" : "单只证券"; }
function subjectLabel(record: Pick<ResearchRecord, "scope" | "subject">): string { return record.scope === "market" ? "沪深 A 股整体" : record.subject.code ?? "证券代码不可用"; }
function statusLabel(status: ResearchRecord["status"]): string { return { passed: "已通过", partial: "部分可用", blocked: "已阻断", failed: "失败", cancelled: "已取消" }[status]; }
function originLabel(origin: ResearchRecord["origin"]): string { return { web: "WebUI", cli: "CLI", scheduled: "定时调度", legacy: "历史导入" }[origin]; }
function qualitySummaryLabel(record: ResearchRecord): string {
  const summary = record.quality_summary;
  const base = {
    passed: "质量：已通过",
    warning: "质量：存在限制",
    blocked: "质量：已阻断",
    unavailable: "质量：报告不可用",
    not_applicable: "质量：不适用",
  }[summary.status];
  const details = [
    summary.limitations_count ? `${summary.limitations_count} 项限制` : "",
    summary.blocked_insights ? `${summary.blocked_insights} 项 Insight 未发布` : "",
  ].filter(Boolean);
  return details.length ? `${base}（${details.join("，")}）` : base;
}
