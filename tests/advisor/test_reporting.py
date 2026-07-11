import json
from pathlib import Path

import pytest

from advisor.quality import QualityResult
from advisor.reporting import contracts
from advisor.reporting.contracts import AdviceItem, ReviewItem
from advisor.reporting.premarket import write_premarket_report
from advisor.reporting.review import write_review_report


def passing_quality() -> list[QualityResult]:
    return [QualityResult("market_data", "blocking", True, "market data is current")]


def test_write_premarket_report_archives_markdown_and_json(tmp_path: Path):
    paths = write_premarket_report(
        "2026-07-11",
        [AdviceItem("adv-1", "600519", "watch", 0.72, "放量并有信息流催化", ["ev-1"])],
        tmp_path,
        quality_results=passing_quality(),
    )

    assert paths.markdown_path.name == "premarket.md"
    assert paths.json_path.name == "premarket.json"
    assert "08:30 Premarket Advice" in paths.markdown_path.read_text(encoding="utf-8")
    payload = json.loads(paths.json_path.read_text(encoding="utf-8"))
    assert payload["advice"][0]["advice_id"] == "adv-1"


def test_write_review_report_links_to_morning_advice(tmp_path: Path):
    advice = [AdviceItem("adv-1", "600519", "watch", 0.72, "morning rationale", ["ev-1"])]

    paths = write_review_report(
        "2026-07-11",
        advice,
        [ReviewItem("rev-1", "adv-1", "partly_valid", "量能延续但未触发买入条件")],
        tmp_path,
        quality_results=passing_quality(),
    )

    text = paths.markdown_path.read_text(encoding="utf-8")
    assert paths.markdown_path.name == "review.md"
    assert paths.json_path.name == "review.json"
    assert "22:30 Daily Review" in text
    assert "adv-1" in text


@pytest.mark.parametrize("writer", ["premarket", "review"])
def test_missing_or_blocking_quality_archives_a_blocked_report_without_recommendations(
    tmp_path: Path,
    writer: str,
):
    advice = [AdviceItem("adv-1", "600519", "watch", 0.72, "do not publish this rationale", ["ev-1"])]
    blocked_quality = [QualityResult("three_year_history", "blocking", False, "only one market row is available")]

    if writer == "premarket":
        paths = write_premarket_report("2026-07-11", advice, tmp_path / "blocked", quality_results=blocked_quality)
    else:
        paths = write_review_report(
            "2026-07-11",
            advice,
            [ReviewItem("rev-1", "adv-1", "valid", "do not publish this review")],
            tmp_path / "blocked",
            quality_results=blocked_quality,
        )

    payload = json.loads(paths.json_path.read_text(encoding="utf-8"))
    text = paths.markdown_path.read_text(encoding="utf-8")
    assert payload["quality_status"] == "blocked"
    assert payload["quality_results"][0]["check_name"] == "three_year_history"
    assert payload["quality_results"][0]["details"] == "only one market row is available"
    assert payload["advice"] == [] if writer == "premarket" else payload["morning_advice"] == []
    assert "do not publish this rationale" not in text
    assert "do not publish this review" not in text


def test_missing_quality_fails_closed_and_is_archived(tmp_path: Path):
    paths = write_premarket_report(
        "2026-07-11",
        [AdviceItem("adv-1", "600519", "watch", 0.72, "hidden", ["ev-1"])],
        tmp_path,
    )

    payload = json.loads(paths.json_path.read_text(encoding="utf-8"))
    assert payload["quality_status"] == "blocked"
    assert payload["advice"] == []
    assert payload["quality_results"][0]["check_name"] == "quality_input"


def test_blocked_reports_do_not_archive_recommendation_context(tmp_path: Path):
    blocked_quality = [QualityResult("market_data", "blocking", False, "stale market data")]
    paths = write_premarket_report(
        "2026-07-11",
        [],
        tmp_path,
        quality_results=blocked_quality,
        context={"analyst_flow": ["Buy 600519 immediately"]},
    )

    payload = json.loads(paths.json_path.read_text(encoding="utf-8"))
    assert "Buy 600519 immediately" not in paths.markdown_path.read_text(encoding="utf-8")
    assert payload["context"] == {}


def test_reports_render_required_sections_and_deterministic_metadata_for_minimal_context(tmp_path: Path):
    premarket = write_premarket_report("2026-07-11", [], tmp_path / "premarket", quality_results=passing_quality())
    review = write_review_report("2026-07-11", [], [], tmp_path / "review", quality_results=passing_quality())

    premarket_text = premarket.markdown_path.read_text(encoding="utf-8")
    review_text = review.markdown_path.read_text(encoding="utf-8")
    for section in (
        "Data Quality",
        "Market Regime/Index Context",
        "Information Flow",
        "Capital Flow",
        "Analyst Flow",
        "Suggested Watchlist/Actions",
        "Ledger Exposure/Risk",
        "Entry/Invalidation/Risk Controls",
        "Evidence",
        "Disclaimer",
    ):
        assert f"## {section}" in premarket_text
    for section in (
        "Data Quality",
        "Morning Advice",
        "Market Outcome",
        "Candidate Review",
        "Evidence Worked/Failed/Missing",
        "Ledger Impact",
        "Stock Profile Updates",
        "Next-Day Carryover",
        "Disclaimer",
    ):
        assert f"## {section}" in review_text
    assert premarket_text.count("- No current entries") >= 8
    assert review_text.count("- No current entries") >= 7
    payload = json.loads(premarket.json_path.read_text(encoding="utf-8"))
    assert payload == {
        "advice": [],
        "advice_ids": [],
        "context": {},
        "quality_results": [{"check_name": "market_data", "details": "market data is current", "passed": True, "severity": "blocking"}],
        "quality_status": "passed",
        "report_date": "2026-07-11",
        "report_time": "08:30",
        "report_type": "premarket",
        "run_id": "initial",
        "supersession": None,
    }


def test_reports_render_supplied_context_without_inventing_additional_entries(tmp_path: Path):
    paths = write_review_report(
        "2026-07-11",
        [],
        [],
        tmp_path,
        quality_results=passing_quality(),
        context={"market_outcome": ["CSI 300 closed higher"], "next_day_carryover": ["watch adv-1"]},
    )

    text = paths.markdown_path.read_text(encoding="utf-8")
    payload = json.loads(paths.json_path.read_text(encoding="utf-8"))
    assert "- CSI 300 closed higher" in text
    assert "- watch adv-1" in text
    assert payload["context"] == {
        "market_outcome": ["CSI 300 closed higher"],
        "next_day_carryover": ["watch adv-1"],
    }


def test_review_rejects_orphan_or_duplicate_ids_before_writing_files(tmp_path: Path):
    advice = [AdviceItem("adv-1", "600519", "watch", 0.72, "rationale", ["ev-1"])]
    with pytest.raises(ValueError, match="orphan advice_id: missing"):
        write_review_report(
            "2026-07-11",
            advice,
            [ReviewItem("rev-1", "missing", "invalid", "review")],
            tmp_path,
            quality_results=passing_quality(),
        )
    assert not (tmp_path / "review.md").exists()
    assert not (tmp_path / "review.json").exists()

    with pytest.raises(ValueError, match="duplicate advice_id: adv-1"):
        write_premarket_report(
            "2026-07-11",
            [advice[0], advice[0]],
            tmp_path,
            quality_results=passing_quality(),
        )
    with pytest.raises(ValueError, match="duplicate review_id: rev-1"):
        write_review_report(
            "2026-07-11",
            advice,
            [ReviewItem("rev-1", "adv-1", "valid", "one"), ReviewItem("rev-1", "adv-1", "valid", "two")],
            tmp_path,
            quality_results=passing_quality(),
        )
    assert not (tmp_path / "premarket.md").exists()
    assert not (tmp_path / "review.md").exists()


def test_reports_are_immutable_and_versioned_reruns_record_supersession(tmp_path: Path):
    write_premarket_report("2026-07-11", [], tmp_path, quality_results=passing_quality())
    with pytest.raises(FileExistsError):
        write_premarket_report("2026-07-11", [], tmp_path, quality_results=passing_quality())
    with pytest.raises(ValueError, match="unsafe run_id"):
        write_premarket_report(
            "2026-07-11",
            [],
            tmp_path,
            quality_results=passing_quality(),
            run_id="../escape",
            rerun_reason="corrected source",
            supersedes="initial",
        )

    rerun = write_premarket_report(
        "2026-07-11",
        [],
        tmp_path,
        quality_results=passing_quality(),
        run_id="rerun-1",
        rerun_reason="corrected source",
        supersedes="initial",
    )
    payload = json.loads(rerun.json_path.read_text(encoding="utf-8"))
    assert rerun.markdown_path.name == "premarket.rerun-1.md"
    assert rerun.json_path.name == "premarket.rerun-1.json"
    assert payload["run_id"] == "rerun-1"
    assert payload["supersession"] == {"reason": "corrected source", "supersedes": "initial"}


def test_premarket_atomic_pair_write_cleans_up_after_second_replace_fails(tmp_path: Path, monkeypatch):
    real_replace = contracts.os.replace

    def fail_json_replace(source: str, destination: str) -> None:
        if Path(destination).name == "premarket.json":
            raise OSError("simulated JSON replacement failure")
        real_replace(source, destination)

    monkeypatch.setattr(contracts.os, "replace", fail_json_replace)

    with pytest.raises(OSError, match="simulated JSON replacement failure"):
        write_premarket_report("2026-07-11", [], tmp_path, quality_results=passing_quality())

    assert not (tmp_path / "premarket.md").exists()
    assert not (tmp_path / "premarket.json").exists()
    assert list(tmp_path.iterdir()) == []
