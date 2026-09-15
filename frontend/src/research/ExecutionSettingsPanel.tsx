import { FormEvent, useEffect, useRef, useState } from "react";
import { Cpu } from "lucide-react";
import { researchApiError } from "./contracts";

type ModelOption = { model: string; reasoning_efforts: string[]; default_reasoning_effort: string };
type Settings = { policy_ref: string; model: string; reasoning_effort: string; model_options: ModelOption[] };

const effortLabels: Record<string, string> = { none: "无", minimal: "最少", low: "低", medium: "中", high: "高", xhigh: "很高", max: "最高", ultra: "Ultra" };
function effortLabel(effort: string) { return effortLabels[effort] ? `${effortLabels[effort]}（${effort}）` : effort; }

function parse(value: unknown): Settings {
  if (!value || typeof value !== "object") throw new Error("模型配置格式无效");
  const item = value as Partial<Settings>;
  const currentEffort = item.reasoning_effort;
  if (typeof item.policy_ref !== "string" || typeof item.model !== "string" || typeof item.reasoning_effort !== "string"
    || !Array.isArray(item.model_options) || !item.model_options.every((option) => option && typeof option.model === "string"
      && Array.isArray(option.reasoning_efforts) && option.reasoning_efforts.length > 0
      && option.reasoning_efforts.every((effort: unknown) => typeof effort === "string")
      && option.reasoning_efforts.includes(option.default_reasoning_effort))
    || typeof currentEffort !== "string"
    || !item.model_options.some((option) => option.model === item.model && option.reasoning_efforts.includes(currentEffort))) {
    throw new Error("模型配置格式无效");
  }
  return item as Settings;
}

export default function ExecutionSettingsPanel({ refreshToken = 0 }: { refreshToken?: number }) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState("");
  const [busy, setBusy] = useState(false);
  const dirty = useRef(false);
  const loadSequence = useRef(0);
  const [feedback, setFeedback] = useState<{ kind: "success" | "error"; text: string } | null>(null);

  async function load() {
    const sequence = ++loadSequence.current;
    setBusy(true);
    try {
      const response = await fetch("/api/research/execution-settings");
      if (!response.ok) throw new Error(await researchApiError(response, "模型配置暂不可读取"));
      const current = parse(await response.json());
      if (sequence !== loadSequence.current) return;
      setSettings(current);
      setModel(current.model);
      setEffort(current.reasoning_effort);
      dirty.current = false;
      setFeedback(null);
    } catch (error) {
      if (sequence !== loadSequence.current) return;
      setFeedback({ kind: "error", text: error instanceof Error ? error.message : "模型配置暂不可读取" });
    } finally { if (sequence === loadSequence.current) setBusy(false); }
  }

  useEffect(() => { if (!dirty.current) void load(); }, [refreshToken]);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!settings) return;
    setBusy(true);
    setFeedback(null);
    try {
      const response = await fetch("/api/research/execution-settings", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_policy_ref: settings.policy_ref, model, reasoning_effort: effort }),
      });
      if (!response.ok) throw new Error(await researchApiError(response, "模型配置保存失败"));
      const current = parse(await response.json());
      setSettings(current);
      setModel(current.model);
      setEffort(current.reasoning_effort);
      dirty.current = false;
      setFeedback({ kind: "success", text: `已保存，新开始的研究将使用 ${current.model}，推理强度为 ${effortLabel(current.reasoning_effort)}。` });
    } catch (error) {
      setFeedback({ kind: "error", text: error instanceof Error ? error.message : "模型配置保存失败" });
    } finally { setBusy(false); }
  }

  return <section className="panel research-model-settings" aria-label="研究模型">
    <div className="research-panel-heading"><h2><Cpu size={18} />研究模型</h2>
      <button type="button" className="secondary-button" disabled={busy} onClick={() => void load()}>刷新模型配置</button>
    </div>
    <p className="research-note">统一设置研究 Agent 使用的默认模型和推理强度，保存后从下一次开始执行的研究生效。正在运行的研究继续使用原设置。</p>
    <form className="research-launcher-form" onSubmit={(event) => void save(event)}>
      <label>模型名称
        <select aria-label="模型名称" value={model} disabled={busy || !settings} required onChange={(event) => {
          const next = settings?.model_options.find((option) => option.model === event.target.value);
          if (!next) return;
          dirty.current = true;
          setModel(next.model);
          if (!next.reasoning_efforts.includes(effort)) setEffort(next.default_reasoning_effort);
          setFeedback(null);
        }}>
          {!settings ? <option value="">正在读取模型</option> : null}
          {settings?.model_options.map((option) => <option key={option.model} value={option.model}>{option.model}</option>)}
        </select>
      </label>
      <label>推理强度
        <select aria-label="推理强度" value={effort} disabled={busy || !settings} required onChange={(event) => {
          dirty.current = true; setEffort(event.target.value); setFeedback(null);
        }}>
          {!settings ? <option value="">正在读取推理强度</option> : null}
          {settings?.model_options.find((option) => option.model === model)?.reasoning_efforts.map((value) =>
            <option key={value} value={value}>{effortLabel(value)}</option>)}
        </select>
      </label>
      <p className="research-form-note">选项来自本机 Codex 模型列表；推理强度随模型调整。列表不可读取时保留当前设置。</p>
      {settings ? <p className="research-form-note">当前模型：{settings.model} · 推理强度：{effortLabel(settings.reasoning_effort)}</p> : null}
      <div><button type="submit" disabled={busy || !settings || !model || !effort || (model === settings.model && effort === settings.reasoning_effort)}>{busy ? "正在处理" : "保存模型配置"}</button></div>
    </form>
    {feedback ? <p className={`research-control-feedback ${feedback.kind}`} role={feedback.kind === "error" ? "alert" : "status"}>{feedback.text}</p> : null}
  </section>;
}
