from __future__ import annotations

from datetime import datetime, timezone

import pytest

from advisor.research.contracts import EvidenceRef, ResearchSubject
from advisor.research.decision.contracts import ResearchCandidate


def _candidate(**overrides: object) -> ResearchCandidate:
    payload = {
        "method": "成交与质量筛选",
        "code": "600519",
        "rationale": "仅作为未来研究输入，结论可由关联证据复核。",
        "evidence": (EvidenceRef(evidence_id="fixture:1", source="fixture", observed_at=datetime(2026, 8, 6, tzinfo=timezone.utc)),),
        "risks": ("样本可能失效。",),
    }
    payload.update(overrides)
    return ResearchCandidate(**payload)


def test_research_candidate_is_bounded_and_never_becomes_a_security_proposal():
    candidate = _candidate(rank=1)

    assert candidate.selection_method == "成交与质量筛选"
    assert candidate.method == "成交与质量筛选"
    assert candidate.rank == 1

    with pytest.raises(Exception):
        _candidate(price_range={"low": 1.0, "high": 2.0})
    with pytest.raises(Exception):
        _candidate(child_request={"team_ref": "normal@1"})


def test_scope_contract_rejects_cdr_codes_consistently_with_the_market_universe():
    with pytest.raises(ValueError, match="six-digit A-share"):
        ResearchSubject(code="689009")
    with pytest.raises(Exception):
        _candidate(code="689009")
