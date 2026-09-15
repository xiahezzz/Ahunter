export type JsonObject = Record<string, unknown>;
export type Cursor = string | number | null;
export type Page<T> = { items: T[]; next: Cursor; total?: number };
export type Draft = { experiment_id: string; name: string; created_at: string };
export type Node = { record_id: string; proposal_id: string; parent_proposal_id: string | null; hypothesis: string;
  source: string; package_hash: string; diff_artifact_hash: string | null; test_count: number;
  test_status_counts: Record<string, number>; current_baseline: boolean; initial_baseline: boolean };
export type Test = { record_id: string; task_id: string; role: string; purpose: string; repeat_index: number;
  status: string; rerun_of: string | null; eligible_for_original_comparison: boolean; detail_hidden: boolean };
export type Comparison = { record_id: string; baseline: string; candidate: string; result_id: string | null;
  status: string; feedback: { decision: string; mean_improvement: string | null } | null };
export type Selection = { record_id: string; operation: string; current_baseline_id?: string; comparison_id?: string; reason?: string };
export type Preflight = { record_id: string; created_at: string; status: string; checks: { code: string; message: string; status: string }[] };

export function object(value: unknown): JsonObject {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error("实验数据格式不完整，请刷新后重试。");
  return value as JsonObject;
}
function text(value: unknown): string {
  if (typeof value !== "string") throw new Error("实验文本字段无效。");
  return value;
}
function nullable(value: unknown): string | null { return value === null ? null : text(value); }
function count(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) throw new Error("实验计数字段无效。");
  return value;
}
function flag(value: unknown): boolean {
  if (typeof value !== "boolean") throw new Error("实验状态字段无效。");
  return value;
}
function rows(value: unknown): unknown[] {
  if (!Array.isArray(value) || value.length > 200) throw new Error("实验列表格式无效。");
  return value;
}
function page<T>(value: unknown, parse: (row: JsonObject) => T, offset = false): Page<T> {
  const data = object(value);
  const next = data[offset ? "next_offset" : "next_cursor"];
  if (!offset && data.execution_available !== false) throw new Error("实验执行状态与当前界面不兼容。");
  return { items: rows(data.items).map((row) => parse(object(row))), next: next === null ? null : offset ? count(next) : text(next),
    total: data.total === undefined ? undefined : count(data.total) };
}
export const parseDrafts = (value: unknown) => page<Draft>(value, (r) => ({ experiment_id: text(r.experiment_id), name: text(r.name), created_at: text(r.created_at) }), true);
export const parseNodes = (value: unknown) => page<Node>(value, (r) => ({ record_id: text(r.record_id), proposal_id: text(r.proposal_id),
  parent_proposal_id: nullable(r.parent_proposal_id), hypothesis: text(r.hypothesis), source: text(r.source), package_hash: text(r.package_hash),
  diff_artifact_hash: nullable(r.diff_artifact_hash), test_count: count(r.test_count), current_baseline: flag(r.current_baseline),
  initial_baseline: flag(r.initial_baseline), test_status_counts: Object.fromEntries(Object.entries(object(r.test_status_counts)).map(([k, v]) => [k, count(v)])) }));
export const parseTests = (value: unknown) => page<Test>(value, (r) => ({ record_id: text(r.record_id), task_id: text(r.task_id), role: text(r.role),
  purpose: text(r.purpose), repeat_index: count(r.repeat_index), status: text(r.status), rerun_of: nullable(r.rerun_of),
  eligible_for_original_comparison: flag(r.eligible_for_original_comparison), detail_hidden: flag(r.detail_hidden) }));
export const parseComparisons = (value: unknown) => page<Comparison>(value, (r) => {
  const feedback = r.feedback === null ? null : object(r.feedback);
  const mean = feedback ? nullable(feedback.mean_improvement) : null;
  if (mean !== null && (mean.trim() === "" || !Number.isFinite(Number(mean)))) throw new Error("比较收益字段无效。");
  return { record_id: text(r.record_id), baseline: text(r.baseline), candidate: text(r.candidate), result_id: nullable(r.result_id),
    status: text(r.status), feedback: feedback ? { decision: text(feedback.decision), mean_improvement: mean } : null };
});
export const parseSelections = (value: unknown) => page<Selection>(value, (r) => ({ record_id: text(r.record_id), operation: text(r.operation),
  current_baseline_id: r.current_baseline_id === undefined ? undefined : text(r.current_baseline_id),
  comparison_id: r.comparison_id === undefined ? undefined : text(r.comparison_id), reason: r.reason === undefined ? undefined : text(r.reason) }));
export const parsePreflights = (value: unknown) => page<Preflight>(value, (r) => ({ record_id: text(r.record_id), created_at: text(r.created_at), status: text(r.status),
  checks: rows(r.checks).map((c) => { const check = object(c); return { code: text(check.code), message: text(check.message), status: text(check.status) }; }) }), true);

export const labels: Record<string, string> = { created: "未运行", preflight: "预检中", queued: "排队中", running: "运行中", evaluating: "等待评估",
  completed: "已完成", blocked: "已阻断", failed: "失败", cancelled: "已取消", tuning: "调优", selection_validation: "选择验证",
  final_holdout: "最终留出", calibration: "成本标定", initialize: "固定初始基线", promote: "晋级", retain: "保留基线", finalize: "冻结最终选择",
  promoted: "已晋级", eligible: "满足比较门槛", inconclusive: "证据不充分", unstable: "改善不稳定", not_improved: "未改善",
  stale_baseline: "基线已变化", tuning_only: "仅作调优参考", selection_frozen: "选择已冻结", manual: "手工提案", coding_task: "编码任务", optimizer: "优化器提案",
  awaiting_result: "等待生成结果", required_samples_invalid: "原样本不足或无效", runtime_acceptance_incomplete: "运行资格尚未验收",
  selection_thresholds_satisfied: "满足晋级门槛", selection_revision_changed: "比较绑定的基线已变化", comparison_has_no_selection_binding: "比较未绑定选择版本",
  role_excluded_from_selection: "该任务不参与晋级", mixed_durations_require_explicit_weights: "不同周期尚未配置权重" };
export const label = (value: string) => labels[value] ?? value;
