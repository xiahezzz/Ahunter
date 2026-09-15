from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from advisor.research.reviews import TeamReview
from advisor.research.language import outcome_label, status_label


@dataclass(frozen=True)
class TeamReviewPublication:
    root: Path
    json_path: Path
    markdown_path: Path


class TeamReviewReporter:
    def __init__(self, reports_root: Path) -> None:
        self.reports_root = reports_root.resolve()

    def publish(self, review: TeamReview) -> TeamReviewPublication:
        report_date = review.review_as_of.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        root = self.reports_root / report_date / "reviews" / "teams" / str(review.team)
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise ValueError(f"Team review path is not a directory: {root}")
        root.mkdir(parents=True, exist_ok=True)
        stem = review.subject_code
        json_path = root / f"{stem}.json"
        markdown_path = root / f"{stem}.md"
        payload = {
            "review_id": review.review_id,
            "conclusion_hash": review.conclusion_hash,
            "team": str(review.team),
            "subject_code": review.subject_code,
            "morning_as_of": review.morning_as_of.isoformat(),
            "review_as_of": review.review_as_of.isoformat(),
            "status": review.status.value,
            "outcome": review.outcome,
            "observed_change": review.observed_change,
            "message": review.message,
        }
        markdown = _markdown(payload)
        _write_new(json_path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        _write_new(markdown_path, markdown)
        return TeamReviewPublication(root, json_path, markdown_path)


def _markdown(payload: dict) -> str:
    lines = [
        "# 团队晚间复盘",
        "",
        f"- 研究团队：`{payload['team']}`",
        f"- 研究标的：`{payload['subject_code']}`",
        f"- 早间结论时间：`{payload['morning_as_of']}`",
        f"- 复盘时间：`{payload['review_as_of']}`",
        f"- 状态：`{status_label(payload['status'])}`",
        f"- 结论存档编号：`{payload['conclusion_hash']}`",
        "",
    ]
    if payload["status"] == "passed":
        lines.extend([
            f"- 复盘结果：`{outcome_label(payload['outcome'])}`",
            f"- 实际涨跌：`{float(payload['observed_change']):.2%}`",
        ])
    else:
        lines.append("- 阻断原因：缺少可靠的复盘数据")
    return "\n".join(lines) + "\n"


def _write_new(path: Path, content: str) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(content)


__all__ = ["TeamReviewPublication", "TeamReviewReporter"]
