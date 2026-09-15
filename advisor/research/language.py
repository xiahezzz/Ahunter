from __future__ import annotations

import re
from typing import Any, Iterable


OUTPUT_LANGUAGE_POLICY = "plain-zh-cn@2"
PLAIN_CHINESE_PROMPT = (
    "所有面向读者的文本必须使用简体中文，包括摘要、观点、证据摘要、风险、失效条件、时间范围和仓位说明。"
    "使用短句和日常表达；必须使用专业术语时，第一次出现就用中文解释。"
    "不要写英文段落，不要照搬上游英文原文。"
    "JSON 键名、固定枚举值、Agent、Team、Stage、证据编号和证券代码必须保持契约要求，不要翻译。"
)

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_VERSIONED_REF_RE = re.compile(r"(?<![A-Za-z0-9_])[a-z][a-z0-9_]*@[1-9][0-9]*(?![A-Za-z0-9_])")
_EVIDENCE_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:query:[a-z0-9_-]+|product:[a-z][a-z0-9_]*@[1-9][0-9]*)"
    r":[0-9a-f]{16}(?![A-Za-z0-9_])"
)
_SOURCE_URL_RE = re.compile(r"""https?://[^\s<>"'`()\[\]，。；！？、（）]+""")

_STATUS_LABELS = {
    "pending": "待处理",
    "running": "运行中",
    "passed": "已完成",
    "partial": "部分完成",
    "blocked": "已阻断",
    "failed": "失败",
    "cancelled": "已取消",
    "warning": "需谨慎",
}
_STANCE_LABELS = {
    "watch_buy": "关注潜在买入机会",
    "watch_add": "关注增加配置机会",
    "hold": "持有观察",
    "watch_reduce": "关注降低配置",
    "watch_exit": "关注退出条件",
}
_CONVICTION_LABELS = {"low": "低", "medium": "中", "high": "高"}
_QUALITY_LABELS = {"passed": "通过", "warning": "需谨慎", "blocked": "不可用"}
_OUTCOME_LABELS = {
    "flat": "基本持平",
    "aligned": "与早间判断一致",
    "misaligned": "与早间判断不一致",
    "risk_review": "需要重新检查风险",
}
_AGENT_LABELS = {
    "market": "市场走势专家",
    "social": "市场讨论专家",
    "news": "新闻信息专家",
    "fundamentals": "基本面专家",
    "policy": "政策环境专家",
    "hot_money": "资金与短线活跃度专家",
    "lockup": "解禁与股东变化专家",
}
_STAGE_LABELS = {
    "bull_review": "看多观点",
    "bear_review": "看空观点",
    "research_manager": "研究负责人判断",
    "trader": "观察方案",
    "aggressive_risk": "进取型风险审查",
    "neutral_risk": "中性风险审查",
    "conservative_risk": "保守型风险审查",
    "portfolio_manager": "投资组合负责人结论",
}


def validate_research_finding_language(finding: Any) -> None:
    fields: list[tuple[str, str | None]] = [("summary", finding.summary)]
    fields.extend(("claims.statement", claim.statement) for claim in finding.claims)
    fields.extend(("evidence.excerpt", item.excerpt) for item in finding.evidence)
    fields.extend(("risks", item) for item in finding.risks)
    fields.extend(("invalidation_conditions", item) for item in finding.invalidation_conditions)
    ensure_plain_chinese(fields)


def validate_decision_output_language(output: Any) -> None:
    from advisor.research.contracts import TeamConclusion
    from advisor.research.decision.contracts import StageReview, TraderProposal

    fields: list[tuple[str, str | None]] = []
    if isinstance(output, StageReview):
        fields.append(("summary", output.summary))
        fields.extend(("claims.statement", claim.statement) for claim in output.claims)
        fields.extend(("risks", item) for item in output.risks)
        fields.extend(("invalidation_conditions", item) for item in output.invalidation_conditions)
    elif isinstance(output, (TraderProposal, TeamConclusion)):
        fields.append(("thesis", output.thesis))
        fields.extend(("key_risks", item) for item in output.key_risks)
        fields.extend(("invalidation_conditions", item) for item in output.invalidation_conditions)
        fields.append(("time_horizon", output.time_horizon))
        fields.append(("position_limit", output.position_limit))
        quality = output.quality if isinstance(output, TraderProposal) else output.evidence_quality
        fields.extend(("quality.limitations", item) for item in quality.limitations)
    fields.extend(("evidence.excerpt", item.excerpt) for item in getattr(output, "evidence", ()))
    ensure_plain_chinese(fields)


def ensure_plain_chinese(fields: Iterable[tuple[str, str | None]]) -> None:
    for field, value in fields:
        if value is None or not value.strip():
            continue
        # References are machine identifiers, not reader-facing prose. Count
        # only the remaining text so long product names and source URLs cannot
        # outweigh an otherwise Chinese sentence. References alone still fail.
        prose = _SOURCE_URL_RE.sub("", value)
        prose = _VERSIONED_REF_RE.sub("", _EVIDENCE_REF_RE.sub("", prose))
        cjk_count = len(_CJK_RE.findall(prose))
        if cjk_count == 0:
            raise ValueError(f"{field} 必须使用简体中文")
        latin_count = len(_LATIN_RE.findall(prose))
        if latin_count > max(18, cjk_count):
            raise ValueError(f"{field} 必须使用简体中文，不能包含英文段落")


def status_label(value: str) -> str:
    return _STATUS_LABELS.get(value, value)


def stance_label(value: str) -> str:
    return _STANCE_LABELS.get(value, value)


def conviction_label(value: str) -> str:
    return _CONVICTION_LABELS.get(value, value)


def quality_label(value: str) -> str:
    return _QUALITY_LABELS.get(value, value)


def outcome_label(value: str | None) -> str:
    if value is None:
        return "暂无结果"
    return _OUTCOME_LABELS.get(value, value)


def agent_label(agent_ref: str) -> str:
    agent_id = agent_ref.split("@", 1)[0]
    return _AGENT_LABELS.get(agent_id, "研究专家")


def stage_label(stage: str) -> str:
    return _STAGE_LABELS.get(stage, "决策审查")


__all__ = [
    "OUTPUT_LANGUAGE_POLICY",
    "PLAIN_CHINESE_PROMPT",
    "agent_label",
    "conviction_label",
    "ensure_plain_chinese",
    "outcome_label",
    "quality_label",
    "stage_label",
    "stance_label",
    "status_label",
    "validate_decision_output_language",
    "validate_research_finding_language",
]
