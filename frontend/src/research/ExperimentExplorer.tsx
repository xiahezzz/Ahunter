import { useCallback, useEffect, useRef, useState } from "react";
import { GitBranch, RefreshCw } from "lucide-react";
import { newSubmissionIdentity, researchApiError } from "./contracts";
import { Comparison, Cursor, Draft, JsonObject, Node, Page, Preflight, Selection, Test, label, object,
  parseComparisons, parseDrafts, parseNodes, parsePreflights, parseSelections, parseTests } from "./experimentContracts";

const ROOT = "/api/research/experiments";
const encoded = encodeURIComponent;
async function request(path: string, init?: RequestInit): Promise<unknown> {
  const response = await fetch(path, init);
  if (!response.ok) throw new Error(await researchApiError(response, "实验记录暂不可读取"));
  return response.json();
}
function usePages<T>(path: string, parse: (value: unknown) => Page<T>, offset = false) {
  const [pages, setPages] = useState<Page<T>[]>([]);
  const [index, setIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const serial = useRef(0);
  const retry = useRef<{ cursor: Cursor; position: number }>({ cursor: null, position: 0 });
  const read = useCallback(async (cursor: Cursor, position: number) => {
    const current = ++serial.current;
    retry.current = { cursor, position };
    setLoading(true); setError(null);
    const query = new URLSearchParams({ limit: "20" });
    if (cursor !== null) query.set(offset ? "offset" : "cursor", String(cursor));
    try {
      const result = parse(await request(`${path}?${query}`));
      if (current !== serial.current) return;
      setPages((prior) => [...prior.slice(0, position), result]); setIndex(position);
    } catch (caught) {
      if (current === serial.current) setError(caught instanceof Error ? caught.message : "实验列表暂不可读取");
    } finally { if (current === serial.current) setLoading(false); }
  }, [path, parse, offset]);
  useEffect(() => { setPages([]); setIndex(0); void read(null, 0); return () => { serial.current++; }; }, [read]);
  return { page: pages[index], index, loading, error,
    refresh: () => { setPages([]); setIndex(0); void read(null, 0); },
    retry: () => void read(retry.current.cursor, retry.current.position),
    previous: () => { setError(null); setIndex((i) => Math.max(0, i - 1)); },
    next: () => { if (pages[index + 1]) { setIndex(index + 1); setError(null); }
      else if (pages[index]?.next != null) void read(pages[index].next, index + 1); } };
}
function Paging<T>({ list, name }: { list: ReturnType<typeof usePages<T>>; name: string }) {
  return <><div className="research-pagination">
    <button type="button" className="secondary-button" onClick={list.previous} disabled={list.loading || list.index === 0} aria-label={`${name}上一页`}>上一页</button>
    <span>第 {list.index + 1} 页{list.page?.total !== undefined ? ` · 共 ${list.page.total} 项` : ""}</span>
    <button type="button" className="secondary-button" onClick={list.next} disabled={list.loading || list.page?.next == null} aria-label={`${name}下一页`}>下一页</button>
    <button type="button" className="icon-button" onClick={list.refresh} disabled={list.loading} aria-label={`刷新${name}`}><RefreshCw size={16} /></button>
  </div>{list.loading ? <p role="status">正在读取{name}…</p> : null}
    {list.error ? <div role="alert" className="research-control-feedback error">{list.error} <button type="button" className="secondary-button" onClick={list.retry}>重试{name}</button></div> : null}
  </>;
}

export default function ExperimentExplorer() {
  const [open, setOpen] = useState(false);
  return <section className="panel experiment-explorer" aria-label="历史实验">
    <div className="research-panel-heading"><h2><GitBranch size={18} />历史实验</h2>
      <button type="button" className="secondary-button" aria-expanded={open} aria-controls="experiment-browser" onClick={() => setOpen(!open)}>{open ? "收起历史实验" : "浏览历史实验"}</button></div>
    <p className="research-note">查看候选分叉、原测试与基线选择。历史实验尚未开放执行，已有记录不会替代真实运行验收。</p>
    {open ? <div id="experiment-browser"><ExperimentDirectory /></div> : null}
  </section>;
}
function ExperimentDirectory() {
  const list = usePages<Draft>(ROOT, parseDrafts, true);
  const [selected, setSelected] = useState<Draft | null>(null);
  return <><Paging list={list} name="实验列表" />
    {list.page?.items.length === 0 ? <p className="empty">尚未登记历史实验。可通过 ahunter 登记实验配置及候选后在此查看。</p> : null}
    <ul className="experiment-picker">{list.page?.items.map((draft) => <li key={draft.experiment_id}>
      <button type="button" className="secondary-button" aria-pressed={selected?.experiment_id === draft.experiment_id} onClick={() => setSelected(draft)}>{draft.name}</button>
    </li>)}</ul>
    {selected ? <ExperimentDetail key={selected.experiment_id} draft={selected} /> : null}
  </>;
}
type AuditTarget = { kind: "record"; id: string } | { kind: "events"; id: string; after: number };
function ExperimentDetail({ draft }: { draft: Draft }) {
  const base = `${ROOT}/${encoded(draft.experiment_id)}`;
  const [tab, setTab] = useState("tree");
  const [proposal, setProposal] = useState<string | null>(null);
  const [audit, setAudit] = useState<{ target: AuditTarget; value: JsonObject } | null>(null);
  const [auditError, setAuditError] = useState<string | null>(null);
  const [auditing, setAuditing] = useState(false);
  const [target, setTarget] = useState<AuditTarget | null>(null);
  const serial = useRef(0);
  const identities = useRef(new Map<string, string>());
  useEffect(() => () => { serial.current++; }, []);
  function selectProposal(id: string) { serial.current++; setAudit(null); setAuditError(null); setAuditing(false); setTarget(null); setProposal(id); }
  async function readAudit(next: AuditTarget) {
    const ticket = ++serial.current;
    const key = `${next.kind}:${next.id}:${next.kind === "events" ? next.after : ""}`;
    if (!identities.current.has(key)) identities.current.set(key, newSubmissionIdentity("experiment-read"));
    setAuditing(true); setAudit(null); setAuditError(null); setTarget(next);
    const path = next.kind === "record" ? `${base}/records/${encoded(next.id)}/detail` : `${base}/tests/${encoded(next.id)}/events?after=${next.after}&limit=20`;
    try {
      const value = object(await request(path, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ audit_identity: identities.current.get(key) }) }));
      if (next.kind === "record" && (value.record_id !== next.id || value.experiment_id !== draft.experiment_id)) throw new Error("读取结果不属于当前实验记录。");
      if (next.kind === "events" && (!Array.isArray(value.items) || value.items.some((row) => object(row).test_id !== next.id))) throw new Error("运行事件不属于当前测试。");
      if (ticket === serial.current) setAudit({ target: next, value });
    } catch (caught) { if (ticket === serial.current) setAuditError(caught instanceof Error ? caught.message : "审计读取失败"); }
    finally { if (ticket === serial.current) setAuditing(false); }
  }
  return <article className="experiment-detail" aria-label={`实验 ${draft.name}`}>
    <h3>{draft.name}</h3><p className="experiment-id">{draft.experiment_id}</p>
    <nav className="experiment-tabs" aria-label="实验视图">{[["tree", "进化树"], ["history", "选择历史"], ["preflights", "预检记录"], ["config", "原配置"]].map(([id, title]) =>
      <button type="button" className="secondary-button" key={id} aria-pressed={tab === id} onClick={() => setTab(id)}>{title}</button>)}</nav>
    <p className="research-note">分页保留原观察范围；刷新后显示后续变化。审计读取会保存访问记录，读取隐藏任务明细将计入信息暴露。</p>
    {tab === "tree" ? <div className="experiment-columns"><Tree path={`${base}/tree`} selected={proposal} onSelect={selectProposal} onAudit={(id) => void readAudit({ kind: "record", id })} />
      {proposal ? <NodeDetail key={proposal} base={base} proposal={proposal} onAudit={(id) => void readAudit({ kind: "record", id })} onEvents={(id) => void readAudit({ kind: "events", id, after: 0 })} /> : <p className="empty">选择候选查看全部测试和比较。</p>}</div> : null}
    {tab === "history" ? <SelectionHistory path={`${base}/selection/history`} onAudit={(id) => void readAudit({ kind: "record", id })} /> : null}
    {tab === "preflights" ? <Preflights path={`${base}/preflights`} /> : null}
    {tab === "config" ? <Configuration path={base} /> : null}
    {auditing ? <p role="status">正在审计读取…</p> : null}
    {auditError ? <div role="alert" className="research-control-feedback error">{auditError} <button type="button" className="secondary-button" onClick={() => target && void readAudit(target)}>重试审计读取</button></div> : null}
    {audit ? <AuditedDetail value={audit.value} target={audit.target} onRead={(id) => void readAudit({ kind: "record", id })}
      onEvents={(after) => void readAudit({ kind: "events", id: audit.target.id, after })} onClose={() => setAudit(null)} /> : null}
  </article>;
}
function Tree({ path, selected, onSelect, onAudit }: { path: string; selected: string | null; onSelect: (id: string) => void; onAudit: (id: string) => void }) {
  const list = usePages<Node>(path, parseNodes);
  return <section aria-label="候选进化树"><h4>候选父链</h4><Paging list={list} name="候选" />
    {list.page?.items.length === 0 ? <p className="empty">尚未登记候选。没有可展示的测试收益。</p> : null}
    <ol className="experiment-nodes">{list.page?.items.map((node) => <li key={node.record_id} className={selected === node.proposal_id ? "selected" : ""}>
      <div className="experiment-parent">父版本：{node.parent_proposal_id ? <button type="button" className="experiment-link" onClick={() => onSelect(node.parent_proposal_id!)}>{node.parent_proposal_id}</button> : "无（根提案）"}</div>
      <button type="button" className="experiment-node-title" aria-pressed={selected === node.proposal_id} onClick={() => onSelect(node.proposal_id)}>{node.proposal_id}</button>
      <div className="experiment-badges">{node.current_baseline ? <span>当前基线</span> : null}{node.initial_baseline ? <span>初始基线</span> : null}<span>{label(node.source)}</span></div>
      <p>{node.hypothesis}</p><p>{node.test_count} 个测试 · {Object.entries(node.test_status_counts).map(([state, count]) => `${label(state)} ${count}`).join(" / ") || "未运行"}</p>
      <details><summary>版本身份</summary><p className="experiment-id">包：{node.package_hash}</p><p className="experiment-id">差异：{node.diff_artifact_hash ?? "未登记"}</p>
        <button type="button" className="secondary-button" onClick={() => onAudit(node.record_id)}>审计读取候选记录</button></details>
    </li>)}</ol></section>;
}
function NodeDetail({ base, proposal, onAudit, onEvents }: { base: string; proposal: string; onAudit: (id: string) => void; onEvents: (id: string) => void }) {
  const [tab, setTab] = useState("tests");
  return <section className="experiment-node-detail" aria-label={`候选 ${proposal}`}><h4>{proposal}</h4>
    <div className="experiment-tabs"><button type="button" className="secondary-button" aria-pressed={tab === "tests"} onClick={() => setTab("tests")}>全部测试</button>
      <button type="button" className="secondary-button" aria-pressed={tab === "comparisons"} onClick={() => setTab("comparisons")}>版本比较</button></div>
    {tab === "tests" ? <Tests path={`${base}/candidates/${encoded(proposal)}/tests`} onAudit={onAudit} onEvents={onEvents} /> : <Comparisons path={`${base}/candidates/${encoded(proposal)}/comparisons`} onAudit={onAudit} />}
  </section>;
}
function Tests({ path, onAudit, onEvents }: { path: string; onAudit: (id: string) => void; onEvents: (id: string) => void }) {
  const list = usePages<Test>(path, parseTests);
  return <><Paging list={list} name="测试" />{list.page?.items.length === 0 ? <p className="empty">这个候选尚无测试记录。</p> : null}
    <ul className="experiment-records">{list.page?.items.map((test) => <li key={test.record_id}><strong>{test.task_id} · 第 {test.repeat_index + 1} 次</strong>
      <p>{label(test.role)} · {label(test.purpose)} · {label(test.status)}</p>{test.rerun_of ? <p>关联重跑，不替换原比较样本。</p> : null}
      {test.detail_hidden ? <p className="research-note">隐藏任务：明细读取将计入信息暴露。</p> : null}
      <div className="experiment-actions"><button type="button" className="secondary-button" onClick={() => onAudit(test.record_id)}>审计读取测试详情</button>
        <button type="button" className="secondary-button" onClick={() => onEvents(test.record_id)}>审计读取运行事件</button></div>
    </li>)}</ul></>;
}
function Comparisons({ path, onAudit }: { path: string; onAudit: (id: string) => void }) {
  const list = usePages<Comparison>(path, parseComparisons);
  return <><Paging list={list} name="比较" />{list.page?.items.length === 0 ? <p className="empty">尚未登记比较。</p> : null}
    <ul className="experiment-records">{list.page?.items.map((comparison) => <li key={comparison.record_id}><strong>{comparison.candidate} 对比 {comparison.baseline}</strong>
      <p>比较对手：{comparison.baseline} · {label(comparison.status)}</p>
      <p>{comparison.feedback ? label(comparison.feedback.decision) : "尚无结果"} · 平均改善：{comparison.feedback?.mean_improvement != null ? `${(Number(comparison.feedback.mean_improvement) * 100).toFixed(3)}%` : "—（无可用数值）"}</p>
      <p className="research-note">比较达标仍需选择决定，才会改变基线。</p>
      <div className="experiment-actions"><button type="button" className="secondary-button" onClick={() => onAudit(comparison.record_id)}>审计读取比较计划</button>
        {comparison.result_id ? <button type="button" className="secondary-button" onClick={() => onAudit(comparison.result_id!)}>审计读取原结果</button> : null}</div>
    </li>)}</ul></>;
}
function SelectionHistory({ path, onAudit }: { path: string; onAudit: (id: string) => void }) {
  const list = usePages<Selection>(path, parseSelections);
  return <><Paging list={list} name="选择历史" />{list.page?.items.length === 0 ? <p className="empty">尚未固定初始基线。</p> : null}
    <ol className="experiment-records">{list.page?.items.map((item) => <li key={item.record_id}><strong>{label(item.operation)}</strong>
      {item.reason ? <p>{label(item.reason)}</p> : null}{item.current_baseline_id ? <p className="experiment-id">基线记录：{item.current_baseline_id}</p> : null}
      <button type="button" className="secondary-button" onClick={() => onAudit(item.record_id)}>审计读取选择记录</button>
      {item.comparison_id ? <button type="button" className="secondary-button" onClick={() => onAudit(item.comparison_id!)}>审计读取关联比较</button> : null}</li>)}</ol></>;
}
function Preflights({ path }: { path: string }) {
  const list = usePages<Preflight>(path, parsePreflights, true);
  return <><Paging list={list} name="预检" />{list.page?.items.length === 0 ? <p className="empty">尚无预检记录；执行能力未验收。</p> : null}
    <ul className="experiment-records">{list.page?.items.map((item) => <li key={item.record_id}><strong>{label(item.status)}</strong><p>{item.created_at}</p>
      <ul>{item.checks.map((check, index) => <li key={`${check.code}-${index}`}>{label(check.status)} · {check.message}</li>)}</ul></li>)}</ul></>;
}
function Configuration({ path }: { path: string }) {
  const [value, setValue] = useState<JsonObject | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => { let active = true; setValue(null); setError(null);
    void request(path).then(object).then((data) => { if (active) setValue(data); }).catch((caught) => { if (active) setError(String(caught.message)); });
    return () => { active = false; }; }, [path, revision]);
  return <section aria-label="原始实验配置">{error ? <p role="alert">{error} <button type="button" onClick={() => setRevision(revision + 1)}>重试配置</button></p> : null}
    {value ? <><p>原配置只读。配置修改应另存新的实验草案。</p><JsonDetails title="配置校验问题" value={value.validation_errors} open />
      <JsonDetails title="完整原配置" value={value.raw_config} /></> : !error ? <p role="status">正在读取原配置…</p> : null}</section>;
}
function JsonDetails({ title, value, open = false }: { title: string; value: unknown; open?: boolean }) {
  return <details className="experiment-json" open={open}><summary>{title}</summary><pre>{JSON.stringify(value, null, 2)}</pre></details>;
}
function AuditedDetail({ value, target, onRead, onEvents, onClose }: { value: JsonObject; target: AuditTarget; onRead: (id: string) => void; onEvents: (after: number) => void; onClose: () => void }) {
  const events = target.kind === "events" ? (value.items as unknown[]).map(object) : null;
  const record = target.kind === "record" ? object(value.value) : null;
  const references = record ? ["plan_id", "definition_id", "comparison_id", "campaign_id", "rerun_of", "previous_selection_id"].flatMap((key) => typeof record[key] === "string" ? [[key, record[key] as string]] : []) : [];
  return <section className="experiment-audit" aria-label="已审计的原始记录"><div className="research-panel-heading"><h4>已审计的原始记录</h4><button type="button" className="secondary-button" onClick={onClose}>关闭原始记录</button></div>
    <p className="experiment-id">{target.id}</p>
    {events ? <><ol className="experiment-records">{events.map((event) => <li key={String(event.sequence)}><strong>{String(event.kind)} · {String(event.phase_id)}</strong>
      <p>记录时间：{String(event.created_at ?? "未记录")} · 模拟时点：{String(event.simulated_at ?? "未记录")}</p>
      <JsonDetails title="此事件的原始状态与证据" value={event.value} /></li>)}</ol>
      {events.length === 0 ? <p className="empty">此测试尚无运行事件。</p> : null}
      {typeof value.next_after === "number" ? <button type="button" className="secondary-button" onClick={() => onEvents(value.next_after as number)}>审计读取下一页事件</button> : null}</> : null}
    {record ? <><p>类型：{String(value.kind)} · 登记：{String(value.created_at)}</p>
      <div className="experiment-actions">{references.map(([key, id]) => <button type="button" className="secondary-button" key={key} onClick={() => onRead(id)}>审计读取 {key.replace(/_id$/, "")}</button>)}</div>
      <JsonDetails title="原记录内容" value={record} open /></> : null}
  </section>;
}
