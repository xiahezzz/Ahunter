import json
import hashlib
import os
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


def completion_marker(paths) -> Path:
    return paths.json_path.with_name(f"{paths.json_path.stem}.complete.json")


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
    marker_path = completion_marker(paths)
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["report_date"] == "2026-07-11"
    assert marker["report_type"] == "premarket"
    assert marker["run_id"] == "initial"
    assert marker["files"] == {
        "json": {
            "name": "premarket.json",
            "sha256": hashlib.sha256(paths.json_path.read_bytes()).hexdigest(),
        },
        "markdown": {
            "name": "premarket.md",
            "sha256": hashlib.sha256(paths.markdown_path.read_bytes()).hexdigest(),
        },
    }


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


@pytest.mark.parametrize("damage", ["missing_marker", "invalid_marker", "markdown_hash", "json_hash"])
def test_review_requires_complete_hash_verified_premarket_archive(tmp_path: Path, damage: str):
    paths = archive_morning_advice(tmp_path)
    marker_path = completion_marker(paths)
    if damage == "missing_marker":
        marker_path.unlink()
    elif damage == "invalid_marker":
        marker_path.write_text("not json", encoding="utf-8")
    elif damage == "markdown_hash":
        paths.markdown_path.write_text("changed markdown", encoding="utf-8")
    else:
        paths.json_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid premarket archive"):
        write_review_report(
            "2026-07-11",
            [],
            [],
            tmp_path,
            quality_results=passing_quality(),
        )


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


@pytest.mark.parametrize("missing_name", ["premarket.md", "premarket.json", "premarket.complete.json"])
def test_versioned_rerun_requires_complete_predecessor_pair(tmp_path: Path, missing_name: str):
    archive_morning_advice(tmp_path)
    (tmp_path / "2026-07-11" / missing_name).unlink()

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


class SimulatedCrash(BaseException):
    pass


def public_names(date_dir: Path) -> list[str]:
    return sorted(path.name for path in date_dir.iterdir() if not path.name.startswith("."))


def test_second_link_failure_never_deletes_first_public_file(tmp_path: Path, monkeypatch):
    real_link = contracts.os.link

    def fail_json_link(source: str, destination: str, *args, **kwargs) -> None:
        if Path(destination).name == "premarket.json":
            raise OSError("simulated JSON link failure")
        real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(contracts.os, "link", fail_json_link)

    with pytest.raises(OSError, match="simulated JSON link failure"):
        write_premarket_report("2026-07-11", [], tmp_path, quality_results=passing_quality())

    date_dir = tmp_path / "2026-07-11"
    assert (date_dir / "premarket.md").is_file()
    assert not (date_dir / "premarket.json").exists()
    assert not (date_dir / "premarket.complete.json").exists()
    assert (date_dir / ".premarket.claim").is_file()


def test_publication_never_overwrites_destination_created_before_first_link(tmp_path: Path, monkeypatch):
    real_link = contracts.os.link
    raced = False

    def race_first_link(source: str, destination: str, *args, **kwargs) -> None:
        nonlocal raced
        destination_path = Path(destination)
        if destination_path.name == "premarket.md" and not raced:
            raced = True
            descriptor = os.open(
                destination,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
                dir_fd=kwargs.get("dst_dir_fd"),
            )
            if kwargs.get("dst_dir_fd") is not None:
                os.write(descriptor, b"racer-owned markdown")
                os.close(descriptor)
            else:
                os.close(descriptor)
                destination_path.write_text("racer-owned markdown", encoding="utf-8")
        real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(contracts.os, "link", race_first_link)

    with pytest.raises(FileExistsError):
        write_premarket_report("2026-07-11", [], tmp_path, quality_results=passing_quality())

    date_dir = tmp_path / "2026-07-11"
    assert (date_dir / "premarket.md").read_text(encoding="utf-8") == "racer-owned markdown"
    assert not (date_dir / "premarket.json").exists()
    assert public_names(date_dir) == ["premarket.md"]


def test_second_link_failure_preserves_replaced_and_preexisting_destinations(tmp_path: Path, monkeypatch):
    real_link = contracts.os.link
    date_dir = tmp_path / "2026-07-11"

    def replace_first_before_failing_second(source: str, destination: str, *args, **kwargs) -> None:
        destination_path = Path(destination)
        if destination_path.name == "premarket.json":
            markdown_path = date_dir / "premarket.md"
            markdown_path.unlink()
            markdown_path.write_text("replacement markdown", encoding="utf-8")
            (date_dir / destination_path).write_text("preexisting json", encoding="utf-8")
        real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(contracts.os, "link", replace_first_before_failing_second)

    with pytest.raises(FileExistsError):
        write_premarket_report("2026-07-11", [], tmp_path, quality_results=passing_quality())

    assert (date_dir / "premarket.md").read_text(encoding="utf-8") == "replacement markdown"
    assert (date_dir / "premarket.json").read_text(encoding="utf-8") == "preexisting json"
    assert public_names(date_dir) == ["premarket.json", "premarket.md"]
    assert not (date_dir / "premarket.complete.json").exists()


def test_concurrent_publication_uses_live_exclusive_report_claim(tmp_path: Path, monkeypatch):
    first_writer_started = threading.Event()
    release_first_writer = threading.Event()
    thread_errors: list[Exception] = []

    def publication_hook(event: str, **_context) -> None:
        if event == "claim_acquired" and not first_writer_started.is_set():
            first_writer_started.set()
            assert release_first_writer.wait(timeout=2)

    def run_first_writer() -> None:
        try:
            write_premarket_report("2026-07-11", [], tmp_path, quality_results=passing_quality())
        except Exception as error:  # pragma: no cover - assertion reports captured thread errors
            thread_errors.append(error)

    monkeypatch.setattr(contracts, "_publication_hook", publication_hook, raising=False)
    thread = threading.Thread(target=run_first_writer)
    thread.start()
    assert first_writer_started.wait(timeout=2)
    try:
        with pytest.raises(FileExistsError, match="live publication claim"):
            write_premarket_report("2026-07-11", [], tmp_path, quality_results=passing_quality())
    finally:
        release_first_writer.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert thread_errors == []
    assert (tmp_path / "2026-07-11" / "premarket.json").is_file()


def test_stale_claim_recovers_interruption_after_first_final_link(tmp_path: Path, monkeypatch):
    crashed = False

    def crash_after_markdown(event: str, **_context) -> None:
        nonlocal crashed
        if event == "after_markdown_link" and not crashed:
            crashed = True
            raise SimulatedCrash()

    monkeypatch.setattr(contracts, "_publication_hook", crash_after_markdown, raising=False)
    with pytest.raises(SimulatedCrash):
        write_premarket_report("2026-07-11", [], tmp_path, quality_results=passing_quality())

    date_dir = tmp_path / "2026-07-11"
    assert public_names(date_dir) == ["premarket.md"]
    claim = json.loads((date_dir / ".premarket.claim").read_text(encoding="utf-8"))
    assert claim["pid"] == os.getpid()
    assert claim["report_date"] == "2026-07-11"
    assert claim["report_type"] == "premarket"
    assert claim["run_id"] == "initial"

    monkeypatch.setattr(contracts, "_publication_hook", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(contracts, "_pid_is_alive", lambda _pid: False, raising=False)
    paths = write_premarket_report("2026-07-11", [], tmp_path, quality_results=passing_quality())

    assert paths.markdown_path.is_file()
    assert paths.json_path.is_file()
    assert completion_marker(paths).is_file()
    assert not (date_dir / ".premarket.claim").exists()


def test_stale_recovery_rejects_conflicting_destination_without_deleting_public_files(
    tmp_path: Path,
    monkeypatch,
):
    def crash_after_markdown(event: str, **_context) -> None:
        if event == "after_markdown_link":
            raise SimulatedCrash()

    monkeypatch.setattr(contracts, "_publication_hook", crash_after_markdown, raising=False)
    with pytest.raises(SimulatedCrash):
        write_premarket_report("2026-07-11", [], tmp_path, quality_results=passing_quality())

    date_dir = tmp_path / "2026-07-11"
    (date_dir / "premarket.json").write_text("conflicting json", encoding="utf-8")
    monkeypatch.setattr(contracts, "_publication_hook", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(contracts, "_pid_is_alive", lambda _pid: False, raising=False)
    with pytest.raises(ValueError, match="conflicting recovery destination"):
        write_premarket_report("2026-07-11", [], tmp_path, quality_results=passing_quality())

    assert (date_dir / "premarket.md").is_file()
    assert (date_dir / "premarket.json").read_text(encoding="utf-8") == "conflicting json"
    assert not (date_dir / "premarket.complete.json").exists()


def test_stale_recovery_rejects_claim_with_unbound_stage_path(tmp_path: Path, monkeypatch):
    date_dir = tmp_path / "2026-07-11"
    date_dir.mkdir()
    claim = {
        "pid": 999999,
        "report_date": "2026-07-11",
        "report_type": "premarket",
        "run_id": "initial",
        "stage": "../escape",
        "transaction_id": "a" * 32,
    }
    (date_dir / ".premarket.claim").write_text(json.dumps(claim), encoding="utf-8")
    monkeypatch.setattr(contracts, "_pid_is_alive", lambda _pid: False)

    with pytest.raises(ValueError, match="invalid publication claim"):
        archive_morning_advice(tmp_path)

    assert public_names(date_dir) == []


def test_final_links_are_fd_relative_and_completion_marker_is_last(tmp_path: Path, monkeypatch):
    real_link = contracts.os.link
    links: list[tuple[str, dict]] = []

    def record_link(source: str, destination: str, *args, **kwargs) -> None:
        links.append((str(destination), kwargs))
        real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(contracts.os, "link", record_link)
    archive_morning_advice(tmp_path)

    assert [name for name, _ in links] == ["premarket.md", "premarket.json", "premarket.complete.json"]
    assert all(call["src_dir_fd"] is not None and call["dst_dir_fd"] is not None for _, call in links)


def test_staged_transaction_directory_is_durable_before_first_public_link(tmp_path: Path, monkeypatch):
    real_fsync = contracts.os.fsync
    real_link = contracts.os.link
    date_fd: int | None = None
    events: list[str] = []

    def publication_hook(event: str, **context) -> None:
        nonlocal date_fd
        if event == "date_fd_opened":
            date_fd = context["date_fd"]
        events.append(event)

    def record_fsync(descriptor: int) -> None:
        if descriptor == date_fd:
            events.append("date_fsync")
        real_fsync(descriptor)

    def record_link(source: str, destination: str, *args, **kwargs) -> None:
        events.append(f"link:{destination}")
        real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(contracts, "_publication_hook", publication_hook)
    monkeypatch.setattr(contracts.os, "fsync", record_fsync)
    monkeypatch.setattr(contracts.os, "link", record_link)
    archive_morning_advice(tmp_path)

    claim_index = events.index("claim_acquired")
    stage_index = events.index("stage_manifest_durable")
    first_link_index = events.index("link:premarket.md")
    assert "date_fsync" in events[claim_index + 1 : stage_index]
    assert stage_index < first_link_index


def test_date_path_swap_after_fd_open_cannot_redirect_publication(tmp_path: Path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    held_date = tmp_path / "held-date"

    def swap_date_path(event: str, **context) -> None:
        if event == "date_fd_opened":
            date_path = tmp_path / context["report_date"]
            date_path.rename(held_date)
            date_path.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(contracts, "_publication_hook", swap_date_path, raising=False)
    with pytest.raises(ValueError, match="report date path changed"):
        archive_morning_advice(tmp_path)

    assert list(outside.iterdir()) == []
    assert public_names(held_date) == []


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
