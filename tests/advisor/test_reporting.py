import json
from pathlib import Path
import threading

import pytest

from advisor import paths as advisor_paths
from advisor.quality import QualityResult
from advisor.reporting import contracts
from advisor.reporting.contracts import AdviceItem, ReviewItem, normalize_quality_results
from advisor.reporting.premarket import write_premarket_report
from advisor.reporting.review import write_review_report


def passing_quality() -> list[QualityResult]:
    return [QualityResult("market_data", "blocking", True, "market data is current")]


@pytest.fixture(autouse=True)
def use_temporary_reports_root(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: tmp_path)


def archive_morning_advice(root: Path, advice: list[AdviceItem] | None = None):
    return write_premarket_report(
        "2026-07-11",
        advice or [],
        root,
        quality_results=passing_quality(),
    )


@pytest.mark.parametrize(
    "result",
    [
        QualityResult("market_data", "blocking", "false", "current"),
        QualityResult("market_data", "critical", True, "current"),
        QualityResult("market_data", [], True, "current"),
        QualityResult("market_data", "blocking", True, " "),
        QualityResult("", "blocking", True, "current"),
    ],
)
def test_malformed_quality_fields_fail_closed(result: QualityResult):
    status, payload = normalize_quality_results([result])

    assert status == "blocked"
    assert payload == [
        {
            "check_name": "quality_input",
            "details": "missing or ambiguous quality input",
            "passed": False,
            "severity": "blocking",
        }
    ]


def test_valid_nonblocking_warning_does_not_block_recommendations():
    warning = QualityResult("optional_news", "warning", False, "source unavailable")

    status, payload = normalize_quality_results([warning])

    assert status == "passed"
    assert payload[0]["passed"] is False


def test_write_premarket_report_archives_markdown_and_json(tmp_path: Path):
    paths = write_premarket_report(
        "2026-07-11",
        [AdviceItem("adv-1", "600519", "watch", 0.72, "放量并有信息流催化", ["ev-1"])],
        tmp_path,
        quality_results=passing_quality(),
    )

    assert paths.markdown_path.name == "premarket.md"
    assert paths.json_path.name == "premarket.json"
    assert paths.markdown_path.parent == tmp_path / "2026-07-11"
    assert "08:30 Premarket Advice" in paths.markdown_path.read_text(encoding="utf-8")
    payload = json.loads(paths.json_path.read_text(encoding="utf-8"))
    assert payload["advice"][0]["advice_id"] == "adv-1"


def test_write_review_report_links_to_morning_advice(tmp_path: Path):
    advice = [AdviceItem("adv-1", "600519", "watch", 0.72, "morning rationale", ["ev-1"])]
    archive_morning_advice(tmp_path, advice)

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
    payload = json.loads(paths.json_path.read_text(encoding="utf-8"))
    assert payload["linked_premarket"] == {
        "json_file": "premarket.json",
        "report_date": "2026-07-11",
        "report_time": "08:30",
        "report_type": "premarket",
        "run_id": "initial",
    }


@pytest.mark.parametrize("writer", ["premarket", "review"])
def test_missing_or_blocking_quality_archives_a_blocked_report_without_recommendations(
    tmp_path: Path,
    writer: str,
):
    advice = [AdviceItem("adv-1", "600519", "watch", 0.72, "do not publish this rationale", ["ev-1"])]
    blocked_quality = [QualityResult("three_year_history", "blocking", False, "only one market row is available")]

    if writer == "premarket":
        paths = write_premarket_report("2026-07-11", advice, tmp_path, quality_results=blocked_quality)
    else:
        paths = write_review_report(
            "2026-07-11",
            advice,
            [ReviewItem("rev-1", "adv-1", "valid", "do not publish this review")],
            tmp_path,
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


def test_blocked_reports_skip_advice_review_and_context_validation(tmp_path: Path):
    blocked_quality = [QualityResult("market_data", "blocking", False, "stale market data")]
    advice = AdviceItem("adv-1", "600519", "watch", 0.72, "hidden", ["ev-1"])

    premarket = write_premarket_report(
        "2026-07-11",
        [advice, advice],
        tmp_path,
        quality_results=blocked_quality,
        context=object(),
    )
    review = write_review_report(
        "2026-07-11",
        [advice, advice],
        [ReviewItem("rev-1", "orphan", "unknown", "hidden")],
        tmp_path,
        quality_results=blocked_quality,
        context=object(),
    )

    premarket_payload = json.loads(premarket.json_path.read_text(encoding="utf-8"))
    review_payload = json.loads(review.json_path.read_text(encoding="utf-8"))
    assert premarket_payload["advice"] == []
    assert premarket_payload["context"] == {}
    assert review_payload["morning_advice"] == []
    assert review_payload["reviews"] == []
    assert review_payload["context"] == {}


def test_blocked_review_still_rejects_unsafe_premarket_run_metadata(tmp_path: Path):
    blocked_quality = [QualityResult("market_data", "blocking", False, "stale market data")]

    with pytest.raises(ValueError, match="unsafe run_id"):
        write_review_report(
            "2026-07-11",
            [],
            [],
            tmp_path,
            quality_results=blocked_quality,
            premarket_run_id="../escape",
        )


def test_reports_render_required_sections_and_deterministic_metadata_for_minimal_context(tmp_path: Path):
    premarket = write_premarket_report("2026-07-11", [], tmp_path, quality_results=passing_quality())
    review = write_review_report("2026-07-11", [], [], tmp_path, quality_results=passing_quality())

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
    archive_morning_advice(tmp_path)
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
    archive_morning_advice(tmp_path, advice)
    with pytest.raises(ValueError, match="orphan advice_id: missing"):
        write_review_report(
            "2026-07-11",
            advice,
            [ReviewItem("rev-1", "missing", "invalid", "review")],
            tmp_path,
            quality_results=passing_quality(),
        )
    assert not (tmp_path / "2026-07-11" / "review.md").exists()
    assert not (tmp_path / "2026-07-11" / "review.json").exists()

    with pytest.raises(ValueError, match="duplicate advice_id: adv-1"):
        write_premarket_report(
            "2026-07-12",
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
    assert not (tmp_path / "2026-07-12" / "premarket.md").exists()
    assert not (tmp_path / "2026-07-11" / "review.md").exists()


def test_passed_review_requires_same_day_matching_premarket_archive(tmp_path: Path):
    advice = [AdviceItem("adv-1", "600519", "watch", 0.72, "morning rationale", ["ev-1"])]
    with pytest.raises(ValueError, match="premarket archive not found"):
        write_review_report(
            "2026-07-11",
            advice,
            [ReviewItem("rev-1", "adv-1", "valid", "review")],
            tmp_path,
            quality_results=passing_quality(),
        )

    archive_morning_advice(tmp_path, advice)
    changed_advice = [AdviceItem("adv-1", "600519", "watch", 0.72, "changed rationale", ["ev-1"])]
    with pytest.raises(ValueError, match="morning advice does not match premarket archive"):
        write_review_report(
            "2026-07-11",
            changed_advice,
            [ReviewItem("rev-1", "adv-1", "valid", "review")],
            tmp_path,
            quality_results=passing_quality(),
        )
    assert not (tmp_path / "2026-07-11" / "review.json").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("report_type", "review"),
        ("report_date", "2026-07-10"),
        ("run_id", "other"),
        ("quality_status", "blocked"),
    ],
)
def test_review_rejects_invalid_premarket_archive_metadata(
    tmp_path: Path,
    field: str,
    value: str,
):
    paths = archive_morning_advice(tmp_path)
    payload = json.loads(paths.json_path.read_text(encoding="utf-8"))
    payload[field] = value
    paths.json_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid premarket archive"):
        write_review_report(
            "2026-07-11",
            [],
            [],
            tmp_path,
            quality_results=passing_quality(),
        )
    assert not (tmp_path / "2026-07-11" / "review.json").exists()


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


def test_versioned_rerun_rejects_missing_predecessor(tmp_path: Path):
    with pytest.raises(ValueError, match="predecessor archive not found"):
        write_premarket_report(
            "2026-07-11",
            [],
            tmp_path,
            quality_results=passing_quality(),
            run_id="rerun-1",
            rerun_reason="corrected source",
            supersedes="initial",
        )
    assert not (tmp_path / "2026-07-11" / "premarket.rerun-1.json").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [("report_type", "review"), ("report_date", "2026-07-10"), ("run_id", "other")],
)
def test_versioned_rerun_rejects_wrong_predecessor_metadata(
    tmp_path: Path,
    field: str,
    value: str,
):
    paths = archive_morning_advice(tmp_path)
    payload = json.loads(paths.json_path.read_text(encoding="utf-8"))
    payload[field] = value
    paths.json_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid predecessor archive"):
        write_premarket_report(
            "2026-07-11",
            [],
            tmp_path,
            quality_results=passing_quality(),
            run_id="rerun-1",
            rerun_reason="corrected source",
            supersedes="initial",
        )


def test_versioned_rerun_rejects_malformed_or_symlinked_predecessor(tmp_path: Path):
    date_dir = tmp_path / "2026-07-11"
    date_dir.mkdir()
    predecessor = date_dir / "premarket.json"
    predecessor.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid predecessor archive"):
        write_premarket_report(
            "2026-07-11",
            [],
            tmp_path,
            quality_results=passing_quality(),
            run_id="rerun-1",
            rerun_reason="corrected source",
            supersedes="initial",
        )

    predecessor.unlink()
    predecessor.symlink_to(tmp_path / "outside.json")
    with pytest.raises(ValueError, match="symlinked predecessor archive"):
        write_premarket_report(
            "2026-07-11",
            [],
            tmp_path,
            quality_results=passing_quality(),
            run_id="rerun-1",
            rerun_reason="corrected source",
            supersedes="initial",
        )


def test_premarket_atomic_pair_write_cleans_up_after_second_link_fails(tmp_path: Path, monkeypatch):
    real_link = contracts.os.link

    def fail_json_link(source: str, destination: str) -> None:
        if Path(destination).name == "premarket.json":
            raise OSError("simulated JSON link failure")
        real_link(source, destination)

    monkeypatch.setattr(contracts.os, "link", fail_json_link)

    with pytest.raises(OSError, match="simulated JSON link failure"):
        write_premarket_report("2026-07-11", [], tmp_path, quality_results=passing_quality())

    assert not (tmp_path / "2026-07-11" / "premarket.md").exists()
    assert not (tmp_path / "2026-07-11" / "premarket.json").exists()
    assert list((tmp_path / "2026-07-11").iterdir()) == []


def test_publication_never_overwrites_destination_created_before_first_link(tmp_path: Path, monkeypatch):
    real_link = contracts.os.link
    raced = False

    def race_first_link(source: str, destination: str) -> None:
        nonlocal raced
        destination_path = Path(destination)
        if destination_path.name == "premarket.md" and not raced:
            raced = True
            destination_path.write_text("racer-owned markdown", encoding="utf-8")
        real_link(source, destination)

    monkeypatch.setattr(contracts.os, "link", race_first_link)

    with pytest.raises(FileExistsError):
        write_premarket_report("2026-07-11", [], tmp_path, quality_results=passing_quality())

    date_dir = tmp_path / "2026-07-11"
    assert (date_dir / "premarket.md").read_text(encoding="utf-8") == "racer-owned markdown"
    assert not (date_dir / "premarket.json").exists()
    assert sorted(path.name for path in date_dir.iterdir()) == ["premarket.md"]


def test_second_link_failure_preserves_replaced_and_preexisting_destinations(tmp_path: Path, monkeypatch):
    real_link = contracts.os.link
    date_dir = tmp_path / "2026-07-11"

    def replace_first_before_failing_second(source: str, destination: str) -> None:
        destination_path = Path(destination)
        if destination_path.name == "premarket.json":
            markdown_path = date_dir / "premarket.md"
            markdown_path.unlink()
            markdown_path.write_text("replacement markdown", encoding="utf-8")
            destination_path.write_text("preexisting json", encoding="utf-8")
        real_link(source, destination)

    monkeypatch.setattr(contracts.os, "link", replace_first_before_failing_second)

    with pytest.raises(FileExistsError):
        write_premarket_report("2026-07-11", [], tmp_path, quality_results=passing_quality())

    assert (date_dir / "premarket.md").read_text(encoding="utf-8") == "replacement markdown"
    assert (date_dir / "premarket.json").read_text(encoding="utf-8") == "preexisting json"
    assert sorted(path.name for path in date_dir.iterdir()) == ["premarket.json", "premarket.md"]


def test_concurrent_publication_uses_exclusive_report_claim(tmp_path: Path, monkeypatch):
    real_write_temporary = contracts._write_temporary_file
    first_writer_started = threading.Event()
    release_first_writer = threading.Event()
    first_call = True
    thread_errors: list[Exception] = []

    def pause_first_temporary_write(output_dir: Path, content: str) -> Path:
        nonlocal first_call
        if first_call:
            first_call = False
            first_writer_started.set()
            assert release_first_writer.wait(timeout=2)
        return real_write_temporary(output_dir, content)

    def run_first_writer() -> None:
        try:
            write_premarket_report("2026-07-11", [], tmp_path, quality_results=passing_quality())
        except Exception as error:  # pragma: no cover - assertion reports captured thread errors
            thread_errors.append(error)

    monkeypatch.setattr(contracts, "_write_temporary_file", pause_first_temporary_write)
    thread = threading.Thread(target=run_first_writer)
    thread.start()
    assert first_writer_started.wait(timeout=2)
    try:
        with pytest.raises(FileExistsError, match="report publication already claimed"):
            write_premarket_report("2026-07-11", [], tmp_path, quality_results=passing_quality())
    finally:
        release_first_writer.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert thread_errors == []
    assert (tmp_path / "2026-07-11" / "premarket.json").is_file()


@pytest.mark.parametrize("report_date", ["2026-7-11", "../2026-07-11", "not-a-date"])
def test_report_date_must_be_canonical_iso(report_date: str, tmp_path: Path):
    with pytest.raises(ValueError, match="invalid report_date"):
        write_premarket_report(report_date, [], tmp_path, quality_results=passing_quality())


def test_output_root_must_equal_configured_reports_root(tmp_path: Path):
    with pytest.raises(ValueError, match="output_dir must equal configured reports root"):
        write_premarket_report(
            "2026-07-11",
            [],
            tmp_path / "other",
            quality_results=passing_quality(),
        )
    with pytest.raises(ValueError, match="unsafe output_dir"):
        write_premarket_report(
            "2026-07-11",
            [],
            tmp_path / "nested" / "..",
            quality_results=passing_quality(),
        )


def test_symlinked_root_date_or_report_path_is_rejected(tmp_path: Path, monkeypatch):
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: linked_root)
    with pytest.raises(ValueError, match="symlinked reports root"):
        write_premarket_report("2026-07-11", [], linked_root, quality_results=passing_quality())

    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: real_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (real_root / "2026-07-12").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked report date path"):
        write_premarket_report("2026-07-12", [], real_root, quality_results=passing_quality())

    date_dir = real_root / "2026-07-13"
    date_dir.mkdir()
    (date_dir / "premarket.md").symlink_to(tmp_path / "outside.md")
    with pytest.raises(ValueError, match="symlinked report path"):
        write_premarket_report("2026-07-13", [], real_root, quality_results=passing_quality())
