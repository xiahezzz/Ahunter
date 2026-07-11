import {
  Activity,
  BarChart3,
  BookOpen,
  BriefcaseBusiness,
  ExternalLink,
  HeartPulse,
  RefreshCw,
  Save,
  ShieldAlert,
  TrendingUp,
  Upload,
  Users,
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useState } from "react";

type StatusCount = { status: string; count: number };
type Advice = {
  advice_id: string;
  code: string;
  action: string;
  confidence: number;
  rationale: string;
  evidence_ids: string[];
};
type Position = {
  code: string;
  quantity: number;
  cost_basis: number;
  market_price: number | null;
  market_value: number | null;
  unrealized_pnl: number;
};
type ReportLink = { report_date: string; report_type: "premarket" | "review"; run_id: string; quality_status: string; href: string };
type ProfileLink = { code: string; name: string; href: string };
type ChartLink = { asset_id: string; code: string; chart_type: string; as_of: string; href: string };
type CurrentState = {
  today: string;
  advice_status: string;
  advice: Advice[];
  review: { status: string; items: unknown[] };
  ledger: {
    cash: number;
    positions: Position[];
    realized_pnl: number;
    unrealized_pnl: number;
    accounts: unknown[];
    status?: string;
  };
  flows: { information: StatusCount; capital: StatusCount; analyst: StatusCount };
  blocking_quality_checks: Array<{
    check_name: string;
    severity: string;
    status: string;
    created_at: string;
  }>;
  reports: ReportLink[];
  report_list: { status: string; truncated?: boolean };
  profiles: ProfileLink[];
  charts: ChartLink[];
  health: Record<string, string>;
};

const CNY = new Intl.NumberFormat("zh-CN", { style: "currency", currency: "CNY" });

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isStatusCount(value: unknown): value is StatusCount {
  return isRecord(value) && typeof value.status === "string" && isFiniteNumber(value.count);
}

function isSafeHref(value: unknown): value is string {
  return typeof value === "string" && value.startsWith("/api/") && !value.startsWith("//");
}

function isReportLink(value: unknown): value is ReportLink {
  return isRecord(value) &&
    typeof value.report_date === "string" &&
    (value.report_type === "premarket" || value.report_type === "review") &&
    typeof value.run_id === "string" &&
    typeof value.quality_status === "string" &&
    isSafeHref(value.href);
}

function isProfileLink(value: unknown): value is ProfileLink {
  return isRecord(value) && typeof value.code === "string" && typeof value.name === "string" && isSafeHref(value.href);
}

function isChartLink(value: unknown): value is ChartLink {
  return isRecord(value) &&
    typeof value.asset_id === "string" &&
    typeof value.code === "string" &&
    typeof value.chart_type === "string" &&
    typeof value.as_of === "string" &&
    isSafeHref(value.href);
}

function parseCurrentState(value: unknown): CurrentState {
  if (!isRecord(value)) throw new Error("当前状态格式无效");
  const ledger = value.ledger;
  const flows = value.flows;
  const review = value.review;
  const reportList = value.report_list;
  if (
    typeof value.today !== "string" ||
    typeof value.advice_status !== "string" ||
    !["passed", "blocked", "missing"].includes(value.advice_status) ||
    !Array.isArray(value.advice) ||
    !isRecord(review) ||
    typeof review.status !== "string" ||
    !["passed", "blocked", "missing"].includes(review.status) ||
    !Array.isArray(review.items) ||
    !isRecord(ledger) ||
    !isFiniteNumber(ledger.cash) ||
    !Array.isArray(ledger.positions) ||
    !isFiniteNumber(ledger.realized_pnl) ||
    !isFiniteNumber(ledger.unrealized_pnl) ||
    !Array.isArray(ledger.accounts) ||
    !isRecord(flows) ||
    !isStatusCount(flows.information) ||
    !isStatusCount(flows.capital) ||
    !isStatusCount(flows.analyst) ||
    !Array.isArray(value.blocking_quality_checks) ||
    !Array.isArray(value.reports) ||
    !isRecord(reportList) ||
    typeof reportList.status !== "string" ||
    !["ok", "degraded"].includes(reportList.status) ||
    !Array.isArray(value.profiles) ||
    !Array.isArray(value.charts) ||
    !isRecord(value.health)
  ) {
    throw new Error("当前状态格式无效");
  }
  const validAdvice = value.advice.every(
    (item) =>
      isRecord(item) &&
      typeof item.advice_id === "string" &&
      typeof item.code === "string" &&
      typeof item.action === "string" &&
      isFiniteNumber(item.confidence) &&
      typeof item.rationale === "string" &&
      Array.isArray(item.evidence_ids) &&
      item.evidence_ids.every((id) => typeof id === "string"),
  );
  const validPositions = ledger.positions.every(
    (item) =>
      isRecord(item) &&
      typeof item.code === "string" &&
      isFiniteNumber(item.quantity) &&
      isFiniteNumber(item.cost_basis) &&
      (item.market_price === null || isFiniteNumber(item.market_price)) &&
      (item.market_value === null || isFiniteNumber(item.market_value)) &&
      isFiniteNumber(item.unrealized_pnl),
  );
  const validChecks = value.blocking_quality_checks.every(
    (item) =>
      isRecord(item) &&
      typeof item.check_name === "string" &&
      typeof item.severity === "string" &&
      typeof item.status === "string" &&
      typeof item.created_at === "string",
  );
  const validHealth = Object.values(value.health).every((item) => typeof item === "string");
  if (
    !validAdvice ||
    !validPositions ||
    !validChecks ||
    !value.reports.every(isReportLink) ||
    !value.profiles.every(isProfileLink) ||
    !value.charts.every(isChartLink) ||
    !validHealth
  ) {
    throw new Error("当前状态格式无效");
  }
  return value as CurrentState;
}

function statusLabel(status: string) {
  return { passed: "已通过", blocked: "已阻断", missing: "缺失", ok: "正常", running: "运行中", degraded: "降级", unknown: "未知", failed: "失败" }[status] ?? status;
}

function statusClass(status: string) {
  if (["ok", "passed", "running"].includes(status)) return "status-ok";
  if (["blocked", "failed"].includes(status)) return "status-danger";
  if (["degraded", "missing"].includes(status)) return "status-warn";
  return "status-muted";
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="metric"><dt>{label}</dt><dd>{value}</dd></div>;
}

async function apiError(response: Response) {
  try {
    const payload: unknown = await response.json();
    if (isRecord(payload) && typeof payload.detail === "string") return payload.detail.slice(0, 240);
  } catch {
    // The bounded status message below is the fail-closed fallback.
  }
  return `请求失败 (${response.status})`;
}

function ManualLedgerForm({ onSaved }: { onSaved: () => Promise<void> }) {
  const [transactionId, setTransactionId] = useState("");
  const [tradeDate, setTradeDate] = useState("");
  const [transactionType, setTransactionType] = useState("cash_deposit");
  const [code, setCode] = useState("");
  const [quantity, setQuantity] = useState("0");
  const [price, setPrice] = useState("0");
  const [amount, setAmount] = useState("0");
  const [fees, setFees] = useState("0");
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<{ kind: "success" | "error"; text: string } | null>(null);
  const isTrade = transactionType === "buy" || transactionType === "sell";

  async function submit(event: FormEvent) {
    event.preventDefault();
    setMessage(null);
    const values = [quantity, price, amount, fees].map(Number);
    if (!transactionId.trim() || !tradeDate || values.some((value) => !Number.isFinite(value))) {
      setMessage({ kind: "error", text: "请填写有效的流水编号、日期和数值" });
      return;
    }
    if (isTrade && !/^\d{6}$/.test(code)) {
      setMessage({ kind: "error", text: "股票代码必须为 6 位数字" });
      return;
    }
    const payload = {
      transaction_id: transactionId.trim(),
      trade_date: tradeDate,
      transaction_type: transactionType,
      ...(isTrade ? { code } : {}),
      quantity: values[0],
      price: values[1],
      amount: values[2],
      fees: values[3],
    };
    setPending(true);
    try {
      const response = await fetch("/api/ledger/transactions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) throw new Error(await apiError(response));
      await onSaved();
      setMessage({ kind: "success", text: "流水已保存，状态已刷新" });
      setTransactionId("");
      setAmount("0");
    } catch (caught) {
      setMessage({ kind: "error", text: caught instanceof Error ? caught.message : "流水保存失败" });
    } finally {
      setPending(false);
    }
  }

  return (
    <form className="ledger-form" aria-label="新增流水" onSubmit={submit}>
      <div className="form-heading"><h2>新增流水</h2><span>手工</span></div>
      <div className="form-grid">
        <label>流水编号<input value={transactionId} onChange={(event) => setTransactionId(event.target.value)} maxLength={128} required /></label>
        <label>交易日期<input type="date" value={tradeDate} onChange={(event) => setTradeDate(event.target.value)} required /></label>
        <label>流水类型<select value={transactionType} onChange={(event) => setTransactionType(event.target.value)}><option value="cash_deposit">资金存入</option><option value="cash_withdrawal">资金取出</option><option value="buy">买入</option><option value="sell">卖出</option><option value="fee">费用</option><option value="tax">税费</option></select></label>
        {isTrade && <label>股票代码<input value={code} onChange={(event) => setCode(event.target.value)} inputMode="numeric" pattern="[0-9]{6}" maxLength={6} required /></label>}
        {isTrade && <label>数量<input type="number" value={quantity} onChange={(event) => setQuantity(event.target.value)} min="0" step="1" required /></label>}
        {isTrade && <label>价格<input type="number" value={price} onChange={(event) => setPrice(event.target.value)} min="0" step="0.01" required /></label>}
        <label>金额<input type="number" value={amount} onChange={(event) => setAmount(event.target.value)} step="0.01" required /></label>
        <label>费用<input type="number" value={fees} onChange={(event) => setFees(event.target.value)} min="0" step="0.01" required /></label>
      </div>
      <div className="form-actions"><button type="submit" disabled={pending}><Save size={16} />{pending ? "正在保存" : "保存流水"}</button>{message && <p className={`form-message ${message.kind}`} role={message.kind === "error" ? "alert" : "status"}>{message.text}</p>}</div>
    </form>
  );
}

const MAX_IMPORT_CHARACTERS = 512 * 1024;
const MAX_IMPORT_ITEMS = 500;

function LedgerImportForm({ onImported }: { onImported: () => Promise<void> }) {
  const [text, setText] = useState("");
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<{ kind: "success" | "error"; text: string } | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setMessage(null);
    if (!text || text.length > MAX_IMPORT_CHARACTERS) {
      setMessage({ kind: "error", text: "JSON 内容为空或超过大小限制" });
      return;
    }
    try {
      const parsed: unknown = JSON.parse(text);
      if (!Array.isArray(parsed) || parsed.length > MAX_IMPORT_ITEMS) {
        setMessage({ kind: "error", text: "JSON 必须是最多 500 条流水的列表" });
        return;
      }
    } catch {
      setMessage({ kind: "error", text: "JSON 格式无效" });
      return;
    }
    setPending(true);
    try {
      const response = await fetch("/api/ledger/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: text,
      });
      if (!response.ok) throw new Error(await apiError(response));
      await onImported();
      setMessage({ kind: "success", text: "JSON 流水已导入，状态已刷新" });
      setText("");
    } catch (caught) {
      setMessage({ kind: "error", text: caught instanceof Error ? caught.message : "JSON 导入失败" });
    } finally {
      setPending(false);
    }
  }

  return (
    <form className="import-form" aria-label="JSON 导入" onSubmit={submit}>
      <div className="form-heading"><h2>JSON 导入</h2><span>最多 500 条</span></div>
      <label>JSON 流水列表<textarea value={text} onChange={(event) => setText(event.target.value)} maxLength={MAX_IMPORT_CHARACTERS} rows={8} required /></label>
      <div className="form-actions"><button type="submit" disabled={pending}><Upload size={16} />{pending ? "正在导入" : "导入 JSON"}</button>{message && <p className={`form-message ${message.kind}`} role={message.kind === "error" ? "alert" : "status"}>{message.text}</p>}</div>
    </form>
  );
}

function App() {
  const [state, setState] = useState<CurrentState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);

  const loadState = useCallback(async (signal?: AbortSignal) => {
    setError(null);
    setIsRefreshing(true);
    try {
      const result = await fetch("/api/current-state", { signal });
      if (!result.ok) throw new Error(`请求失败 (${result.status})`);
      setState(parseCurrentState(await result.json()));
    } catch (caught) {
      if (caught instanceof DOMException && caught.name === "AbortError") return;
      setState(null);
      setError(caught instanceof Error ? caught.message : "未知错误");
    } finally {
      setIsRefreshing(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void loadState(controller.signal);
    return () => controller.abort();
  }, [loadState]);

  if (!state && !error) return <main className="center-state" aria-live="polite">正在读取当前状态…</main>;
  if (!state) return <main className="center-state" role="alert"><strong>当前状态读取失败</strong><span>{error}</span><button type="button" onClick={() => void loadState()} disabled={isRefreshing}><RefreshCw size={16} />{isRefreshing ? "正在重试" : "重试读取"}</button></main>;

  const knownValues = state.ledger.positions.flatMap((position) => position.market_value === null ? [] : [position.market_value]);
  const exposure = knownValues.length === state.ledger.positions.length
    ? knownValues.reduce((sum, value) => sum + value, 0)
    : null;
  const reports = state.reports;
  const profiles = state.profiles;
  const charts = state.charts;

  return (
    <main className="shell">
      <header className="topbar">
        <div><p className="eyebrow">本地投顾运行台</p><h1>A Hunter Advisor</h1></div>
        <div className="topbar-meta"><span>{state.today}</span><span className={statusClass(state.advice_status)}>建议 {statusLabel(state.advice_status)}</span><span className={statusClass(state.report_list.status)}>报告 {statusLabel(state.report_list.status)}</span><button type="button" className="icon-button" aria-label="刷新当前状态" title="刷新当前状态" onClick={() => void loadState()} disabled={isRefreshing}><RefreshCw className={isRefreshing ? "spin" : undefined} size={17} /></button></div>
      </header>

      <section className="summary-grid" aria-label="投资概览">
        <article className="panel ledger-summary">
          <h2><BriefcaseBusiness size={18} />账户</h2>
          <dl className="metrics"><Metric label="可用现金" value={CNY.format(state.ledger.cash)} /><Metric label="持仓市值" value={exposure === null ? "暂无数据" : CNY.format(exposure)} /><Metric label="已实现盈亏" value={CNY.format(state.ledger.realized_pnl)} /><Metric label="未实现盈亏" value={CNY.format(state.ledger.unrealized_pnl)} /></dl>
        </article>
        <article className="panel advice-summary">
          <h2><TrendingUp size={18} />08:30 建议</h2>
          <span className={`status-badge ${statusClass(state.advice_status)}`}>{statusLabel(state.advice_status)}</span>
          {state.advice_status === "blocked" ? <p className="blocked-message">建议已阻断</p> : state.advice.length === 0 ? <p className="empty">暂无建议</p> : <ul className="plain-list">{state.advice.map((item) => <li key={item.advice_id}><div><strong>{item.code}</strong><span>{item.action} · {Math.round(item.confidence * 100)}%</span></div><p>{item.rationale}</p></li>)}</ul>}
        </article>
      </section>

      <section className="flow-grid" aria-label="三流状态">
        {([["信息流", state.flows.information], ["资金流", state.flows.capital], ["分析流", state.flows.analyst]] as const).map(([label, flow]) => <div className="flow-item" key={label}><span>{label}</span><strong className={statusClass(flow.status)}>{statusLabel(flow.status)}</strong><small>{flow.count} 条</small></div>)}
      </section>

      <section className="main-grid">
        <article className="panel positions-panel"><h2><BarChart3 size={18} />持仓</h2>{state.ledger.positions.length === 0 ? <p className="empty">暂无持仓</p> : <div className="table-wrap"><table><thead><tr><th>代码</th><th>数量</th><th>成本</th><th>现价</th><th>市值</th><th>浮动盈亏</th></tr></thead><tbody>{state.ledger.positions.map((position) => <tr key={position.code}><td>{position.code}</td><td>{position.quantity}</td><td>{CNY.format(position.cost_basis)}</td><td>{position.market_price === null ? "暂无数据" : CNY.format(position.market_price)}</td><td>{position.market_value === null ? "暂无数据" : CNY.format(position.market_value)}</td><td>{CNY.format(position.unrealized_pnl)}</td></tr>)}</tbody></table></div>}</article>
        <article className="panel quality-panel"><h2><ShieldAlert size={18} />质量检查</h2>{state.blocking_quality_checks.length === 0 ? <p className="healthy">无阻断项</p> : <ul className="plain-list">{state.blocking_quality_checks.map((check) => <li key={`${check.check_name}-${check.created_at}`}><strong>{check.check_name}</strong><span>{statusLabel(check.status)}</span><small>{check.created_at}</small></li>)}</ul>}</article>
        <article className="panel health-panel"><h2><HeartPulse size={18} />进程健康</h2><dl className="status-list">{Object.entries(state.health).filter(([name]) => !["status", "service"].includes(name)).map(([name, status]) => <div key={name}><dt>{name}</dt><dd className={statusClass(status)}>{statusLabel(status)}</dd></div>)}</dl></article>
      </section>

      <section className="resources">
        <div><h2><BookOpen size={18} />报告</h2>{reports.length === 0 ? <p className="empty">暂无报告</p> : <ul className="link-list">{reports.map((report) => <li key={report.href}><a href={report.href} target="_blank" rel="noreferrer">{report.report_date} {report.report_type === "premarket" ? "盘前" : "复盘"}<ExternalLink size={14} /></a></li>)}</ul>}</div>
        <div><h2><Users size={18} />股票档案</h2>{profiles.length === 0 ? <p className="empty">暂无档案</p> : <ul className="link-list">{profiles.map((profile) => <li key={profile.href}><a href={profile.href} target="_blank" rel="noreferrer">{profile.code} {profile.name}<ExternalLink size={14} /></a></li>)}</ul>}</div>
        <div><h2><Activity size={18} />K线图</h2>{charts.length === 0 ? <p className="empty">暂无图表</p> : <ul className="link-list">{charts.map((chart) => <li key={chart.href}><a href={chart.href} target="_blank" rel="noreferrer">{chart.code} K线 · {chart.as_of}<ExternalLink size={14} /></a></li>)}</ul>}</div>
      </section>
      <section className="ledger-tools">
        <ManualLedgerForm onSaved={() => loadState()} />
        <LedgerImportForm onImported={() => loadState()} />
      </section>
    </main>
  );
}

export default App;
