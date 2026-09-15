import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { Bot, RefreshCw } from "lucide-react";
import DataProductCatalog from "./DataProductCatalog";
import {
  AccessDirectory,
  AgentAccess,
  AgentAccessGroup,
  DataAccess,
  parseAccessDirectory,
  parseProductCatalog,
  parsePublishedAccess,
  parsePublishedInstructions,
  Product,
  ProductGroup,
  researchApiError,
} from "./contracts";
import { diffLines } from "./lineDiff";

type Feedback = { kind: "success" | "error"; text: string };
type DraftAccess = { product: string; rids: number[] | null };
type InstructionsDraft = {
  base: AgentAccess;
  sourceRef: string;
  text: string;
  reviewing: boolean;
};

type AgentAccessPanelProps = {
  onReviseTeam: (teamRef: string) => void;
  onInstructionsDirtyChange?: (dirty: boolean) => void;
  refreshToken?: number;
};

function displayError(error: unknown, fallback: string): string {
  return error instanceof Error && /[\u3400-\u9fff]/.test(error.message) ? error.message.slice(0, 120) : fallback;
}

async function requestJson(path: string, init?: RequestInit): Promise<unknown> {
  const response = await fetch(path, init);
  if (!response.ok) throw new Error(await researchApiError(response, "研究配置暂不可读取"));
  return response.json();
}

function asDraft(access: DataAccess): DraftAccess {
  return { product: access.product_ref, rids: access.feed_scope ? access.feed_scope.rids.map((item) => item.rid) : null };
}

function versionNumber(reference: string): number {
  return Number(reference.split("@")[1]);
}

function nextVersion(reference: string): string {
  const [id] = reference.split("@", 1);
  return `${id}@${versionNumber(reference) + 1}`;
}

function accessLabel(access: DataAccess): string {
  if (!access.feed_scope) return access.product_ref;
  const rids = access.feed_scope.rids.map((item) => `${item.rid}${item.authorization === "revoked" ? "（历史）" : ""}`);
  return `${access.product_ref} · MX RID Feeds：${rids.join("、")}`;
}

export default function AgentAccessPanel({
  onReviseTeam,
  onInstructionsDirtyChange,
  refreshToken = 0,
}: AgentAccessPanelProps) {
  const [products, setProducts] = useState<ProductGroup[]>([]);
  const [directory, setDirectory] = useState<AccessDirectory | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [directoryLoading, setDirectoryLoading] = useState(true);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [directoryError, setDirectoryError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [accessDraftBase, setAccessDraftBase] = useState<AgentAccess | null>(null);
  const [draftAccesses, setDraftAccesses] = useState<DraftAccess[]>([]);
  const [selectedProduct, setSelectedProduct] = useState("");
  const [instructionsDraft, setInstructionsDraft] = useState<InstructionsDraft | null>(null);
  const [publishing, setPublishing] = useState<"access" | "instructions" | null>(null);
  const [expandedHistory, setExpandedHistory] = useState<Set<string>>(new Set());

  const instructionsDirty = instructionsDraft !== null && instructionsDraft.text !== instructionsDraft.base.instructions;

  const refresh = useCallback(async (): Promise<boolean> => {
    setCatalogLoading(true);
    setDirectoryLoading(true);
    const [catalogResult, accessResult] = await Promise.allSettled([
      requestJson("/api/research/data-catalog"),
      requestJson("/api/research/agent-access"),
    ]);
    let complete = true;
    if (catalogResult.status === "fulfilled") {
      try {
        setProducts(parseProductCatalog(catalogResult.value));
        setCatalogError(null);
      } catch (caught) {
        complete = false;
        setCatalogError(displayError(caught, "Data Product Catalog 暂不可读取"));
      }
    } else {
      complete = false;
      setCatalogError(displayError(catalogResult.reason, "Data Product Catalog 暂不可读取"));
    }
    if (accessResult.status === "fulfilled") {
      try {
        setDirectory(parseAccessDirectory(accessResult.value));
        setDirectoryError(null);
      } catch (caught) {
        complete = false;
        setDirectoryError(displayError(caught, "Research Agent 目录暂不可读取"));
      }
    } else {
      complete = false;
      setDirectoryError(displayError(accessResult.reason, "Research Agent 目录暂不可读取"));
    }
    setCatalogLoading(false);
    setDirectoryLoading(false);
    return complete;
  }, []);

  useEffect(() => { void refresh(); }, [refresh, refreshToken]);

  useEffect(() => {
    onInstructionsDirtyChange?.(instructionsDirty);
  }, [instructionsDirty, onInstructionsDirtyChange]);

  useEffect(() => () => onInstructionsDirtyChange?.(false), [onInstructionsDirtyChange]);

  useEffect(() => {
    if (!instructionsDirty) return undefined;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [instructionsDirty]);

  const productVersions = useMemo(() => products.flatMap((group) => group.history), [products]);
  const productsByRef = useMemo(() => new Map(productVersions.map((product) => [product.product_ref, product])), [productVersions]);
  const currentRids = useMemo(() => {
    if (!directory) return [];
    const values = new Set<number>();
    for (const group of directory.agents) {
      for (const rid of group.latest.unassigned_rids) values.add(rid);
      for (const access of group.latest.data_access) {
        for (const item of access.feed_scope?.rids ?? []) if (item.authorization === "current") values.add(item.rid);
      }
    }
    return [...values].sort((left, right) => left - right);
  }, [directory]);

  const revokedDraftRids = useMemo(() => {
    if (!accessDraftBase) return [];
    return draftAccesses.flatMap((draft) => {
      const original = accessDraftBase.data_access.find((access) => access.product_ref === draft.product);
      return (draft.rids ?? []).filter((rid) => original?.feed_scope?.rids.some((item) => item.rid === rid && item.authorization === "revoked"));
    });
  }, [draftAccesses, accessDraftBase]);

  const affectedTeamRefs = useMemo(() => {
    if (!instructionsDraft || !directory) return [];
    const agentId = instructionsDraft.base.agent_ref.split("@", 1)[0];
    const group = directory.agents.find((item) => item.agent_id === agentId);
    return [...new Set(group?.history.flatMap((agent) => agent.used_by_team_refs) ?? [])].sort();
  }, [directory, instructionsDraft]);

  const instructionChanges = useMemo(
    () => instructionsDraft ? diffLines(instructionsDraft.base.instructions, instructionsDraft.text) : [],
    [instructionsDraft],
  );

  function cancelAccessDraft() {
    setAccessDraftBase(null);
    setDraftAccesses([]);
    setSelectedProduct("");
  }

  function cancelInstructionsDraft() {
    setInstructionsDraft(null);
  }

  function startAccessRevision(agent: AgentAccess) {
    if (instructionsDirty && !window.confirm("放弃未发布的 Agent Instructions 草稿？")) return;
    cancelInstructionsDraft();
    setAccessDraftBase(agent);
    setDraftAccesses(agent.data_access.map(asDraft));
    setSelectedProduct("");
    setFeedback(null);
  }

  function startInstructionsRevision(group: AgentAccessGroup, source: AgentAccess) {
    if (accessDraftBase && !window.confirm("放弃当前 Agent Data Access 草稿？")) return;
    if (instructionsDirty && !window.confirm("放弃未发布的 Agent Instructions 草稿？")) return;
    cancelAccessDraft();
    setInstructionsDraft({
      base: group.latest,
      sourceRef: source.agent_ref,
      text: source.instructions,
      reviewing: false,
    });
    setFeedback(null);
  }

  function addProduct() {
    const product = productsByRef.get(selectedProduct);
    if (!product) {
      setFeedback({ kind: "error", text: "请选择一个已发布的 Data Product" });
      return;
    }
    if (draftAccesses.some((access) => access.product === product.product_ref)) {
      setFeedback({ kind: "error", text: "该 Data Product 已在访问草稿中" });
      return;
    }
    setDraftAccesses((current) => [...current, { product: product.product_ref, rids: product.supports_feed_scope ? [] : null }]);
    setSelectedProduct("");
    setFeedback(null);
  }

  function removeProduct(productRef: string) {
    setDraftAccesses((current) => current.filter((access) => access.product !== productRef));
    setFeedback(null);
  }

  function toggleRid(productRef: string, rid: number) {
    setDraftAccesses((current) => current.map((access) => {
      if (access.product !== productRef || access.rids === null) return access;
      return { ...access, rids: access.rids.includes(rid) ? access.rids.filter((item) => item !== rid) : [...access.rids, rid].sort((left, right) => left - right) };
    }));
    setFeedback(null);
  }

  function validateAccessDraft(): string | null {
    if (!accessDraftBase || !directory) return "Agent 数据访问目录暂不可读取，不能发布";
    if (draftAccesses.length === 0) return "请至少保留一个 Data Product";
    if (revokedDraftRids.length > 0) return "存在已撤销的 RID Feed；请重新授权或从草稿中移除该 Feed";
    for (const access of draftAccesses) {
      const product = productsByRef.get(access.product);
      if (!product) return "Data Product Catalog 已变化，请刷新后再提交";
      if (product.supports_feed_scope && (!access.rids || access.rids.length === 0)) return "mx_events@2 必须逐项选择至少一个当前授权 RID";
      if (!product.supports_feed_scope && access.rids !== null) return "Data Product scope 已变化，请刷新后再提交";
      if (access.rids && access.rids.some((rid) => !currentRids.includes(rid))) return "所选 RID 已不再处于当前授权集合，请刷新后再提交";
    }
    return null;
  }

  async function publishAccess(event: FormEvent) {
    event.preventDefault();
    const problem = validateAccessDraft();
    if (problem) {
      setFeedback({ kind: "error", text: problem });
      return;
    }
    if (!accessDraftBase || !directory) return;
    setPublishing("access");
    setFeedback(null);
    try {
      const payload = {
        data_access: draftAccesses.map((access) => access.rids === null ? { product: access.product } : { product: access.product, feed_scope: { rids: access.rids } }),
        rid_version: directory.rid_version,
      };
      const result = parsePublishedAccess(await requestJson(`/api/research/agents/${encodeURIComponent(accessDraftBase.agent_ref)}/access-revisions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }));
      const message = result.created ? `已发布 ${result.agent.agent_ref}` : `已确认已发布 ${result.agent.agent_ref}`;
      cancelAccessDraft();
      const refreshed = await refresh();
      setFeedback({ kind: "success", text: refreshed ? `${message}；Team 和每日启用未改变` : `${message}；目录刷新失败，请刷新确认` });
    } catch (caught) {
      setFeedback({ kind: "error", text: displayError(caught, "Agent 数据访问发布失败") });
    } finally {
      setPublishing(null);
    }
  }

  function reviewInstructions(event: FormEvent) {
    event.preventDefault();
    if (!instructionsDraft) return;
    if (!instructionsDraft.text.trim()) {
      setFeedback({ kind: "error", text: "Agent Instructions 不能为空" });
      return;
    }
    if (instructionsDraft.text.length > 20_000) {
      setFeedback({ kind: "error", text: "Agent Instructions 不能超过 20000 个字符" });
      return;
    }
    if (!instructionsDirty) {
      setFeedback({ kind: "error", text: "Agent Instructions 未发生变化" });
      return;
    }
    setFeedback(null);
    setInstructionsDraft({ ...instructionsDraft, reviewing: true });
  }

  async function publishInstructions() {
    if (!instructionsDraft || !instructionsDirty) return;
    setPublishing("instructions");
    setFeedback(null);
    try {
      const result = parsePublishedInstructions(await requestJson(
        `/api/research/agents/${encodeURIComponent(instructionsDraft.base.agent_ref)}/instruction-revisions`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ instructions: instructionsDraft.text }),
        },
      ));
      const message = result.created ? `已发布 ${result.agent.agent_ref}` : `已确认已发布 ${result.agent.agent_ref}`;
      cancelInstructionsDraft();
      const refreshed = await refresh();
      setFeedback({ kind: "success", text: refreshed ? `${message}；Team 和每日启用未改变` : `${message}；目录刷新失败，请刷新确认` });
    } catch (caught) {
      setFeedback({ kind: "error", text: displayError(caught, "Agent Instructions 发布失败") });
    } finally {
      setPublishing(null);
    }
  }

  function renderVersion(group: AgentAccessGroup, agent: AgentAccess, historical = false) {
    const busy = publishing !== null;
    return <div className={historical ? "agent-access-version historical" : "agent-access-version"} key={agent.agent_ref}>
      <div>
        <strong>{historical ? agent.agent_ref : agent.title}</strong>
        <small>{historical ? agent.title : agent.agent_ref}</small>
        <section className="agent-instructions-block" aria-label={`${agent.agent_ref} Agent Instructions`}>
          <h4>Agent Instructions</h4>
          <pre>{agent.instructions}</pre>
        </section>
        <h4 className="agent-access-heading">Agent Data Access</h4>
        <ul className="agent-access-products">{agent.data_access.map((access) => <li key={access.product_ref}>{accessLabel(access)}</li>)}</ul>
        {agent.unassigned_rids.length ? <p className="research-note">未分配当前 RID：{agent.unassigned_rids.join("、")}</p> : null}
        {agent.blocked_reasons.length ? <p className="research-blocked">{agent.blocked_reasons.join("；")}。请重新授权，或发布一个移除该 Feed 的新版本。</p> : null}
        <p className="research-read-only">只读摘要：查询次数 {agent.read_only.query_budget ?? "无限制"}，结果行数 {agent.read_only.max_result_rows ?? "无限制"}，{agent.read_only.implementation === "code" ? "代码实现" : "声明式实现"}</p>
        {agent.used_by_team_refs.length ? <div className="agent-team-usage"><span>固定使用此版本的 Team：</span>{agent.used_by_team_refs.map((teamRef) => <button className="text-button" type="button" key={teamRef} onClick={() => onReviseTeam(teamRef)} disabled={busy}>基于 {teamRef} 修订 Team</button>)}</div> : null}
      </div>
      <div className="agent-version-actions">
        <button type="button" className="secondary-button" onClick={() => startInstructionsRevision(group, agent)} disabled={busy || directoryLoading}>{historical ? "恢复此版本指令" : "修订指令"}</button>
        {!historical ? <button type="button" className="secondary-button" onClick={() => startAccessRevision(agent)} disabled={busy || directoryLoading}>修订数据访问</button> : null}
      </div>
    </div>;
  }

  const busy = publishing !== null;
  return <div className="research-access-area">
    <section className="panel agent-access-panel" aria-label="Research Agents">
      <div className="research-panel-heading"><h2><Bot size={18} />Research Agents</h2><button type="button" className="icon-button" aria-label="刷新 Research Agents" title="刷新 Research Agents" onClick={() => void refresh()} disabled={catalogLoading || directoryLoading || busy}><RefreshCw className={catalogLoading || directoryLoading ? "spin" : undefined} size={17} /></button></div>
      {directoryLoading && !directory ? <p className="unavailable-message">正在读取 Research Agent 目录…</p> : null}
      {directoryError && !directory ? <p className="unavailable-message" role="alert">{directoryError}</p> : null}
      {directoryError && directory ? <p className="research-inline-error" role="alert">{directoryError}</p> : null}
      {feedback ? <p className={`research-feedback ${feedback.kind}`} role={feedback.kind === "error" ? "alert" : "status"}>{feedback.text}</p> : null}

      {instructionsDraft ? instructionsDraft.reviewing ? <section className="agent-instructions-review" aria-label="Agent Instructions 发布审阅">
        <div><h3>审阅 {instructionsDraft.base.title} 的 Instructions 修订</h3><p className="research-note">基础版本 {instructionsDraft.base.agent_ref} · 目标版本 {nextVersion(instructionsDraft.base.agent_ref)}</p></div>
        {instructionsDraft.sourceRef !== instructionsDraft.base.agent_ref ? <p className="research-note">正在把 {instructionsDraft.sourceRef} 的历史文本恢复为新版本；历史版本本身不会改变。</p> : null}
        <p className="research-note">受影响但不会自动升级的 Team：{affectedTeamRefs.length ? affectedTeamRefs.join("、") : "无"}</p>
        <ol className="agent-instructions-diff" aria-label="Agent Instructions 逐行差异">{instructionChanges.map((change, index) => <li className={`diff-${change.kind}`} key={`${change.kind}-${change.oldLine}-${change.newLine}-${index}`}><span>{change.kind === "removed" ? "−" : change.kind === "added" ? "+" : " "}</span><small>{change.oldLine ?? ""}</small><small>{change.newLine ?? ""}</small><code>{change.text || " "}</code></li>)}</ol>
        <div className="agent-draft-actions"><button type="button" onClick={() => void publishInstructions()} disabled={busy}>{publishing === "instructions" ? "正在发布" : `确认发布 ${nextVersion(instructionsDraft.base.agent_ref)}`}</button><button type="button" className="secondary-button" onClick={() => setInstructionsDraft({ ...instructionsDraft, reviewing: false })} disabled={busy}>返回编辑</button><button type="button" className="secondary-button" onClick={cancelInstructionsDraft} disabled={busy}>取消草稿</button></div>
      </section> : <form className="agent-instructions-draft" onSubmit={reviewInstructions} aria-label="Agent Instructions 草稿">
        <div><h3>修订 {instructionsDraft.base.title} 的 Agent Instructions</h3><p className="research-note">只改变 Instructions；Data Access、输出契约、查询预算和实现保持不变。</p></div>
        {instructionsDraft.sourceRef !== instructionsDraft.base.agent_ref ? <p className="research-note">文本来源：历史版本 {instructionsDraft.sourceRef}；发布基准仍是最新版 {instructionsDraft.base.agent_ref}。</p> : null}
        <label>Agent Instructions<textarea aria-label="Agent Instructions" value={instructionsDraft.text} onChange={(event) => setInstructionsDraft({ ...instructionsDraft, text: event.target.value, reviewing: false })} maxLength={20_000} rows={12} spellCheck={false} required /></label>
        <p className="research-character-count">{instructionsDraft.text.length} / 20000</p>
        <div className="agent-draft-actions"><button type="submit" disabled={busy || !instructionsDirty}>审阅并发布</button><button type="button" className="secondary-button" onClick={cancelInstructionsDraft} disabled={busy}>取消草稿</button></div>
      </form> : null}

      {accessDraftBase ? <form className="agent-access-draft" onSubmit={(event) => void publishAccess(event)} aria-label="Agent 数据访问草稿">
        <div><h3>修订 {accessDraftBase.title} 的数据访问</h3><p className="research-note">标题、Instructions、输出契约、查询预算与实现均为只读；本草稿只改变 Data Product 与产品 scope。</p></div>
        {revokedDraftRids.length ? <p className="research-blocked" role="alert">草稿引用已撤销 RID：{revokedDraftRids.join("、")}。请重新授权，或移除对应 Feed 后发布。</p> : null}
        <ul className="agent-draft-products">{draftAccesses.map((access) => {
          const product = productsByRef.get(access.product);
          return <li key={access.product}><div><strong>{access.product}</strong>{product ? <small>{product.title}</small> : <small>目录中未找到此版本</small>}{access.rids !== null ? <fieldset><legend>逐项选择当前授权 MX RID</legend>{currentRids.length === 0 ? <p className="empty">当前没有可选择的 RID。</p> : currentRids.map((rid) => <label key={rid} className="rid-checkbox"><input type="checkbox" checked={access.rids?.includes(rid) ?? false} onChange={() => toggleRid(access.product, rid)} disabled={busy} />RID {rid}</label>)}</fieldset> : null}</div><button type="button" className="text-button" onClick={() => removeProduct(access.product)} disabled={busy}>移除</button></li>;
        })}</ul>
        <div className="agent-add-product"><label>添加 Data Product<select aria-label="添加 Data Product" value={selectedProduct} onChange={(event) => setSelectedProduct(event.target.value)} disabled={busy}><option value="">请选择精确版本</option>{productVersions.map((product: Product) => <option key={product.product_ref} value={product.product_ref}>{product.product_ref} · {product.title}</option>)}</select></label><button type="button" className="secondary-button" onClick={addProduct} disabled={busy}>添加</button></div>
        <div className="agent-draft-actions"><button type="submit" disabled={busy}>{publishing === "access" ? "正在发布" : "发布新的 Agent Access 版本"}</button><button type="button" className="secondary-button" onClick={cancelAccessDraft} disabled={busy}>取消草稿</button></div>
      </form> : null}

      {!directoryLoading || directory ? <div className="agent-access-cards">{directory?.agents.length === 0 ? <p className="empty">暂无已发布 Agent</p> : directory?.agents.map((group) => {
        const history = group.history.filter((agent) => agent.agent_ref !== group.latest.agent_ref).sort((left, right) => versionNumber(right.agent_ref) - versionNumber(left.agent_ref));
        const open = expandedHistory.has(group.agent_id);
        return <article className="agent-access-card" key={group.agent_id} aria-label={`${group.latest.title} ${group.latest.agent_ref}`}>
          {renderVersion(group, group.latest)}
          {history.length ? <><button type="button" className="text-button" onClick={() => setExpandedHistory((current) => { const next = new Set(current); if (next.has(group.agent_id)) next.delete(group.agent_id); else next.add(group.agent_id); return next; })}>{open ? "收起历史版本" : "查看历史版本"}</button>{open ? <div className="agent-access-history">{history.map((agent) => renderVersion(group, agent, true))}</div> : null}</> : null}
        </article>;
      })}</div> : null}
    </section>
    <DataProductCatalog products={products} loading={catalogLoading} error={catalogError} onRefresh={() => void refresh()} />
  </div>;
}
