import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { Play } from "lucide-react";
import {
  ResearchRequest,
  ResearchTeamGroup,
  newSubmissionIdentity,
  parseResearchRequestResponse,
  parseResearchTeamDirectory,
  researchApiError,
  validResearchSecurityCode,
} from "./contracts";

type Props = { refreshToken?: number; onSubmitted?: (request: ResearchRequest) => void };

export default function ResearchLauncher({ refreshToken = 0, onSubmitted }: Props) {
  const [teams, setTeams] = useState<ResearchTeamGroup[]>([]);
  const [teamRef, setTeamRef] = useState("");
  const [code, setCode] = useState("");
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<{ kind: "success" | "error"; text: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await fetch("/api/research/teams");
      if (!response.ok) throw new Error(await researchApiError(response, "暂时无法读取已发布 Team"));
      const next = parseResearchTeamDirectory(await response.json());
      setTeams(next);
      setTeamRef((current) => next.some((team) => team.history.some((version) => version.team_ref === current)) ? current : (next[0]?.latest.team_ref ?? ""));
    } catch (error) {
      setMessage({ kind: "error", text: error instanceof Error ? error.message : "暂时无法读取已发布 Team" });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load, refreshToken]);

  const versions = useMemo(() => teams.flatMap((team) => team.history), [teams]);
  const selected = versions.find((item) => item.team_ref === teamRef) ?? null;

  async function submit(event: FormEvent) {
    event.preventDefault();
    setMessage(null);
    if (!selected) {
      setMessage({ kind: "error", text: "请选择一个已发布 Team 版本" });
      return;
    }
    const trimmed = code.trim();
    if (selected.scope === "security" && !validResearchSecurityCode(trimmed)) {
      setMessage({ kind: "error", text: "请输入合法的六位沪深 A 股代码" });
      return;
    }
    setPending(true);
    try {
      const response = await fetch("/api/research/requests", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          team_ref: selected.team_ref,
          scope: selected.scope,
          code: selected.scope === "security" ? trimmed : null,
          submission_identity: newSubmissionIdentity("web"),
        }),
      });
      if (!response.ok) throw new Error(await researchApiError(response, "研究请求提交失败"));
      const submitted = parseResearchRequestResponse(await response.json());
      setMessage({ kind: "success", text: `已提交研究请求 ${submitted.request_id}，正在等待 Research Service。` });
      onSubmitted?.(submitted);
    } catch (error) {
      setMessage({ kind: "error", text: error instanceof Error ? error.message : "研究请求提交失败" });
    } finally {
      setPending(false);
    }
  }

  return <section className="panel research-launcher" aria-label="立即研究">
    <div className="research-panel-heading"><h2><Play size={18} />立即研究</h2><button type="button" className="secondary-button" onClick={() => void load()} disabled={loading || pending}>刷新 Team</button></div>
    <p className="research-note">提交后会立即返回请求编号；页面不会等待研究完成，也不会自动再次提交。</p>
    {message ? <p className={`research-control-feedback ${message.kind}`} role={message.kind === "error" ? "alert" : "status"}>{message.text}</p> : null}
    <form className="research-launcher-form" onSubmit={(event) => void submit(event)}>
      <label>已发布 Team 版本
        <select aria-label="已发布 Team 版本" value={teamRef} onChange={(event) => { setTeamRef(event.target.value); setMessage(null); }} disabled={loading || pending || versions.length === 0}>
          {versions.map((version) => <option key={version.team_ref} value={version.team_ref}>{version.title} · {version.team_ref} · {version.scope === "market" ? "全市场" : "单只证券"}</option>)}
        </select>
      </label>
      {selected ? <p className="research-form-note">范围：{selected.scope === "market" ? "沪深 A 股整体（不提交证券代码）" : "单只证券"}；成员：{selected.agents.join("、")}</p> : null}
      {selected?.scope === "security" ? <label>证券代码<input aria-label="证券代码" value={code} onChange={(event) => setCode(event.target.value.replace(/\s/g, ""))} inputMode="numeric" maxLength={6} placeholder="例如 600519" disabled={pending} /></label> : null}
      <div><button type="submit" disabled={loading || pending || selected === null}>{pending ? "正在提交" : "提交研究请求"}</button></div>
    </form>
  </section>;
}
