import { useState } from "react";
import { RefreshCw, Settings2 } from "lucide-react";
import AgentAccessPanel from "./AgentAccessPanel";
import ExecutionSettingsPanel from "./ExecutionSettingsPanel";
import CurrentResearchQueue from "./CurrentResearchQueue";
import ResearchLauncher from "./ResearchLauncher";
import ResearchReportExplorer from "./ResearchReportExplorer";
import TeamsPanel, { TeamRevisionRequest } from "./TeamsPanel";
import { ResearchRequest } from "./contracts";
import LAgentPanel from "./LAgentPanel";
import ExperimentExplorer from "./ExperimentExplorer";

export default function ResearchConfigurationPage({ onInstructionsDirtyChange }: { onInstructionsDirtyChange?: (dirty: boolean) => void }) {
  const [refreshToken, setRefreshToken] = useState(0);
  const [teamRevision, setTeamRevision] = useState<TeamRevisionRequest | null>(null);
  const [submittedRequests, setSubmittedRequests] = useState<ResearchRequest[]>([]);
  function trackSubmittedRequest(request: ResearchRequest) {
    setSubmittedRequests((current) => [
      ...current.filter((item) => item.request_id !== request.request_id),
      request,
    ]);
    setRefreshToken((value) => value + 1);
  }
  return <div className="research-configuration-page" id="configuration">
    <section className="research-page-heading" aria-label="研究配置">
      <div><p className="eyebrow">版本化目录与显式发布</p><h2><Settings2 size={18} />研究配置</h2><p>研究数据源、研究助手的数据权限和研究团队都必须通过各自的显式发布流程变更。</p></div>
      <button type="button" className="secondary-button" onClick={() => setRefreshToken((value) => value + 1)}><RefreshCw size={16} />刷新研究目录</button>
    </section>
    <ExecutionSettingsPanel refreshToken={refreshToken} />
    <LAgentPanel refreshToken={refreshToken} onSubmitted={trackSubmittedRequest} />
    <div className="research-control-grid">
      <ResearchLauncher refreshToken={refreshToken} onSubmitted={trackSubmittedRequest} />
      <CurrentResearchQueue refreshToken={refreshToken} submittedRequests={submittedRequests} onTerminal={(requestId) => {
        setSubmittedRequests((current) => current.filter((item) => item.request_id !== requestId));
        setRefreshToken((value) => value + 1);
      }} />
    </div>
    <ExperimentExplorer />
    <ResearchReportExplorer refreshToken={refreshToken} onRerun={trackSubmittedRequest} />
    <AgentAccessPanel refreshToken={refreshToken} onInstructionsDirtyChange={onInstructionsDirtyChange} onReviseTeam={(teamRef) => setTeamRevision({ teamRef, nonce: Date.now() })} />
    <TeamsPanel refreshToken={refreshToken} revisionRequest={teamRevision} onRevisionRequestConsumed={() => setTeamRevision(null)} />
  </div>;
}
