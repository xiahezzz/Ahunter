import subprocess
import sys
from pathlib import Path

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
