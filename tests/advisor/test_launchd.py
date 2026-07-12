import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from advisor import paths as advisor_paths
from advisor.evidence.mx_adapter import CollectorSnapshot
from advisor.quality import QualityResult
from advisor.scheduler import premarket as scheduler_premarket
from advisor.scheduler import review as scheduler_review
from advisor.scheduler.launchd import main, render_launchd_template, validate_launchd_template


LAUNCHD_DIR = Path("config/launchd")


def test_launchd_templates_are_valid():
    for name in [
        "com.ahunter.advisor-api.plist.template",
        "com.ahunter.advisor-premarket.plist.template",
        "com.ahunter.advisor-review.plist.template",
    ]:
        assert validate_launchd_template(LAUNCHD_DIR / name)


def test_premarket_launchd_uses_composed_refresh_then_advice_command():
    rendered = render_launchd_template(
        LAUNCHD_DIR / "com.ahunter.advisor-premarket.plist.template",
        repo_root=Path("/repo"),
        python=Path("/repo/.venv311/bin/python"),
    )

    assert "advisor.scheduler.premarket" in rendered
    assert "advisor.reporting.premarket" not in rendered


def test_review_launchd_uses_composed_refresh_then_review_command():
    rendered = render_launchd_template(
        LAUNCHD_DIR / "com.ahunter.advisor-review.plist.template",
        repo_root=Path("/repo"),
        python=Path("/repo/.venv311/bin/python"),
    )

    assert "advisor.scheduler.review" in rendered
    assert "advisor.reporting.review" not in rendered


def test_invalid_launchd_template_returns_false(tmp_path):
    template = tmp_path / "invalid.plist.template"
    template.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Label</key><string>com.ahunter.invalid</string></dict></plist>
""",
        encoding="utf-8",
    )

    assert not validate_launchd_template(template)


def test_launchd_validator_cli_accepts_templates():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "advisor.scheduler.launchd",
            str(LAUNCHD_DIR / "com.ahunter.advisor-api.plist.template"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "valid launchd template" in result.stdout


def test_installed_launchd_console_script_imports_advisor_outside_repo(tmp_path):
    script = Path(sys.executable).with_name("advisor-launchd-render")

    result = subprocess.run(
        [str(script), "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Validate or render A Hunter advisor launchd templates" in result.stdout


def test_launchd_render_creates_log_directories_for_output(tmp_path):
    output = tmp_path / "Library" / "LaunchAgents" / "advisor.plist"
    repo_root = tmp_path / "repo"
    logs_dir = repo_root / "logs"

    exit_code = main(
        [
            str(LAUNCHD_DIR / "com.ahunter.advisor-api.plist.template"),
            "--repo-root",
            str(repo_root),
            "--python",
            str(repo_root / ".venv311/bin/python"),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    assert output.exists()
    assert logs_dir.is_dir()


def test_review_scheduler_archives_sanitized_failure_when_morning_archive_linkage_fails(
    tmp_path,
    monkeypatch,
    capsys,
):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    output_dir = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: output_dir)
    config_path = config_dir / "advisor.yaml"
    config_path.write_text(
        """
market:
  primary: A股
schedule:
  premarket_time: "08:30"
  review_time: "22:30"
storage:
  database: data/advisor/operational.sqlite
data_sources:
  allow_tushare: false
  free_sources: []
""".lstrip(),
        encoding="utf-8",
    )
    allowed_rids = config_dir / "allowed-rids.yaml"
    allowed_rids.write_text("allowed_rids: []\n", encoding="utf-8")
    as_of = datetime(2026, 7, 12, 22, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    snapshot = CollectorSnapshot(
        events=(),
        quality=QualityResult("collector_state", "blocking", True, "collector ready"),
        as_of=as_of,
        allowed_rids=(),
    )
    monkeypatch.setattr(
        scheduler_review,
        "read_collector_snapshot",
        lambda *_args, **_kwargs: snapshot,
    )
    monkeypatch.setattr(
        scheduler_review,
        "_expanded_candidate_codes",
        lambda *_args, **_kwargs: ("600519",),
    )
    monkeypatch.setattr(
        scheduler_review.ConfiguredProviderRegistry,
        "from_yaml",
        lambda *_args, **_kwargs: SimpleNamespace(historical_provider=object()),
    )
    monkeypatch.setattr(
        scheduler_review,
        "update_market_database",
        lambda *_args, **_kwargs: None,
    )

    def fail_missing_archive(**_kwargs):
        raise ValueError("premarket archive not found token=secret raw-evidence")

    exit_code = scheduler_review.main(
        [
            "--config",
            str(config_path),
            "--allowed-rids",
            str(allowed_rids),
            "--events-db",
            str(tmp_path / "events.sqlite"),
            "--output-dir",
            str(output_dir),
            "--date",
            "2026-07-12",
            "--run-id",
            "review-runtime",
        ],
        coordinator=fail_missing_archive,
    )

    payload = json.loads(capsys.readouterr().out)
    archive_payload = json.loads(Path(payload["json_path"]).read_text(encoding="utf-8"))
    combined = json.dumps(payload) + json.dumps(archive_payload)
    assert exit_code == 1
    assert payload["status"] == "failed"
    assert payload["error"] == "ValueError"
    assert archive_payload["report_type"] == "failure"
    assert archive_payload["attempted_run_type"] == "review"
    assert "premarket archive not found" not in combined
    assert "token=secret" not in combined
    assert "raw-evidence" not in combined


def test_premarket_scheduler_archives_sanitized_failure_when_calendar_unsupported(
    tmp_path,
    monkeypatch,
    capsys,
):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    output_dir = tmp_path / "reports"
    monkeypatch.setattr(advisor_paths, "reports_dir", lambda: output_dir)
    config_path = config_dir / "advisor.yaml"
    config_path.write_text(
        """
market:
  primary: A股
schedule:
  premarket_time: "08:30"
  review_time: "22:30"
storage:
  database: data/advisor/operational.sqlite
data_sources:
  allow_tushare: false
  free_sources: []
""".lstrip(),
        encoding="utf-8",
    )
    allowed_rids = config_dir / "allowed-rids.yaml"
    allowed_rids.write_text("allowed_rids: []\n", encoding="utf-8")
    as_of = datetime(2027, 1, 4, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    snapshot = CollectorSnapshot(
        events=(),
        quality=QualityResult("collector_state", "blocking", True, "collector ready"),
        as_of=as_of,
        allowed_rids=(),
    )
    monkeypatch.setattr(
        scheduler_premarket,
        "read_collector_snapshot",
        lambda *_args, **_kwargs: snapshot,
    )
    monkeypatch.setattr(
        scheduler_premarket,
        "_expanded_candidate_codes",
        lambda *_args, **_kwargs: ("600519",),
    )

    exit_code = scheduler_premarket.main(
        [
            "--config",
            str(config_path),
            "--allowed-rids",
            str(allowed_rids),
            "--events-db",
            str(tmp_path / "events.sqlite"),
            "--output-dir",
            str(output_dir),
            "--as-of",
            as_of.isoformat(),
            "--date",
            "2027-01-04",
            "--run-id",
            "premarket-runtime",
        ],
        coordinator=lambda **_kwargs: None,
    )

    payload = json.loads(capsys.readouterr().out)
    archive_payload = json.loads(Path(payload["json_path"]).read_text(encoding="utf-8"))
    combined = json.dumps(payload) + json.dumps(archive_payload)
    assert exit_code == 1
    assert payload["status"] == "failed"
    assert payload["error"] == "UnsupportedTradingCalendarError"
    assert archive_payload["report_type"] == "failure"
    assert archive_payload["attempted_run_type"] == "premarket"
    assert "unsupported A-share trading calendar range" not in combined
