from types import SimpleNamespace

import pytest

from advisor.research.language import ensure_plain_chinese, validate_research_finding_language


@pytest.mark.parametrize("field", ["summary", "risks"])
def test_chinese_finding_allows_versioned_product_references(field):
    text = "依据 market_information@1 和 whole_market_intraday_snapshot@1，政策影响仍需观察。"
    finding = SimpleNamespace(
        summary=text if field == "summary" else "政策影响仍需观察。",
        claims=[], evidence=[], risks=[text] if field == "risks" else [],
        invalidation_conditions=[],
    )
    validate_research_finding_language(finding)


def test_chinese_source_reference_does_not_count_url_as_english_prose():
    ensure_plain_chinese([("evidence.excerpt", "政策来源：https://www.example.gov.cn/policy/2026/09/07/announcement.html，仍需核实执行情况。")])


@pytest.mark.parametrize("reference", [
    "query:history-observed-session-coverage:6050609273c7f8a4",
    "product:whole_market_intraday_snapshot@1:57337dc98a5fd76a",
])
def test_chinese_finding_allows_structured_evidence_identifiers(reference):
    ensure_plain_chinese([("claims.statement", f"依据 {reference}，样本仅覆盖已观测日期。")])
    with pytest.raises(ValueError, match="必须使用简体中文"):
        ensure_plain_chinese([("claims.statement", reference)])


@pytest.mark.parametrize("text", [
    "The policy outlook remains uncertain and requires further evidence.",
    "政策：The policy outlook remains uncertain and requires further evidence.",
    "政策 market_information@1: The policy outlook remains uncertain and requires further evidence.",
    "market_information@1",
    "https://www.example.gov.cn/policy/announcement.html",
])
def test_references_do_not_allow_english_prose_or_replace_chinese(text):
    with pytest.raises(ValueError, match="必须使用简体中文"):
        ensure_plain_chinese([("summary", text)])
