import {
  Activity,
  AlertTriangle,
  ArrowRight,
  BarChart3,
  BookOpen,
  BriefcaseBusiness,
  CircleDollarSign,
  Database,
  ExternalLink,
  FileJson2,
  HeartPulse,
  LayoutDashboard,
  MessageSquareText,
  Microscope,
  Plus,
  RefreshCw,
  Save,
  Settings2,
  ShieldAlert,
  TrendingUp,
  Upload,
  Users,
} from "lucide-react";
import { FormEvent, MouseEvent, ReactNode, useCallback, useEffect, useState } from "react";
import MxOperationsPage from "./mx/MxOperationsPage";
import ResearchConfigurationPage from "./research/ResearchConfigurationPage";

type FlowStatus = "ok" | "degraded" | "unknown";
type ReportStatus = "passed" | "blocked" | "missing";
type ListStatus = "ok" | "degraded";
type HealthStatus = "ok" | "healthy" | "running" | "degraded" | "failed" | "stopped" | "unknown";
type StatusCount = { status: FlowStatus; count: number };
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
type ReportLink = { report_date: string; report_type: "premarket" | "review"; run_id: string; quality_status: "passed" | "blocked"; href: string };
type ProfileLink = { code: string; name: string; href: string };
type ChartLink = { asset_id: string; code: string; chart_type: string; as_of: string; href: string };
type Health = {
  status: "ok";
  service: "advisor-api";
  collector: HealthStatus;
  market_updater: HealthStatus;
  advisor_scheduler: HealthStatus;
  frontend: HealthStatus;
  api: HealthStatus;
};
type MarketDailyStatus = {
  state: "idle" | "waiting_for_cold_start" | "pending" | "running" | "partial" | "complete" | "failed" | "cancelled";
  service_status: "running" | "offline";
  lease_active: boolean;
  lease_expires_at: string | null;
  latest_observed_session: string | null;
  last_successful_update: string | null;
  next_scheduled_at: string;
  run_id: string | null;
  mode: "cold_start" | "catch_up" | null;
  target_session: string | null;
  total_items: number;
  completed_items: number;
  failed_items: number;
  progress: number;
  run_status: string | null;
};
type MarketDailyColdStartSubmission = {
  request_id: string;
  request_status: "pending" | "claimed" | "completed" | "failed" | "cancelled";
  message: string;
};
type MarketFailure = { code: string; status: "source_missing" | "conflicted"; attempts: number; selected_source: string | null; error: string | null; updated_at: string | null };
type ServiceEntry = { 编号: string; 状态: string; 说明: string; 日志: string[]; read_only?: boolean };
type ServiceListPayload = { 服务: ServiceEntry[] };
type CurrentState = {
  today: string;
  last_successful_data_update: string | null;
  advice_status: ReportStatus;
  advice: Advice[];
  review: { status: ReportStatus; items: unknown[] };
  ledger: {
    cash: number;
    positions: Position[];
    realized_pnl: number;
    unrealized_pnl: number;
    accounts: unknown[];
    status?: "ok" | "degraded" | "unknown";
  };
  flows: { information: StatusCount; capital: StatusCount; analyst: StatusCount };
  blocking_quality_checks: Array<{
    check_name: string;
    severity: string;
    status: string;
    created_at: string;
  }>;
  reports: ReportLink[];
  report_list: { status: ListStatus; truncated?: boolean };
  profiles: ProfileLink[];
  profile_list: { status: ListStatus };
  charts: ChartLink[];
  chart_list: { status: ListStatus };
  health: Health;
};

const CNY = new Intl.NumberFormat("zh-CN", { style: "currency", currency: "CNY" });

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isNonNegativeSafeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function isStatusCount(value: unknown): value is StatusCount {
  return isRecord(value) &&
    (value.status === "ok" || value.status === "degraded" || value.status === "unknown") &&
    Number.isSafeInteger(value.count) &&
    (value.count as number) >= 0;
}

const HEALTH_STATUSES = new Set<unknown>(["ok", "healthy", "running", "degraded", "failed", "stopped", "unknown"]);
const HEALTH_COMPONENTS = ["collector", "market_updater", "advisor_scheduler", "frontend", "api"] as const;
const HEALTH_COMPONENT_LABELS: Record<(typeof HEALTH_COMPONENTS)[number], string> = {
  collector: "采集器",
  market_updater: "行情更新",
  advisor_scheduler: "投顾调度",
  frontend: "前端",
  api: "接口",
};
const HEALTH_KEYS = new Set(["status", "service", ...HEALTH_COMPONENTS]);

function isHealth(value: unknown): value is Health {
  return isRecord(value) &&
    Object.keys(value).length === HEALTH_KEYS.size &&
    Object.keys(value).every((key) => HEALTH_KEYS.has(key)) &&
    value.status === "ok" &&
    value.service === "advisor-api" &&
    HEALTH_STATUSES.has(value.collector) &&
    HEALTH_STATUSES.has(value.market_updater) &&
    HEALTH_STATUSES.has(value.advisor_scheduler) &&
    HEALTH_STATUSES.has(value.frontend) &&
    HEALTH_STATUSES.has(value.api);
}

function isMarketDailyStatus(value: unknown): value is MarketDailyStatus {
  return isRecord(value) &&
    ["idle", "waiting_for_cold_start", "pending", "running", "partial", "complete", "failed", "cancelled"].includes(String(value.state)) &&
    (value.service_status === "running" || value.service_status === "offline") &&
    typeof value.lease_active === "boolean" &&
    (value.lease_expires_at === null || isBoundedTimestamp(value.lease_expires_at)) &&
    (value.latest_observed_session === null || isDateString(value.latest_observed_session)) &&
    (value.last_successful_update === null || isBoundedTimestamp(value.last_successful_update)) &&
    isBoundedTimestamp(value.next_scheduled_at) &&
    (value.run_id === null || (typeof value.run_id === "string" && RUN_ID.test(value.run_id))) &&
    (value.mode === null || value.mode === "cold_start" || value.mode === "catch_up") &&
    (value.target_session === null || isDateString(value.target_session)) &&
    isNonNegativeSafeInteger(value.total_items) &&
    isNonNegativeSafeInteger(value.completed_items) &&
    isNonNegativeSafeInteger(value.failed_items) &&
    isFiniteNumber(value.progress) && value.progress >= 0 && value.progress <= 1 &&
    (value.run_status === null || typeof value.run_status === "string");
}

function isMarketDailyColdStartSubmission(value: unknown): value is MarketDailyColdStartSubmission {
  return isRecord(value) &&
    typeof value.request_id === "string" && RUN_ID.test(value.request_id) &&
    ["pending", "claimed", "completed", "failed", "cancelled"].includes(String(value.request_status)) &&
    typeof value.message === "string" && value.message.trim().length > 0 && value.message.length <= 240;
}

function marketDailyStateLabel(state: MarketDailyStatus["state"]): string {
  return {
    idle: "空闲",
    waiting_for_cold_start: "等待冷启动",
    pending: "等待执行",
    running: "执行中",
    partial: "部分完成",
    complete: "已完整完成",
    failed: "失败",
    cancelled: "已取消",
  }[state];
}

function isMarketFailure(value: unknown): value is MarketFailure {
  return isRecord(value) && typeof value.code === "string" && A_SHARE_CODE.test(value.code) &&
    (value.status === "source_missing" || value.status === "conflicted") &&
    isNonNegativeSafeInteger(value.attempts) &&
    (value.selected_source === null || typeof value.selected_source === "string") &&
    (value.error === null || typeof value.error === "string") &&
    (value.updated_at === null || isBoundedTimestamp(value.updated_at));
}

function isServiceEntries(value: unknown): value is ServiceListPayload {
  return isRecord(value) && Array.isArray(value.服务) && value.服务.every((item) =>
    isRecord(item) && typeof item.编号 === "string" && typeof item.状态 === "string" &&
    typeof item.说明 === "string" && Array.isArray(item.日志) && item.日志.every((path) => typeof path === "string")
  );
}

const A_SHARE_CODE = /^[0368]\d{5}$/;
const ASSET_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const REPORT_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;
const RUN_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const ISO_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/;

function isBoundedTimestamp(value: unknown): value is string {
  return typeof value === "string" &&
    value.length <= 64 &&
    ISO_TIMESTAMP.test(value) &&
    isDateString(value.slice(0, 10)) &&
    Number.isFinite(Date.parse(value));
}

function isDateString(value: unknown): value is string {
  if (typeof value !== "string") return false;
  const match = REPORT_DATE.exec(value);
  if (!match) return false;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return year >= 1 && month >= 1 && month <= 12 && day >= 1 && day <= days[month - 1];
}

function isReportLink(value: unknown): value is ReportLink {
  if (!isRecord(value) ||
    typeof value.report_date !== "string" ||
    !isDateString(value.report_date) ||
    (value.report_type !== "premarket" && value.report_type !== "review") ||
    typeof value.run_id !== "string" ||
    !RUN_ID.test(value.run_id) ||
    (value.quality_status !== "passed" && value.quality_status !== "blocked") ||
    typeof value.href !== "string") {
    return false;
  }
  return value.href === `/api/reports/${value.report_date}/${value.report_type}?run_id=${value.run_id}`;
}

function isProfileLink(value: unknown): value is ProfileLink {
  return isRecord(value) &&
    typeof value.code === "string" &&
    A_SHARE_CODE.test(value.code) &&
    typeof value.name === "string" &&
    typeof value.href === "string" &&
    value.href === `/api/profiles/${value.code}`;
}

function isChartLink(value: unknown): value is ChartLink {
  return isRecord(value) &&
    typeof value.asset_id === "string" &&
    ASSET_ID.test(value.asset_id) &&
    typeof value.code === "string" &&
    A_SHARE_CODE.test(value.code) &&
    value.chart_type === "kline" &&
    isDateString(value.as_of) &&
    typeof value.href === "string" &&
    value.href === `/api/charts/${value.asset_id}`;
}

function parseCurrentState(value: unknown): CurrentState {
  if (!isRecord(value)) throw new Error("当前状态格式无效");
  const ledger = value.ledger;
  const flows = value.flows;
  const review = value.review;
  const reportList = value.report_list;
  const profileList = value.profile_list;
  const chartList = value.chart_list;
  if (
    !isDateString(value.today) ||
    (value.last_successful_data_update !== null && !isBoundedTimestamp(value.last_successful_data_update)) ||
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
    (ledger.status !== undefined && !["ok", "degraded", "unknown"].includes(String(ledger.status))) ||
    !isRecord(flows) ||
    !isStatusCount(flows.information) ||
    !isStatusCount(flows.capital) ||
    !isStatusCount(flows.analyst) ||
    !Array.isArray(value.blocking_quality_checks) ||
    !Array.isArray(value.reports) ||
    !isRecord(reportList) ||
    (reportList.status !== "ok" && reportList.status !== "degraded") ||
    (reportList.truncated !== undefined && typeof reportList.truncated !== "boolean") ||
    !Array.isArray(value.profiles) ||
    !isRecord(profileList) ||
    (profileList.status !== "ok" && profileList.status !== "degraded") ||
    !Array.isArray(value.charts) ||
    !isRecord(chartList) ||
    (chartList.status !== "ok" && chartList.status !== "degraded") ||
    !isHealth(value.health)
  ) {
    throw new Error("当前状态格式无效");
  }
  const validAdvice = value.advice.every(
    (item) =>
      isRecord(item) &&
      typeof item.advice_id === "string" &&
      typeof item.code === "string" &&
      A_SHARE_CODE.test(item.code) &&
      typeof item.action === "string" &&
      isFiniteNumber(item.confidence) &&
      item.confidence >= 0 &&
      item.confidence <= 1 &&
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
      item.severity === "blocking" &&
      item.status === "failed" &&
      isBoundedTimestamp(item.created_at),
  );
  const adviceFieldsAgree = value.advice_status === "passed"
    ? value.blocking_quality_checks.length === 0
    : value.advice.length === 0;
  if (
    !validAdvice ||
    !validPositions ||
    !validChecks ||
    !adviceFieldsAgree ||
    (reportList.status === "degraded" && value.reports.length !== 0) ||
    (profileList.status === "degraded" && value.profiles.length !== 0) ||
    (chartList.status === "degraded" && value.charts.length !== 0) ||
    !value.reports.every(isReportLink) ||
    !value.profiles.every(isProfileLink) ||
    !value.charts.every(isChartLink)
  ) {
    throw new Error("当前状态格式无效");
  }
  return value as CurrentState;
}

const STATUS_LABELS = {
  passed: "已通过",
  blocked: "已阻断",
  missing: "缺失",
  ok: "正常",
  healthy: "健康",
  running: "运行中",
  degraded: "降级",
  failed: "失败",
  stopped: "已停止",
  unknown: "未知",
} satisfies Record<HealthStatus | ReportStatus | FlowStatus | "failed", string>;

function statusLabel(status: string) {
  return STATUS_LABELS[status as keyof typeof STATUS_LABELS] ?? status;
}

function statusClass(status: string) {
  if (["ok", "healthy", "passed", "running"].includes(status)) return "status-ok";
  if (["blocked", "failed"].includes(status)) return "status-danger";
  if (["degraded", "missing", "stopped"].includes(status)) return "status-warn";
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

function ManualLedgerForm({ onSaved }: { onSaved: () => Promise<boolean> }) {
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
      if (!await onSaved()) {
        throw new Error("流水已保存，但状态刷新失败；输入已保留，请刷新确认");
      }
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

function LedgerImportForm({ onImported }: { onImported: () => Promise<boolean> }) {
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
      if (!await onImported()) {
        throw new Error("JSON 流水已导入，但状态刷新失败；内容已保留，请刷新确认");
      }
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

type AppRoute = "/" | "/mx" | "/research";
type Navigate = (target: AppRoute, anchor?: string) => void;

function routeFor(pathname: string): AppRoute {
  return pathname === "/mx" || pathname === "/research" ? pathname : "/";
}

function Navigation({ route, navigate }: { route: AppRoute; navigate: Navigate }) {
  function follow(event: MouseEvent<HTMLAnchorElement>, target: AppRoute, anchor?: string) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigate(target, anchor);
  }
  const entries: Array<{ target: AppRoute; label: string; icon: ReactNode; anchor?: string; current?: boolean }> = [
    { target: "/", label: "运行概览", icon: <LayoutDashboard size={18} />, current: route === "/" },
    { target: "/mx", label: "MX 资讯", icon: <MessageSquareText size={18} />, current: route === "/mx" },
    { target: "/research", label: "研究中心", icon: <Microscope size={18} />, current: route === "/research" },
    { target: "/research", anchor: "configuration", label: "研究配置", icon: <Settings2 size={18} /> },
  ];
  return <nav className="side-navigation" aria-label="顶层导航">{entries.map(({ target, label, icon, anchor, current }) => <a key={`${target}-${label}`} href={target} aria-label={label === "MX 资讯" ? "MX 监听" : undefined} aria-current={current ? "page" : undefined} onClick={(event) => follow(event, target, anchor)}>{icon}<span>{label}</span>{label === "MX 资讯" && <i aria-hidden="true" />}</a>)}</nav>;
}

function RouteShell({ route, navigate, children }: { route: AppRoute; navigate: Navigate; children: ReactNode }) {
  return <div className="app-shell">
    <aside className="app-sidebar">
      <div className="brand"><span className="brand-mark" aria-hidden="true">A</span><span><strong>A Hunter</strong><small>LOCAL ADVISOR</small></span></div>
      <p className="sidebar-label">WORKSPACE</p>
      <Navigation route={route} navigate={navigate} />
      <div className="sidebar-divider" />
      <p className="sidebar-label">QUICK ACTIONS</p>
      <nav className="quick-actions" aria-label="快捷操作">
        <a href="/#ledger-entry" onClick={(event) => followQuickAction(event, navigate, "ledger-entry")}><Plus size={18} /><span>新增流水</span></a>
        <a href="/#ledger-import" onClick={(event) => followQuickAction(event, navigate, "ledger-import")}><FileJson2 size={18} /><span>导入 JSON</span></a>
      </nav>
      <div className="sidebar-status"><div><span aria-hidden="true" /><strong>本地服务已连接</strong></div><small>单用户 · 只读投顾 · 不执行交易</small></div>
    </aside>
    <main className="app-content">{children}</main>
  </div>;
}

function followQuickAction(event: MouseEvent<HTMLAnchorElement>, navigate: Navigate, anchor: string) {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  navigate("/", anchor);
}

function isStaleData(today: string, updatedAt: string | null): boolean {
  if (!updatedAt) return true;
  const todayStart = Date.parse(`${today}T00:00:00+08:00`);
  const updated = Date.parse(updatedAt);
  return Number.isFinite(todayStart) && Number.isFinite(updated) && todayStart - updated > 36 * 60 * 60 * 1_000;
}

function compactTimestamp(value: string | null): string {
  return value ? value.slice(0, 16).replace("T", " ") : "暂无数据";
}

function serviceDisplayName(service: ServiceEntry): string {
  if (service.编号 === "market-daily") return "Market Daily";
  if (service.编号 === "mx-listener") return "MX Listener";
  if (service.编号 === "research") return "Research Service";
  return service.编号;
}

function serviceStatusClass(status: string): string {
  if (status === "运行中") return "status-ok";
  if (status === "需检查" || status === "部分完成") return "status-warn";
  return "status-muted";
}

function OverviewPage({ navigate }: { navigate: Navigate }) {
  const [state, setState] = useState<CurrentState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [marketDaily, setMarketDaily] = useState<MarketDailyStatus | null>(null);
  const [marketFailures, setMarketFailures] = useState<MarketFailure[]>([]);
  const [services, setServices] = useState<ServiceEntry[] | null>(null);
  const [marketStartPending, setMarketStartPending] = useState(false);
  const [marketStartMessage, setMarketStartMessage] = useState<{ kind: "success" | "error"; text: string } | null>(null);

  const loadState = useCallback(async (signal?: AbortSignal) => {
    setIsRefreshing(true);
    try {
      const result = await fetch("/api/current-state", { signal });
      if (!result.ok) throw new Error(`请求失败 (${result.status})`);
      const nextState = parseCurrentState(await result.json());
      setState(nextState);
      setError(null);
      return true;
    } catch (caught) {
      if (caught instanceof DOMException && caught.name === "AbortError") return false;
      setError(caught instanceof Error ? caught.message : "未知错误");
      return false;
    } finally {
      setIsRefreshing(false);
    }
  }, []);

  const loadServiceState = useCallback(async (signal?: AbortSignal) => {
    try {
      const marketResponse = await fetch("/api/market-daily/status", { signal });
      if (!marketResponse.ok) throw new Error(await apiError(marketResponse));
      const marketPayload: unknown = await marketResponse.json();
      if (!isMarketDailyStatus(marketPayload)) throw new Error("Market Daily 状态格式无效");
      setMarketDaily(marketPayload);
      if (marketPayload.run_id && marketPayload.failed_items > 0) {
        const failuresResponse = await fetch(`/api/market-daily/runs/${marketPayload.run_id}/failures?limit=20`, { signal });
        if (!failuresResponse.ok) throw new Error(await apiError(failuresResponse));
        const failuresPayload: unknown = await failuresResponse.json();
        if (!isRecord(failuresPayload) || !Array.isArray(failuresPayload.items) || !failuresPayload.items.every(isMarketFailure)) {
          throw new Error("Market Daily 失败项格式无效");
        }
        setMarketFailures(failuresPayload.items);
      } else {
        setMarketFailures([]);
      }
      const servicesResponse = await fetch("/api/services", { signal });
      if (!servicesResponse.ok) throw new Error(await apiError(servicesResponse));
      const servicesPayload: unknown = await servicesResponse.json();
      if (!isServiceEntries(servicesPayload)) throw new Error("服务状态格式无效");
      setServices(servicesPayload.服务);
      return true;
    } catch (caught) {
      if (caught instanceof DOMException && caught.name === "AbortError") return false;
      // 行情状态卡片独立降级，不影响账户与报告的既有只读页面。
      setMarketDaily(null);
      setMarketFailures([]);
      setServices(null);
      return false;
    }
  }, []);

  const submitMarketDailyColdStart = useCallback(async () => {
    setMarketStartPending(true);
    setMarketStartMessage(null);
    try {
      const response = await fetch("/api/market-daily/cold-start", { method: "POST" });
      if (!response.ok) throw new Error(await apiError(response));
      const payload: unknown = await response.json();
      if (!isMarketDailyColdStartSubmission(payload)) throw new Error("冷启动请求响应格式无效");
      const refreshed = await loadServiceState();
      setMarketStartMessage({
        kind: "success",
        text: refreshed ? payload.message : `${payload.message}，但状态刷新失败，请刷新确认`,
      });
    } catch (caught) {
      setMarketStartMessage({ kind: "error", text: caught instanceof Error ? caught.message : "冷启动请求提交失败" });
    } finally {
      setMarketStartPending(false);
    }
  }, [loadServiceState]);

  const refreshAll = useCallback(async (signal?: AbortSignal) => {
    const stateLoaded = await loadState(signal);
    if (stateLoaded) await loadServiceState(signal);
  }, [loadServiceState, loadState]);

  useEffect(() => {
    const controller = new AbortController();
    void refreshAll(controller.signal);
    return () => controller.abort();
  }, [refreshAll]);

  useEffect(() => {
    if (!state || !window.location.hash) return;
    const target = document.getElementById(window.location.hash.slice(1));
    if (typeof target?.scrollIntoView === "function") target.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [state]);

  if (!state && !error) return <section className="overview-state" aria-live="polite"><span className="loading-spinner" aria-hidden="true" />正在读取当前状态…</section>;
  if (!state) return <section className="overview-state" role="alert"><AlertTriangle size={24} /><strong>当前状态读取失败</strong><span>{error}</span><button type="button" onClick={() => void refreshAll()} disabled={isRefreshing}><RefreshCw size={16} />{isRefreshing ? "正在重试" : "重试读取"}</button></section>;

  const snapshotCurrent = error === null;
  const ledgerAvailable = snapshotCurrent && state.ledger.status === "ok";
  const knownValues = ledgerAvailable
    ? state.ledger.positions.flatMap((position) => position.market_value === null ? [] : [position.market_value])
    : [];
  const exposure = ledgerAvailable && knownValues.length === state.ledger.positions.length
    ? knownValues.reduce((sum, value) => sum + value, 0)
    : null;
  const adviceAvailable = snapshotCurrent && state.advice_status === "passed" && state.blocking_quality_checks.length === 0;
  const reports = snapshotCurrent && state.report_list.status === "ok" ? state.reports : [];
  const profiles = snapshotCurrent && state.profile_list.status === "ok" ? state.profiles : [];
  const charts = snapshotCurrent && state.chart_list.status === "ok" ? state.charts : [];
  const dataStale = snapshotCurrent && isStaleData(state.today, state.last_successful_data_update);
  const reportStatus = snapshotCurrent ? state.review.status : "unknown";
  const latestReport = reports[0];
  const researchService = services?.find((service) => service.编号 === "research");
  const attentionItems = [
    marketDaily && marketDaily.failed_items > 0 ? `行情${marketDaily.mode === "cold_start" ? "冷启动" : "同步"}存在 ${marketDaily.failed_items} 个缺口` : null,
    snapshotCurrent && (!adviceAvailable || state.blocking_quality_checks.length > 0) ? "今日建议处于阻断状态" : null,
    dataStale ? "当前数据更新时间已滞后" : null,
  ].filter((item): item is string => item !== null);

  return (
    <div className="overview-page">
      <header className="overview-header">
        <div><h1>运行概览</h1><p>从建议、数据到服务，优先显示需要你处理的事项</p></div>
        <div className="overview-header-actions">
          <time dateTime={state.today}>{snapshotCurrent ? state.today : "日期暂不可确认"}</time>
          <span className={`report-pill ${statusClass(reportStatus)}`}><i aria-hidden="true" />报告{snapshotCurrent ? statusLabel(state.review.status) : "刷新失败"}</span>
          <span className="sr-only">报告 {snapshotCurrent ? statusLabel(state.review.status) : "刷新失败"}</span>
          <button type="button" className="refresh-button" aria-label="刷新当前状态" title="刷新当前状态" onClick={() => void refreshAll()} disabled={isRefreshing}><RefreshCw className={isRefreshing ? "spin" : undefined} size={16} />刷新</button>
          <span className="sr-only">{snapshotCurrent ? `最近数据更新 ${state.last_successful_data_update ?? "暂无数据"}` : "最近数据更新 暂不可确认"}</span>
        </div>
      </header>

      {(!snapshotCurrent || dataStale) && <section className="stale-banner overview-stale-banner" role="alert"><span className="stale-icon"><AlertTriangle size={17} /></span><div><strong>{snapshotCurrent ? "数据已滞后" : "状态刷新失败，先前快照已停用"}</strong><span>{snapshotCurrent ? `最近更新于 ${compactTimestamp(state.last_successful_data_update)}，建议和资产估值可能不是最新状态` : "请刷新成功后再查看建议、账户指标和资源链接"}</span></div><a href="#runtime-details">查看影响范围 <ArrowRight size={14} /></a></section>}

      <section className="overview-hero-grid" aria-label="今日决策与系统摘要">
        <article className="overview-card decision-card" id="advice-details">
          <span className={`attention-pill ${adviceAvailable ? "ready" : "blocked"}`}><i aria-hidden="true" />{adviceAvailable ? "建议可用" : "需要处理"}</span>
          <div className="decision-copy">
            <h2>{adviceAvailable ? (state.advice.length > 0 ? `08:30 建议 · ${state.advice.length} 条` : "08:30 暂无建议") : "08:30 建议已阻断"}</h2>
            {!adviceAvailable ? <><p>今日暂无可执行建议。先确认行情完整性，再重新生成研究结论。</p><span className="sr-only">{snapshotCurrent && state.advice_status === "blocked" ? "建议已阻断" : "建议不可用"}</span></> : state.advice.length === 0 ? <p>当前没有新增操作建议，系统将继续等待下一次研究结果。</p> : <ul>{state.advice.slice(0, 2).map((item) => <li key={item.advice_id}><strong>{item.code}</strong><span>{item.action} · {Math.round(item.confidence * 100)}%</span><span className="advice-rationale">{item.rationale}</span></li>)}</ul>}
          </div>
          {!adviceAvailable && <a className="primary-link" href="#quality-checks">查看阻断原因 <ArrowRight size={15} /></a>}
        </article>
        <article className="overview-card system-summary-card">
          <div className="card-heading"><h2>系统摘要</h2><span>{services ? `${services.length} 项服务` : "状态不可用"}</span></div>
          {!services ? <p className="unavailable-message">服务组状态暂不可用</p> : services.length === 0 ? <p className="empty">暂无已配置服务</p> : <dl>{services.slice(0, 3).map((item) => <div key={item.编号}><dt>{serviceDisplayName(item)}{item.read_only ? "（只读）" : ""}</dt><dd className={serviceStatusClass(item.状态)}>{item.状态}</dd></div>)}</dl>}
        </article>
      </section>

      <section className="overview-metrics" aria-label="投资概览">
        {!ledgerAvailable && <span className="sr-only">{state.ledger.status === "degraded" ? "账户数据已降级" : "账户数据状态未知"}</span>}
        {!ledgerAvailable && <span className="sr-only">持仓数据不可用</span>}
        <article><span>可用现金</span><strong>{ledgerAvailable ? CNY.format(state.ledger.cash) : "暂无数据"}</strong><small>{ledgerAvailable ? "账户余额" : "账户数据不可用"}</small></article>
        <article><span>持仓市值</span><strong>{ledgerAvailable && exposure !== null ? CNY.format(exposure) : "暂无数据"}</strong><small>{ledgerAvailable && state.ledger.positions.length > 0 ? `${state.ledger.positions.length} 个持仓` : "暂无持仓"}</small></article>
        <article><span>已实现盈亏</span><strong>{ledgerAvailable ? CNY.format(state.ledger.realized_pnl) : "暂无数据"}</strong><small>累计已实现</small></article>
        <article><span>未实现盈亏</span><strong>{ledgerAvailable ? CNY.format(state.ledger.unrealized_pnl) : "暂无数据"}</strong><small>{exposure ? "按最近价格估值" : "无市场敞口"}</small></article>
      </section>

      <section className="overview-main-grid" aria-label="行情服务与关注事项">
        <article className="overview-card market-daily-panel">
          <div className="card-heading market-heading"><div><h2><span>Market</span>{" "}<span>Daily</span><span className="sr-only"> 行情服务</span></h2><span>{marketDaily ? `${marketDaily.mode === "cold_start" ? "五年冷启动" : marketDaily.mode === "catch_up" ? "日常补洞" : "尚未创建运行"} · 目标交易日 ${marketDaily.target_session ?? "暂无"}` : "行情服务状态暂不可用"}</span></div>{marketDaily && <strong className={`state-pill ${statusClass(marketDaily.state === "complete" ? "ok" : marketDaily.state === "partial" || marketDaily.state === "failed" ? "failed" : marketDaily.service_status === "running" ? "running" : "unknown")}`}>{marketDailyStateLabel(marketDaily.state)}</strong>}</div>
          {!marketDaily ? <div className="service-empty"><AlertTriangle size={20} /><p>行情服务状态暂不可用</p><button type="button" className="secondary-button" onClick={() => void loadServiceState()}>重试</button></div> : <>
            <progress value={marketDaily.progress} max={1} aria-label="Market Daily 进度" />
            <dl className="market-summary-metrics"><Metric label="完成" value={`${marketDaily.completed_items} / ${marketDaily.total_items}`} /><Metric label="失败" value={`${marketDaily.failed_items}`} /><Metric label="下次检查" value={marketDaily.next_scheduled_at.slice(5, 16).replace("T", " ")} /></dl>
            {marketDaily.state === "idle" && <div className="market-daily-actions"><p>提交后只写入本地队列；服务会在 21:00 后执行五年同步。</p><button type="button" onClick={() => void submitMarketDailyColdStart()} disabled={marketStartPending}><Activity size={16} />{marketStartPending ? "正在提交" : "启动五年同步"}</button></div>}
            {marketDaily.state === "waiting_for_cold_start" && <div className="market-daily-actions"><button type="button" disabled><Activity size={16} />冷启动已排队</button><p className="market-daily-queued">冷启动请求已排队，服务将在 21:00 后执行</p></div>}
            <div className="market-failures" id="market-failures"><h3>待排查证券</h3>{marketFailures.length === 0 ? <p className="empty">当前没有待排查证券</p> : <div className="table-wrap"><table><thead><tr><th>代码</th><th>状态</th><th>尝试</th><th>原因</th></tr></thead><tbody>{marketFailures.slice(0, 3).map((item) => <tr key={item.code}><td>{item.code}</td><td className="status-warn">{item.status === "conflicted" ? "数据冲突" : "来源未证明"}</td><td>{item.attempts}</td><td>{item.error ?? "暂无说明"}</td></tr>)}</tbody></table></div>}</div>
          </>}
          {marketStartMessage && <p className={`market-daily-message ${marketStartMessage.kind}`} role={marketStartMessage.kind === "error" ? "alert" : "status"}>{marketStartMessage.text}</p>}
        </article>
        <article className="overview-card attention-card">
          <div className="card-heading"><h2>需要关注</h2><span>{attentionItems.length} 项</span></div>
          <div className="attention-list">{attentionItems.length === 0 ? <div className="attention-empty"><HeartPulse size={22} /><strong>当前无待处理事项</strong><span>关键数据与运行状态未发现阻断</span></div> : attentionItems.slice(0, 3).map((item, index) => <a href={index === 0 && marketDaily?.failed_items ? "#market-failures" : "#quality-checks"} className={item.includes("建议") ? "danger" : "warning"} key={item}><i aria-hidden="true" /><span><strong>{item}</strong><small>{item.includes("建议") ? "查看质量门禁及停止原因" : "建议在下一次研究前完成排查"}</small></span></a>)}</div>
          <h3>快捷入口</h3><div className="attention-actions"><button type="button" onClick={() => navigate("/mx")}><MessageSquareText size={16} />查看 MX 资讯</button><button type="button" className="secondary-button" onClick={() => navigate("/research")}><Microscope size={16} />进入研究中心</button></div>
        </article>
      </section>

      <section className="overview-bottom-grid">
        <article className="overview-card asset-overview"><h2>资产概览</h2>{!ledgerAvailable ? <p className="unavailable-message">资产数据暂不可用</p> : state.ledger.positions.length === 0 ? <div className="asset-empty"><span><CircleDollarSign size={28} /></span><div><strong>暂无持仓</strong><p>新增流水后，这里会展示持仓与盈亏变化</p><a href="#ledger-entry">新增流水 <ArrowRight size={14} /></a></div></div> : <div className="compact-positions"><div className="table-wrap"><table><thead><tr><th>代码</th><th>数量</th><th>现价</th><th>市值</th><th>浮动盈亏</th></tr></thead><tbody>{state.ledger.positions.slice(0, 3).map((position) => <tr key={position.code}><td>{position.code}</td><td>{position.quantity}</td><td>{position.market_price === null ? "暂无数据" : CNY.format(position.market_price)}</td><td>{position.market_value === null ? "暂无数据" : CNY.format(position.market_value)}</td><td>{CNY.format(position.unrealized_pnl)}</td></tr>)}</tbody></table></div></div>}</article>
        <article className="overview-card research-summary"><h2>研究与报告</h2><div><section><span>最新报告</span>{latestReport ? <a aria-label="打开最新报告" href={latestReport.href} target="_blank" rel="noreferrer">{latestReport.report_date} {latestReport.report_type === "premarket" ? "盘前" : "复盘"}<ExternalLink size={14} /></a> : <strong>暂无可用报告</strong>}<small>{latestReport ? statusLabel(latestReport.quality_status) : "阻断报告不会发布"}</small></section><section><span>研究服务</span><strong>{researchService?.状态 ?? "状态暂不可用"}</strong><button type="button" className="text-button" onClick={() => navigate("/research")}>进入研究中心 <ArrowRight size={14} /></button></section></div></article>
      </section>

      <section className="runtime-details" id="runtime-details" aria-label="运行详情">
        <section className="flow-grid" aria-label="三流状态">
          {([["信息流", state.flows.information], ["资金流", state.flows.capital], ["分析流", state.flows.analyst]] as const).map(([label, flow]) => <div className="flow-item" key={label}><span>{label}</span><strong className={statusClass(snapshotCurrent ? flow.status : "unknown")}>{snapshotCurrent ? statusLabel(flow.status) : "不可用"}</strong><small>{snapshotCurrent && flow.status === "ok" ? `${flow.count} 条` : "暂无数据"}</small></div>)}
        </section>
        <div className="runtime-card-grid">
          <article className="overview-card quality-panel" id="quality-checks"><h2><ShieldAlert size={18} />质量检查</h2>{!snapshotCurrent ? <p className="unavailable-message">质量状态不可用</p> : state.blocking_quality_checks.length === 0 ? <p className="healthy">无阻断项</p> : <ul className="plain-list">{state.blocking_quality_checks.map((check) => <li key={`${check.check_name}-${check.created_at}`}><strong>{check.check_name}</strong><span>{statusLabel(check.status)}</span><small>{check.created_at}</small></li>)}</ul>}</article>
          <article className="overview-card health-panel"><h2><HeartPulse size={18} />进程健康</h2>{!snapshotCurrent ? <p className="unavailable-message">进程状态不可用</p> : <dl className="status-list">{HEALTH_COMPONENTS.map((name) => <div key={name}><dt>{HEALTH_COMPONENT_LABELS[name]}</dt><dd className={statusClass(state.health[name])}>{statusLabel(state.health[name])}</dd></div>)}</dl>}</article>
        </div>
      </section>

      <section className="resources overview-resources">
        <div><h2><BookOpen size={18} />报告</h2>{!snapshotCurrent ? <p className="unavailable-message">报告资源不可用，当前快照已失效</p> : state.report_list.status !== "ok" ? <p className="unavailable-message">报告列表已降级，暂不可用</p> : reports.length === 0 ? <p className="empty">暂无报告</p> : <ul className="link-list">{reports.map((report) => <li key={report.href}><a href={report.href} target="_blank" rel="noreferrer">{report.report_date} {report.report_type === "premarket" ? "盘前" : "复盘"}<ExternalLink size={14} /></a></li>)}</ul>}</div>
        <div><h2><Users size={18} />股票档案</h2>{!snapshotCurrent ? <p className="unavailable-message">档案资源不可用，当前快照已失效</p> : state.profile_list.status !== "ok" ? <p className="unavailable-message">档案列表已降级，暂不可用</p> : profiles.length === 0 ? <p className="empty">暂无档案</p> : <ul className="link-list">{profiles.map((profile) => <li key={profile.href}><a href={profile.href} target="_blank" rel="noreferrer">{profile.code} {profile.name}<ExternalLink size={14} /></a></li>)}</ul>}</div>
        <div><h2><Activity size={18} />K线图</h2>{!snapshotCurrent ? <p className="unavailable-message">图表资源不可用，当前快照已失效</p> : state.chart_list.status !== "ok" ? <p className="unavailable-message">图表列表已降级，暂不可用</p> : charts.length === 0 ? <p className="empty">暂无图表</p> : <ul className="link-list">{charts.map((chart) => <li key={chart.href}><a href={chart.href} target="_blank" rel="noreferrer">{chart.code} K线 · {chart.as_of}<ExternalLink size={14} /></a></li>)}</ul>}</div>
      </section>
      <section className="ledger-tools overview-ledger-tools">
        <div id="ledger-entry"><ManualLedgerForm onSaved={() => loadState()} /></div>
        <div id="ledger-import"><LedgerImportForm onImported={() => loadState()} /></div>
      </section>
    </div>
  );
}

function App() {
  const [route, setRoute] = useState<AppRoute>(() => routeFor(window.location.pathname));
  const [researchInstructionsDirty, setResearchInstructionsDirty] = useState(false);

  useEffect(() => {
    if (window.location.pathname !== "/" && window.location.pathname !== "/mx" && window.location.pathname !== "/research") {
      window.history.replaceState({}, "", "/");
    }
    const onPopState = () => {
      const target = routeFor(window.location.pathname);
      if (route === "/research" && target !== "/research" && researchInstructionsDirty && !window.confirm("放弃未发布的 Agent Instructions 草稿？")) {
        window.history.pushState({}, "", "/research");
        return;
      }
      if (target !== "/research") setResearchInstructionsDirty(false);
      setRoute(target);
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [researchInstructionsDirty, route]);

  const navigate = useCallback((target: AppRoute, anchor?: string) => {
    if (window.location.pathname === target && window.location.hash === (anchor ? `#${anchor}` : "")) return;
    if (route === "/research" && target !== "/research" && researchInstructionsDirty && !window.confirm("放弃未发布的 Agent Instructions 草稿？")) return;
    if (target !== "/research") setResearchInstructionsDirty(false);
    window.history.pushState({}, "", `${target}${anchor ? `#${anchor}` : ""}`);
    setRoute(target);
    if (anchor) window.setTimeout(() => {
      const targetElement = document.getElementById(anchor);
      if (typeof targetElement?.scrollIntoView === "function") targetElement.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 0);
  }, [researchInstructionsDirty, route]);

  if (route === "/mx") return <RouteShell route={route} navigate={navigate}><MxOperationsPage /></RouteShell>;
  if (route === "/research") return <RouteShell route={route} navigate={navigate}><ResearchConfigurationPage onInstructionsDirtyChange={setResearchInstructionsDirty} /></RouteShell>;
  return <RouteShell route={route} navigate={navigate}><OverviewPage navigate={navigate} /></RouteShell>;
}

export default App;
