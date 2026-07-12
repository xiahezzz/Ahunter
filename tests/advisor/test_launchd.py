import subprocess
import sys
from pathlib import Path

from advisor.scheduler.launchd import validate_launchd_template


LAUNCHD_DIR = Path("config/launchd")


def test_launchd_templates_are_valid():
    for name in [
        "com.ahunter.advisor-api.plist.template",
        "com.ahunter.advisor-premarket.plist.template",
        "com.ahunter.advisor-review.plist.template",
    ]:
        assert validate_launchd_template(LAUNCHD_DIR / name)


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
