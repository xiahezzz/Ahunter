import { FormEvent, useCallback, useEffect, useState } from "react";
import { RefreshCw, Users } from "lucide-react";

type Agent = {
  agent_id: string;
  agent_ref: string;
  scope: ResearchScope;
  title: string;
  summary: string;
};

type ResearchScope = "market" | "security";

type TeamVersion = {
  team_ref: string;
  scope: ResearchScope;
  title: string;
  agents: string[];
};

type Team = {
  team_id: string;
  latest: TeamVersion;
  history: TeamVersion[];
  daily_enabled_ref: string | null;
};

type PublishedTeam = { created: boolean; team: TeamVersion };
type Feedback = { kind: "success" | "error"; text: string };
export type TeamRevisionRequest = { teamRef: string; nonce: number };
type TeamsPanelProps = {
  revisionRequest?: TeamRevisionRequest | null;
  onRevisionRequestConsumed?: () => void;
  refreshToken?: number;
};

const TEAM_ID = /^[a-z][a-z0-9_]{1,63}$/;
const CHINESE = /[\u3400-\u9fff]/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isAgent(value: unknown): value is Agent {
  return isRecord(value) &&
    typeof value.agent_id === "string" && TEAM_ID.test(value.agent_id) &&
    typeof value.agent_ref === "string" && typeof value.title === "string" &&
    typeof value.summary === "string" && (value.scope === "market" || value.scope === "security");
}

function isTeamVersion(value: unknown): value is TeamVersion {
  return isRecord(value) &&
    typeof value.team_ref === "string" && typeof value.title === "string" &&
    (value.scope === "market" || value.scope === "security") &&
    Array.isArray(value.agents) && value.agents.every((agent) => typeof agent === "string");
}

function isTeam(value: unknown): value is Team {
  return isRecord(value) &&
    typeof value.team_id === "string" && TEAM_ID.test(value.team_id) &&
    isTeamVersion(value.latest) && Array.isArray(value.history) && value.history.every(isTeamVersion) &&
    (value.daily_enabled_ref === null || typeof value.daily_enabled_ref === "string");
}

function parseAgents(value: unknown): Agent[] {
  if (!isRecord(value) || !Array.isArray(value.agents) || !value.agents.every(isAgent)) {
    throw new Error("研究 Agent 目录格式无效");
  }
  return value.agents;
}

function parseTeams(value: unknown): Team[] {
  if (!isRecord(value) || !Array.isArray(value.teams) || !value.teams.every(isTeam)) {
    throw new Error("研究团队目录格式无效");
  }
  return value.teams;
}

function parsePublished(value: unknown): PublishedTeam {
  if (!isRecord(value) || typeof value.created !== "boolean" || !isTeamVersion(value.team)) {
    throw new Error("发布结果格式无效");
  }
  return { created: value.created, team: value.team };
}

async function responseError(response: Response, fallback: string): Promise<Error> {
  try {
    const payload = await response.json();
    if (isRecord(payload) && typeof payload.detail === "string" && payload.detail.trim()) {
      return new Error(payload.detail.slice(0, 120));
    }
  } catch {
    // The bounded fallback below is intentionally shown for malformed failures.
  }
  return new Error(fallback);
}

async function fetchJson(path: string, init?: RequestInit): Promise<unknown> {
  const response = await fetch(path, init);
  if (!response.ok) throw await responseError(response, "研究团队操作失败");
  return response.json();
}

function userFacingError(caught: unknown, fallback: string): string {
  if (caught instanceof Error && CHINESE.test(caught.message)) return caught.message.slice(0, 120);
  return fallback;
}

function agentIds(version: TeamVersion): string[] {
  return version.agents.map((reference) => reference.split("@", 1)[0]);
}

export default function TeamsPanel({ revisionRequest = null, onRevisionRequestConsumed, refreshToken = 0 }: TeamsPanelProps) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [teams, setTeams] = useState<Team[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [teamId, setTeamId] = useState("");
  const [lockedTeamId, setLockedTeamId] = useState<string | null>(null);
  const [scope, setScope] = useState<ResearchScope>("security");
  const [title, setTitle] = useState("");
  const [selectedAgentIds, setSelectedAgentIds] = useState<string[]>([]);
  const [expandedHistory, setExpandedHistory] = useState<Set<string>>(new Set());

  const refreshDirectory = useCallback(async () => {
    const [agentPayload, teamPayload] = await Promise.all([
      fetchJson("/api/research/agents"),
      fetchJson("/api/research/teams"),
    ]);
    setAgents(parseAgents(agentPayload));
    setTeams(parseTeams(teamPayload));
    setLoadError(null);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      await refreshDirectory();
    } catch (caught) {
      setLoadError(userFacingError(caught, "暂时无法读取研究团队，请稍后重试"));
    } finally {
      setLoading(false);
    }
  }, [refreshDirectory]);

  useEffect(() => {
    void load();
  }, [load, refreshToken]);

  function resetDraft() {
    setTeamId("");
    setLockedTeamId(null);
    setScope("security");
    setTitle("");
    setSelectedAgentIds([]);
  }

  function toggleAgent(agentId: string) {
    setSelectedAgentIds((current) => current.includes(agentId)
      ? current.filter((item) => item !== agentId)
      : [...current, agentId]);
  }

  function startRevision(team: Team, version: TeamVersion) {
    setFeedback(null);
    setTeamId(team.team_id);
    setLockedTeamId(team.team_id);
    setScope(version.scope);
    setTitle(version.title);
    setSelectedAgentIds(agentIds(version));
  }

  useEffect(() => {
    if (!revisionRequest || teams.length === 0) return;
    const team = teams.find((item) => item.team_id === revisionRequest.teamRef.split("@", 1)[0]);
    const version = team?.history.find((item) => item.team_ref === revisionRequest.teamRef);
    if (team && version) startRevision(team, version);
    onRevisionRequestConsumed?.();
  }, [onRevisionRequestConsumed, revisionRequest, teams]);

  async function publish(event: FormEvent) {
    event.preventDefault();
    setFeedback(null);
    if (!TEAM_ID.test(teamId)) {
      setFeedback({ kind: "error", text: "团队 ID 必须为小写下划线名称" });
      return;
    }
    if (!title.trim() || !CHINESE.test(title)) {
      setFeedback({ kind: "error", text: "请填写中文名称" });
      return;
    }
    if (selectedAgentIds.length === 0) {
      setFeedback({ kind: "error", text: "请至少选择一个研究 Agent" });
      return;
    }
    setBusy("publish");
    try {
      const payload = parsePublished(await fetchJson("/api/research/teams", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ team_id: teamId, scope, title: title.trim(), agent_ids: selectedAgentIds }),
      }));
      resetDraft();
      const message = payload.created ? `已发布 ${payload.team.team_ref}` : `已确认已发布 ${payload.team.team_ref}`;
      try {
        await refreshDirectory();
        setFeedback({ kind: "success", text: message });
      } catch {
        setFeedback({ kind: "success", text: `${message}，但刷新失败，请刷新确认` });
      }
    } catch (caught) {
      setFeedback({ kind: "error", text: userFacingError(caught, "研究团队发布失败") });
    } finally {
      setBusy(null);
    }
  }

  async function changeDaily(team: Team, version: TeamVersion) {
    const disabling = team.daily_enabled_ref === version.team_ref;
    const replacing = !disabling && team.daily_enabled_ref !== null;
    setFeedback(null);
    setBusy(version.team_ref);
    try {
      await fetchJson(`/api/research/daily-teams/${encodeURIComponent(version.team_ref)}`, {
        method: disabling ? "DELETE" : "PUT",
      });
      const message = disabling
        ? "已取消每日启用"
        : replacing ? "已启用每日 Team，已替换同一团队的旧版本" : "已启用每日 Team";
      try {
        await refreshDirectory();
        setFeedback({ kind: "success", text: message });
      } catch {
        setFeedback({ kind: "success", text: `${message}，但刷新失败，请刷新确认` });
      }
    } catch (caught) {
      setFeedback({ kind: "error", text: userFacingError(caught, "每日 Team 操作失败") });
    } finally {
      setBusy(null);
    }
  }

  function renderVersion(team: Team, version: TeamVersion, historical = false) {
    const active = team.daily_enabled_ref === version.team_ref;
    return <div className={historical ? "team-version historical" : "team-version"} key={version.team_ref}>
      <div>
        <strong>{historical ? version.team_ref : version.title}</strong>
        {!historical && <small>{version.team_ref} · {scopeLabel(version.scope)}</small>}
        {historical && <small>{version.title}</small>}
        <ul className="team-agent-refs">{version.agents.map((agent) => <li key={agent}>{agent}</li>)}</ul>
      </div>
      <div className="team-version-actions">
        <button type="button" onClick={() => startRevision(team, version)} disabled={busy !== null}>基于此版本新建</button>
        <button type="button" onClick={() => void changeDaily(team, version)} disabled={busy !== null}>
          {active ? "取消每日启用" : "每日启用"}
        </button>
      </div>
    </div>;
  }

  const noDailyTeams = teams.every((team) => team.daily_enabled_ref === null);
  const compatibleAgents = agents.filter((agent) => agent.scope === scope);

  return <section className="panel research-teams-panel" aria-label="研究团队">
    <div className="research-teams-heading">
      <h2><Users size={18} />研究团队</h2>
      <button type="button" className="icon-button" aria-label="刷新研究团队" title="刷新研究团队" onClick={() => void load()} disabled={loading || busy !== null}>
        <RefreshCw className={loading ? "spin" : undefined} size={17} />
      </button>
    </div>
    {loading && teams.length === 0 ? <p className="unavailable-message">正在读取研究团队…</p> : null}
    {loadError && teams.length === 0 ? <div className="research-teams-error" role="alert"><span>研究团队状态读取失败</span><small>{loadError}</small><button type="button" onClick={() => void load()} disabled={loading}>重试读取</button></div> : null}
    {!loading || teams.length > 0 ? <>
      {noDailyTeams ? <p className="team-empty-state">当前未启用每日 Team</p> : null}
      {feedback ? <p className={`team-feedback ${feedback.kind}`} role={feedback.kind === "error" ? "alert" : "status"}>{feedback.text}</p> : null}
      <div className="team-layout">
        <form className="team-form" onSubmit={(event) => void publish(event)}>
          <h3>{lockedTeamId ? "修订研究团队" : "新建研究团队"}</h3>
          <label>团队 ID<input aria-label="团队 ID" value={teamId} onChange={(event) => setTeamId(event.target.value)} disabled={lockedTeamId !== null || busy !== null} placeholder="value_style" /></label>
          {lockedTeamId ? <p className="team-form-note">修订会保留 Team ID，并自动生成新版本。</p> : null}
          <fieldset disabled={lockedTeamId !== null || busy !== null}><legend>研究范围</legend>
            <label><input aria-label="Security 研究范围" type="radio" name="team-scope" checked={scope === "security"} onChange={() => { setScope("security"); setSelectedAgentIds([]); }} />单只证券</label>
            <label><input aria-label="Market 研究范围" type="radio" name="team-scope" checked={scope === "market"} onChange={() => { setScope("market"); setSelectedAgentIds([]); }} />沪深 A 股整体</label>
          </fieldset>
          <label>中文名称<input aria-label="中文名称" value={title} onChange={(event) => setTitle(event.target.value)} disabled={busy !== null} placeholder="例如：价值风格" /></label>
          <fieldset disabled={busy !== null}><legend>选择研究 Agent</legend>{compatibleAgents.length === 0 ? <p className="team-form-note">当前范围暂无可选 Agent。</p> : compatibleAgents.map((agent) => <label className="team-agent-option" key={agent.agent_id}>
            <input aria-label={`选择 ${agent.title}`} type="checkbox" checked={selectedAgentIds.includes(agent.agent_id)} onChange={() => toggleAgent(agent.agent_id)} />
            <span><strong>{agent.title}</strong><small>{agent.summary}</small></span>
          </label>)}</fieldset>
          <div className="team-form-actions"><button type="submit" disabled={busy !== null}>{busy === "publish" ? "正在发布" : "发布 Team"}</button>{lockedTeamId ? <button type="button" className="secondary-button" onClick={resetDraft} disabled={busy !== null}>取消修订</button> : null}</div>
        </form>
        <div className="team-cards" aria-live="polite">
          {teams.length === 0 ? <p className="empty">暂无已发布 Team</p> : teams.map((team) => {
            const history = team.history.filter((version) => version.team_ref !== team.latest.team_ref);
            const expanded = expandedHistory.has(team.team_id);
            return <article className="team-card" key={team.team_id} aria-label={`${team.latest.title} ${team.latest.team_ref}`}>
              {renderVersion(team, team.latest)}
              {history.length > 0 ? <>
                <button type="button" className="history-button" onClick={() => setExpandedHistory((current) => {
                  const next = new Set(current);
                  if (next.has(team.team_id)) next.delete(team.team_id); else next.add(team.team_id);
                  return next;
                })}>{expanded ? "收起历史版本" : "查看历史版本"}</button>
                {expanded ? <div className="team-history">{history.map((version) => renderVersion(team, version, true))}</div> : null}
              </> : null}
            </article>;
          })}
        </div>
      </div>
    </> : null}
  </section>;
}

function scopeLabel(scope: ResearchScope): string {
  return scope === "market" ? "全市场" : "单只证券";
}
