from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

from advisor.research.batch import DailyBatchResult
from advisor.research.contracts import VersionRef
from advisor.research.language import status_label


class DailyBriefRenderer:
    def __init__(self, reports_root: Path) -> None:
        self.reports_root = reports_root.resolve()

    def render(self, batch: DailyBatchResult, team_ref: str) -> Path:
        team = str(VersionRef.parse(team_ref))
        report_date = batch.boundary.as_of.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        target_dir = self.reports_root / report_date / batch.batch_id / "teams" / team
        if target_dir.exists() or target_dir.is_symlink():
            raise FileExistsError("Daily Team Brief already exists")
        target_dir.mkdir(parents=True, exist_ok=False)
        lines = [
            "# 每日研究简报",
            "",
            f"- 批次编号：`{batch.batch_id}`",
            f"- 研究团队：`{team}`",
            f"- 信息截止时间：`{batch.boundary.as_of.isoformat()}`",
            "",
            "| 研究标的 | 任务状态 | 报告 |",
            "|---|---|---|",
        ]
        for code, cycle in sorted(batch.cycles.items()):
            team_result = cycle.team_results.get(team)
            status = team_result.status.value if team_result is not None else "blocked"
            if status == "passed":
                link = f"../../../{cycle.cycle_id}/teams/{team}/report.md"
            else:
                link = f"../../../{cycle.cycle_id}/index.md"
            lines.append(f"| `{code}` | `{status_label(status)}` | [查看]({link}) |")
        target = target_dir / "daily-brief.md"
        with target.open("x", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return target
