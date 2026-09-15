from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from advisor.research.artifacts import ArtifactStore
from advisor.research.contracts import TeamConclusion, VersionRef
from advisor.research.decision.contracts import MarketTeamReport
from advisor.research.language import (
    agent_label,
    conviction_label,
    quality_label,
    stage_label,
    stance_label,
    status_label,
    validate_decision_output_language,
    validate_research_finding_language,
)
from advisor.research.market_safety import contains_unsafe_market_output, snapshot_security_names
from advisor.research.state_machine import ResearchCycleResult, TeamRunResult


_CYCLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class CyclePublication:
    root: Path
    cycle_json: Path
    index: Path
    complete_marker: Path


class CycleReporter:
    def __init__(self, reports_root: Path, *, artifact_store: ArtifactStore | None = None) -> None:
        self.reports_root = reports_root.resolve()
        self.artifact_store = artifact_store

    def publish(self, cycle: ResearchCycleResult) -> CyclePublication:
        _validate_cycle_id(cycle.cycle_id)
        self._validate_market_output_boundary(cycle)
        report_date = cycle.boundary.as_of.astimezone(_SHANGHAI).date().isoformat()
        root = self.reports_root / report_date / cycle.cycle_id
        if root.exists() or root.is_symlink():
            if root.is_symlink():
                raise ValueError("cycle report path is a symlink")
            raise FileExistsError(f"cycle report already exists: {cycle.cycle_id}")
        root.parent.mkdir(parents=True, exist_ok=True)
        # Never expose a partially written final report directory.  A crash
        # before the final same-filesystem rename leaves only a uniquely named
        # staging directory, which is audit evidence but does not prevent a
        # future owner from publishing the immutable Cycle again.
        staging = root.parent / f".{cycle.cycle_id}.staging-{uuid4().hex}"
        staging.mkdir()
        try:
            cycle_payload = self._cycle_payload(cycle, report_date)
            cycle_path = staging / "cycle.json"
            index_path = staging / "index.md"
            self._write_new(cycle_path, _json(cycle_payload))
            self._write_new(index_path, self._index_markdown(cycle, report_date))
            for team_ref, team_result in sorted(cycle.team_results.items()):
                self._publish_team(staging, cycle, team_ref, team_result)
            marker_payload = {
                "cycle_id": cycle.cycle_id,
                "report_date": report_date,
                "files": {
                    "cycle.json": _sha256(cycle_path.read_bytes()),
                    "index.md": _sha256(index_path.read_bytes()),
                },
            }
            for path in sorted(staging.glob("teams/*/*")):
                if path.is_file():
                    marker_payload["files"][str(path.relative_to(staging))] = _sha256(path.read_bytes())
            marker = staging / "complete.json"
            self._write_new(marker, _json(marker_payload))
            if root.exists() or root.is_symlink():
                if root.is_symlink():
                    raise ValueError("cycle report path is a symlink")
                raise FileExistsError(f"cycle report already exists: {cycle.cycle_id}")
            try:
                os.replace(staging, root)
            except OSError as error:
                if root.exists() or root.is_symlink():
                    raise FileExistsError(f"cycle report already exists: {cycle.cycle_id}") from error
                raise
            return CyclePublication(root, root / "cycle.json", root / "index.md", root / "complete.json")
        except Exception:
            # Keep an incomplete staging directory as an audit signal.  It is
            # deliberately distinct from the final path so restart recovery
            # can safely make a new immutable publication attempt.
            raise

    def publish_or_recover(self, cycle: ResearchCycleResult) -> CyclePublication:
        """Publish once, or reuse only a byte-for-byte matching completion.

        A process can crash after the immutable directory and artifact files
        are written but before its durable Request is completed.  The next
        owner must be able to finish that Request without overwriting any
        report.  Incomplete or mismatched directories remain hard failures
        and audit evidence; only a verified complete publication is reusable.
        """
        try:
            return self.publish(cycle)
        except FileExistsError:
            return self.recover(cycle)

    def recover(self, cycle: ResearchCycleResult) -> CyclePublication:
        _validate_cycle_id(cycle.cycle_id)
        self._validate_market_output_boundary(cycle)
        report_date = cycle.boundary.as_of.astimezone(_SHANGHAI).date().isoformat()
        root = self.reports_root / report_date / cycle.cycle_id
        if root.is_symlink() or not root.is_dir():
            raise FileExistsError(f"cycle report is not recoverable: {cycle.cycle_id}")
        expected_files = self._expected_files(cycle, report_date)
        expected_marker = {
            "cycle_id": cycle.cycle_id,
            "report_date": report_date,
            "files": {
                relative: _sha256(content.encode("utf-8"))
                for relative, content in sorted(expected_files.items())
            },
        }
        marker = root / "complete.json"
        try:
            if marker.is_symlink() or not marker.is_file():
                raise ValueError("completion marker is unavailable")
            actual_paths: set[str] = set()
            for path in root.rglob("*"):
                if path.is_symlink():
                    raise ValueError("publication contains a symlink")
                if path.is_file():
                    actual_paths.add(str(path.relative_to(root)))
            if actual_paths != {*expected_files, "complete.json"}:
                raise ValueError("publication file set changed")
            for relative, content in expected_files.items():
                path = root / relative
                if path.is_symlink() or not path.is_file() or path.read_bytes() != content.encode("utf-8"):
                    raise ValueError(f"publication content changed: {relative}")
            marker_payload = json.loads(marker.read_text(encoding="utf-8"))
            if marker_payload != expected_marker:
                raise ValueError("completion marker does not match the cycle")
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            raise FileExistsError(f"cycle report is not recoverable: {cycle.cycle_id}") from error
        return CyclePublication(root, root / "cycle.json", root / "index.md", marker)

    def _expected_files(self, cycle: ResearchCycleResult, report_date: str) -> dict[str, str]:
        files = {
            "cycle.json": _json(self._cycle_payload(cycle, report_date)),
            "index.md": self._index_markdown(cycle, report_date),
        }
        for team_ref, team_result in sorted(cycle.team_results.items()):
            prefix = f"teams/{team_ref}/"
            if team_result.status.value != "passed":
                files[prefix + "status.json"] = _json({
                    "cycle_id": cycle.cycle_id,
                    "team": team_ref,
                    "status": team_result.status.value,
                    "message": team_result.message or "该研究团队已被阻断",
                    "agent_errors": team_result.agent_errors,
                    "snapshot_unavailable": {} if cycle.snapshot is None else getattr(cycle.snapshot, "unavailable", {}),
                })
                continue
            conclusion, report_markdown = self._team_content(cycle, team_ref, team_result.conclusion)
            conclusion_json = _json(conclusion.model_dump(mode="json"))
            files[prefix + "conclusion.json"] = conclusion_json
            files[prefix + "report.md"] = report_markdown
            if self.artifact_store is not None:
                files[prefix + "artifacts.json"] = _json({
                    "conclusion_hash": _sha256(conclusion_json.encode("utf-8")),
                    "report_hash": _sha256(report_markdown.encode("utf-8")),
                })
        return files

    @staticmethod
    def _validate_market_output_boundary(cycle: ResearchCycleResult) -> None:
        """Reject direct Market report publication that bypassed the pipeline.

        The fixed Market Pipeline already validates its output, but the
        reporter is independently callable.  A final output boundary must
        therefore reapply the safety predicate using only identities sealed in
        the Cycle Snapshot; an unsealed or identity-free Market report fails
        closed rather than making named-security wording invisible to the
        guard.
        """
        market_reports = [
            report
            for result in cycle.team_results.values()
            for report in (getattr(result.conclusion, "report", None),)
            if isinstance(report, MarketTeamReport)
        ]
        if not market_reports:
            return
        if cycle.snapshot is None:
            raise ValueError("Market Report lacks a sealed Snapshot")
        security_names = snapshot_security_names(cycle.snapshot)
        if not security_names:
            raise ValueError("Market Report lacks a sealed security identity mapping")
        for report in market_reports:
            if report.subject != cycle.subject or report.boundary != cycle.boundary:
                raise ValueError("Market Report identity does not match its Cycle")
            if contains_unsafe_market_output(report.model_dump(mode="json"), security_names=security_names):
                raise ValueError("Market Report contains a per-security recommendation")

    def _cycle_payload(self, cycle: ResearchCycleResult, report_date: str) -> dict[str, Any]:
        snapshot = cycle.snapshot
        teams = []
        for team_ref, team_result in sorted(cycle.team_results.items()):
            item: dict[str, Any] = {"team": team_ref, "status": team_result.status.value}
            if team_result.status.value == "passed":
                conclusion, report = self._team_content(cycle, team_ref, team_result.conclusion)
                item["paths"] = {
                    "conclusion": f"teams/{team_ref}/conclusion.json",
                    "report": f"teams/{team_ref}/report.md",
                }
                item["artifact_refs"] = {
                    "conclusion": _sha256(_json(conclusion.model_dump(mode="json")).encode("utf-8")),
                    "report": _sha256(report.encode("utf-8")),
                }
            else:
                item["paths"] = {"status": f"teams/{team_ref}/status.json"}
            teams.append(item)
        return {
            "cycle_id": cycle.cycle_id,
            "report_date": report_date,
            "subject": cycle.subject.model_dump(mode="json"),
            "as_of": cycle.boundary.as_of.isoformat(),
            "status": cycle.status.value,
            "cycle_fingerprint": cycle.fingerprint,
            "snapshot": None
            if snapshot is None
            else {
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_hash": snapshot.snapshot_hash,
                "unavailable": getattr(snapshot, "unavailable", {}),
            },
            "agent_errors": cycle.agent_errors,
            "teams": teams,
        }

    def _index_markdown(self, cycle: ResearchCycleResult, report_date: str) -> str:
        lines = [
            "# 研究任务索引",
            "",
            f"- 任务编号：`{cycle.cycle_id}`",
            f"- 研究标的：`{cycle.subject.code or '沪深 A 股整体'}`",
            f"- 信息截止时间：`{cycle.boundary.as_of.isoformat()}`",
            f"- 整体状态：`{status_label(cycle.status.value)}`",
            "",
            "| 研究团队 | 状态 | 文件 |",
            "|---|---|---|",
        ]
        for team_ref, team_result in sorted(cycle.team_results.items()):
            if team_result.status.value == "passed":
                files = f"[阅读报告](teams/{team_ref}/report.md)、[结构化数据](teams/{team_ref}/conclusion.json)"
            else:
                files = f"[查看状态](teams/{team_ref}/status.json)"
            lines.append(f"| `{team_ref}` | `{status_label(team_result.status.value)}` | {files} |")
        return "\n".join(lines) + "\n"

    def _publish_team(self, root: Path, cycle: ResearchCycleResult, team_ref: str, team_result: TeamRunResult) -> None:
        VersionRef.parse(team_ref)
        team_dir = root / "teams" / team_ref
        team_dir.mkdir(parents=True, exist_ok=False)
        if team_result.status.value != "passed":
            status = {
                "cycle_id": cycle.cycle_id,
                "team": team_ref,
                "status": team_result.status.value,
                "message": team_result.message or "该研究团队已被阻断",
                "agent_errors": team_result.agent_errors,
                "snapshot_unavailable": {} if cycle.snapshot is None else getattr(cycle.snapshot, "unavailable", {}),
            }
            self._write_new(team_dir / "status.json", _json(status))
            return
        pipeline = team_result.conclusion
        conclusion, report_markdown = self._team_content(cycle, team_ref, pipeline)
        conclusion_path = team_dir / "conclusion.json"
        report_path = team_dir / "report.md"
        conclusion_json = _json(conclusion.model_dump(mode="json"))
        self._write_new(conclusion_path, conclusion_json)
        self._write_new(report_path, report_markdown)
        if self.artifact_store is not None:
            conclusion_ref = self.artifact_store.put_text(conclusion_json, media_type="application/json")
            report_ref = self.artifact_store.put_text(report_markdown)
            # Keep a sidecar with hashes so cycle.json remains navigation-only.
            self._write_new(
                team_dir / "artifacts.json",
                _json({"conclusion_hash": conclusion_ref.content_hash, "report_hash": report_ref.content_hash}),
            )


    def _team_content(self, cycle: ResearchCycleResult, team_ref: str, pipeline: Any) -> tuple[Any, str]:
        market_report = getattr(pipeline, "report", None)
        if isinstance(market_report, MarketTeamReport):
            return market_report, self._market_team_markdown(cycle, team_ref, market_report)
        conclusion = pipeline.conclusion if hasattr(pipeline, "conclusion") else pipeline
        if not isinstance(conclusion, TeamConclusion):
            conclusion = TeamConclusion.model_validate(conclusion)
        return conclusion, self._team_markdown(cycle, team_ref, conclusion, pipeline)

    def _market_team_markdown(self, cycle: ResearchCycleResult, team_ref: str, report: MarketTeamReport) -> str:
        lines = [
            f"# 全市场团队研究报告：`{team_ref}`",
            "",
            "- 研究范围：`沪深 A 股整体`",
            f"- 信息截止时间：`{cycle.boundary.as_of.isoformat()}`",
            f"- 报告状态：`{'完整' if report.status == 'passed' else '部分可用'}`",
            f"- 观察周期：`{report.time_horizon}`",
            "",
        ]
        for insight in report.insights:
            title = {
                "breadth_sentiment": "市场广度与情绪",
                "sector_rotation": "一级行业轮动",
                "macro_policy": "宏观政策与市场信息",
            }[insight.insight_id]
            lines.extend([f"## {title}", ""])
            if insight.status == "blocked":
                lines.extend([f"- 当前不可用：`{insight.reason_code}`", ""])
                continue
            lines.extend([str(insight.summary), "", "### 方法摘要", ""])
            for key, value in (insight.method or {}).items():
                readable = "、".join(str(item) for item in value) if isinstance(value, list) else str(value)
                lines.append(f"- {key}：{readable}")
            lines.extend(["", "### 主要证据", ""])
            for evidence in insight.evidence:
                excerpt = f"；摘要：{evidence.excerpt}" if evidence.excerpt else ""
                lines.append(f"- 证据编号：`{evidence.evidence_id}`；来源：`{evidence.source}`{excerpt}")
            lines.append("")
        lines.extend(["## 共同风险", ""])
        lines.extend(f"- {item}" for item in report.risks)
        lines.extend(["", "## 失效条件", ""])
        lines.extend(f"- {item}" for item in report.invalidation_conditions)
        lines.extend(["", "## 证据质量", "", f"- 当前状态：`{quality_label(report.evidence_quality.status)}`"])
        if report.evidence_quality.limitations:
            lines.extend(["- 已知限制：", *[f"  - {item}" for item in report.evidence_quality.limitations]])
        lines.append("")
        return "\n".join(lines)

    def _team_markdown(self, cycle: ResearchCycleResult, team_ref: str, conclusion: TeamConclusion, pipeline: Any) -> str:
        validate_decision_output_language(conclusion)
        findings = cycle.team_findings.get(team_ref, {})
        for agent_result in findings.values():
            finding = agent_result if hasattr(agent_result, "summary") else getattr(agent_result, "finding", agent_result)
            validate_research_finding_language(finding)
        stages = getattr(pipeline, "stages", {})
        for stage, result in stages.items():
            if stage in {"finding_quality", "publication_quality"}:
                continue
            validate_decision_output_language(getattr(result, "output", result))
        lines = [
            f"# 团队研究报告：`{team_ref}`",
            "",
            f"- 研究标的：`{cycle.subject.code}`",
            f"- 信息截止时间：`{cycle.boundary.as_of.isoformat()}`",
            "",
            "## 专家观点",
            "",
        ]
        for agent_ref, agent_result in sorted(findings.items()):
            finding = agent_result if hasattr(agent_result, "summary") else getattr(agent_result, "finding", agent_result)
            lines.extend([f"### {agent_label(agent_ref)}（`{agent_ref}`）", "", str(getattr(finding, "summary", "")), ""])
        lines.extend(["## 决策过程", ""])
        for stage, result in stages.items():
            if stage in {"finding_quality", "publication_quality", "portfolio_manager"}:
                continue
            output = getattr(result, "output", result)
            summary = getattr(output, "summary", None) or getattr(output, "thesis", "")
            lines.extend([f"### {stage_label(stage)}", "", str(summary), ""])
        lines.extend(
            [
                "## 最终结论",
                "",
                f"- 判断：`{stance_label(conclusion.stance)}`",
                f"- 把握程度：`{conviction_label(conclusion.conviction)}`",
                f"- 核心理由：{conclusion.thesis}",
                f"- 观察周期：{conclusion.time_horizon}",
                *([] if conclusion.position_limit is None else [f"- 仓位约束：{conclusion.position_limit}"]),
                *(
                    []
                    if conclusion.price_range is None
                    else [
                        f"- 参考价格区间：{conclusion.price_range['low']:g} 至 {conclusion.price_range['high']:g}"
                    ]
                ),
                "",
                "### 主要证据",
                "",
            ]
        )
        for evidence in conclusion.evidence:
            description = f"；摘要：{evidence.excerpt}" if evidence.excerpt else ""
            lines.append(f"- 证据编号：`{evidence.evidence_id}`；来源：`{evidence.source}`{description}")
        lines.extend(["", "### 主要风险", ""])
        lines.extend(f"- {risk}" for risk in conclusion.key_risks)
        lines.extend(["", "### 结论失效条件", ""])
        lines.extend(f"- {item}" for item in conclusion.invalidation_conditions)
        lines.extend(["", "### 证据质量", "", f"- 当前状态：`{quality_label(conclusion.evidence_quality.status)}`"])
        if conclusion.evidence_quality.limitations:
            lines.extend(["- 已知限制：", *[f"  - {item}" for item in conclusion.evidence_quality.limitations]])
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _write_new(path: Path, content: str) -> None:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()


def _validate_cycle_id(cycle_id: str) -> None:
    if not isinstance(cycle_id, str) or not _CYCLE_ID_RE.fullmatch(cycle_id):
        raise ValueError("unsafe cycle_id")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
