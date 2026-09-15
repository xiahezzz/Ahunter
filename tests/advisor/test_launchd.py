import json
import plistlib
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from advisor import paths as advisor_paths
from advisor.evidence.mx_adapter import CollectorSnapshot
from advisor.quality import QualityResult
from advisor.scheduler import launchd
from advisor.scheduler import premarket as scheduler_premarket
from advisor.scheduler import review as scheduler_review
from advisor.scheduler.launchd import main, render_launchd_template, validate_launchd_template


LAUNCHD_DIR = Path("config/launchd")


def _copy_launchd_templates(project_root: Path) -> None:
    template_dir = project_root / "config" / "launchd"
    template_dir.mkdir(parents=True)
    for template_name in launchd.LAUNCHD_TEMPLATE_NAMES:
        (template_dir / template_name).write_text(
            (LAUNCHD_DIR / template_name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )


def test_launchd_templates_are_valid():
    for name in [
        "com.ahunter.advisor-api.plist.template",
        "com.ahunter.advisor-premarket.plist.template",
        "com.ahunter.advisor-review.plist.template",
        "com.ahunter.advisor-frontend.plist.template",
        "com.ahunter.mx-listener.plist.template",
        "com.ahunter.research.plist.template",
    ]:
        assert validate_launchd_template(LAUNCHD_DIR / name)


def test_frontend_launchd_template_uses_node24_npm_cli_and_dev_server():
    template = LAUNCHD_DIR / "com.ahunter.advisor-frontend.plist.template"

    assert validate_launchd_template(template)

    rendered = render_launchd_template(
        template,
        repo_root=Path("/repo"),
        python=Path("/repo/.venv311/bin/python"),
    )
    payload = plistlib.loads(rendered.encode("utf-8"))

    assert payload["Label"] == "com.ahunter.advisor-frontend"
    assert payload["WorkingDirectory"] == "/repo"
    assert payload["KeepAlive"] is True
    assert payload["ProgramArguments"] == [
        "/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node",
        "/Users/mac/.local/share/chrome-devtools-mcp/node/lib/node_modules/npm/bin/npm-cli.js",
        "--prefix",
        "/repo/frontend",
        "run",
        "dev",
    ]
    assert payload["EnvironmentVariables"]["PATH"].split(":")[0] == (
        "/Users/mac/.local/share/chrome-devtools-mcp/node/bin"
    )
    assert "127.0.0.1" in Path("frontend/package.json").read_text(encoding="utf-8")
    assert "5173" in Path("frontend/package.json").read_text(encoding="utf-8")
    assert payload["StandardOutPath"] == "/repo/logs/advisor-frontend.out.log"
    assert payload["StandardErrorPath"] == "/repo/logs/advisor-frontend.err.log"


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


def test_launchd_render_preserves_explicit_python_path(tmp_path):
    output = tmp_path / "Library" / "LaunchAgents" / "advisor.plist"
    repo_root = tmp_path / "repo"
    python_target = tmp_path / "base-python"
    python_target.write_text("", encoding="utf-8")
    explicit_python = repo_root / ".venv311" / "bin" / "python"
    explicit_python.parent.mkdir(parents=True)
    explicit_python.symlink_to(python_target)

    exit_code = main(
        [
            str(LAUNCHD_DIR / "com.ahunter.advisor-api.plist.template"),
            "--repo-root",
            str(repo_root),
            "--python",
            str(explicit_python),
            "--output",
            str(output),
        ]
    )

    payload = plistlib.loads(output.read_bytes())
    assert exit_code == 0
    assert payload["ProgramArguments"][0] == str(explicit_python)


def test_launchd_render_defaults_runtime_python_under_repo_root_outside_repo_cwd(tmp_path, monkeypatch):
    output = tmp_path / "Library" / "LaunchAgents" / "advisor.plist"
    repo_root = tmp_path / "repo"
    _copy_launchd_templates(repo_root)
    template = repo_root / "config" / "launchd" / "com.ahunter.advisor-api.plist.template"
    outside_cwd = tmp_path / "outside"
    outside_cwd.mkdir()
    monkeypatch.chdir(outside_cwd)

    exit_code = main(
        [
            str(template),
            "--repo-root",
            str(repo_root),
            "--output",
            str(output),
        ]
    )

    payload = plistlib.loads(output.read_bytes())
    assert exit_code == 0
    assert payload["ProgramArguments"][0] == str(repo_root.resolve() / ".venv-runtime/bin/python")


def test_launchd_manage_install_writes_all_plists_and_preserves_python_path(tmp_path, capsys):
    launch_agents_dir = tmp_path / "Library" / "LaunchAgents"
    repo_root = tmp_path / "repo"
    _copy_launchd_templates(repo_root)
    logs_dir = repo_root / "logs"
    python_target = tmp_path / "base-python"
    python_target.write_text("", encoding="utf-8")
    explicit_python = repo_root / ".venv311" / "bin" / "python"
    explicit_python.parent.mkdir(parents=True)
    explicit_python.symlink_to(python_target)

    exit_code = launchd.manage_main(
        [
            "install",
            "--repo-root",
            str(repo_root),
            "--python",
            str(explicit_python),
            "--launch-agents-dir",
            str(launch_agents_dir),
        ]
    )

    installed = sorted(launch_agents_dir.glob("*.plist"))
    assert exit_code == 0
    assert [path.name for path in installed] == [
        "com.ahunter.advisor-api.plist",
        "com.ahunter.advisor-frontend.plist",
        "com.ahunter.advisor-premarket.plist",
        "com.ahunter.advisor-review.plist",
        "com.ahunter.research.plist",
    ]
    assert logs_dir.is_dir()
    for plist_path in installed:
        payload = plistlib.loads(plist_path.read_bytes())
        if payload["Label"] == "com.ahunter.advisor-frontend":
            assert payload["ProgramArguments"][0] == "/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node"
        else:
            assert payload["ProgramArguments"][0] == str(explicit_python)
    output = capsys.readouterr().out
    for plist_path in installed:
        assert str(plist_path) in output


def test_launchd_manage_install_writes_no_plists_when_any_template_is_invalid(tmp_path):
    launch_agents_dir = tmp_path / "Library" / "LaunchAgents"
    repo_root = tmp_path / "repo"
    _copy_launchd_templates(repo_root)
    invalid_template = repo_root / "config" / "launchd" / "com.ahunter.advisor-review.plist.template"
    invalid_template.write_text("<plist><dict></dict></plist>", encoding="utf-8")

    try:
        launchd.install_launch_agents(
            repo_root=repo_root,
            launch_agents_dir=launch_agents_dir,
        )
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("install should fail when any launchd template is invalid")

    assert not list(launch_agents_dir.glob("*.plist"))


def test_launchd_manage_install_uses_repo_root_templates_outside_repo_cwd(tmp_path, monkeypatch):
    project_root = tmp_path / "repo"
    _copy_launchd_templates(project_root)
    launch_agents_dir = tmp_path / "Library" / "LaunchAgents"
    outside_cwd = tmp_path / "outside"
    outside_cwd.mkdir()
    python_target = tmp_path / "base-python"
    python_target.write_text("", encoding="utf-8")
    explicit_python = project_root / ".venv311" / "bin" / "python"
    explicit_python.parent.mkdir(parents=True)
    explicit_python.symlink_to(python_target)
    monkeypatch.chdir(outside_cwd)

    exit_code = launchd.manage_main(
        [
            "install",
            "--repo-root",
            str(project_root),
            "--python",
            str(explicit_python),
            "--launch-agents-dir",
            str(launch_agents_dir),
        ]
    )

    installed = sorted(launch_agents_dir.glob("*.plist"))
    assert exit_code == 0
    assert [path.name for path in installed] == [
        "com.ahunter.advisor-api.plist",
        "com.ahunter.advisor-frontend.plist",
        "com.ahunter.advisor-premarket.plist",
        "com.ahunter.advisor-review.plist",
        "com.ahunter.research.plist",
    ]
    for plist_path in installed:
        payload = plistlib.loads(plist_path.read_bytes())
        if payload["Label"] == "com.ahunter.advisor-frontend":
            assert payload["ProgramArguments"][0] == "/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node"
        else:
            assert payload["ProgramArguments"][0] == str(explicit_python)


def test_launchd_manage_install_defaults_python_under_repo_root_outside_repo_cwd(tmp_path, monkeypatch):
    project_root = tmp_path / "repo"
    _copy_launchd_templates(project_root)
    launch_agents_dir = tmp_path / "Library" / "LaunchAgents"
    outside_cwd = tmp_path / "outside"
    outside_cwd.mkdir()
    monkeypatch.chdir(outside_cwd)

    exit_code = launchd.manage_main(
        [
            "install",
            "--repo-root",
            str(project_root),
            "--launch-agents-dir",
            str(launch_agents_dir),
        ]
    )

    installed = sorted(launch_agents_dir.glob("*.plist"))
    assert exit_code == 0
    for plist_path in installed:
        payload = plistlib.loads(plist_path.read_bytes())
        if payload["Label"] == "com.ahunter.advisor-frontend":
            assert payload["ProgramArguments"][0] == "/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node"
        else:
            assert payload["ProgramArguments"][0] == str(project_root.resolve() / ".venv-runtime/bin/python")


def test_launchd_manage_install_does_not_load_without_load_flag(tmp_path, monkeypatch):
    project_root = tmp_path / "repo"
    _copy_launchd_templates(project_root)
    launch_agents_dir = tmp_path / "Library" / "LaunchAgents"
    load_calls = []

    def fake_load_launch_agents(plists):
        load_calls.append(list(plists))
        raise AssertionError("load helper should not be called without --load")

    monkeypatch.setattr(launchd, "load_launch_agents", fake_load_launch_agents)

    exit_code = launchd.manage_main(
        [
            "install",
            "--repo-root",
            str(project_root),
            "--launch-agents-dir",
            str(launch_agents_dir),
        ]
    )

    assert exit_code == 0
    assert load_calls == []


def test_launchd_manage_install_load_dispatches_installed_plists(tmp_path, monkeypatch):
    project_root = tmp_path / "repo"
    _copy_launchd_templates(project_root)
    launch_agents_dir = tmp_path / "Library" / "LaunchAgents"
    load_calls = []

    monkeypatch.setattr(launchd, "load_launch_agents", lambda plists: load_calls.append(list(plists)))

    exit_code = launchd.manage_main(
        [
            "install",
            "--repo-root",
            str(project_root),
            "--launch-agents-dir",
            str(launch_agents_dir),
            "--load",
        ]
    )

    expected = [
        launch_agents_dir / "com.ahunter.advisor-api.plist",
        launch_agents_dir / "com.ahunter.advisor-frontend.plist",
        launch_agents_dir / "com.ahunter.advisor-premarket.plist",
        launch_agents_dir / "com.ahunter.advisor-review.plist",
        launch_agents_dir / "com.ahunter.research.plist",
    ]
    assert exit_code == 0
    assert load_calls == [expected]


def test_launchd_load_bootstraps_each_plist_with_injected_runner(tmp_path, monkeypatch):
    calls = []
    plists = [tmp_path / "one.plist", tmp_path / "two.plist"]
    for plist_path in plists:
        plist_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(launchd.os, "getuid", lambda: 501)

    launchd.load_launch_agents(plists, runner=lambda command: calls.append(command))

    assert calls == [
        ["launchctl", "bootstrap", "gui/501", str(plists[0])],
        ["launchctl", "bootstrap", "gui/501", str(plists[1])],
    ]
