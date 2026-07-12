import argparse
import os
import plistlib
import subprocess
from pathlib import Path
from typing import Callable, Sequence


REPO_ROOT_PLACEHOLDER = "{{REPO_ROOT}}"
PYTHON_PLACEHOLDER = "{{PYTHON}}"
NODE24_BIN = "/Users/mac/.local/share/chrome-devtools-mcp/node/bin"
NODE24 = f"{NODE24_BIN}/node"
NPM_CLI = "/Users/mac/.local/share/chrome-devtools-mcp/node/lib/node_modules/npm/bin/npm-cli.js"

REQUIRED_KEYS = {
    "Label",
    "ProgramArguments",
    "WorkingDirectory",
    "StandardOutPath",
    "StandardErrorPath",
}

LAUNCHD_TEMPLATE_NAMES = (
    "com.ahunter.advisor-api.plist.template",
    "com.ahunter.advisor-frontend.plist.template",
    "com.ahunter.advisor-premarket.plist.template",
    "com.ahunter.advisor-review.plist.template",
)


def validate_launchd_template(path: Path) -> bool:
    try:
        payload = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return False
    if not isinstance(payload, dict) or not REQUIRED_KEYS.issubset(payload):
        return False
    if not _has_valid_common_fields(payload):
        return False
    label = payload["Label"]
    args = payload["ProgramArguments"]
    if label == "com.ahunter.advisor-api":
        return _valid_api_payload(payload, args)
    if label == "com.ahunter.advisor-frontend":
        return _valid_frontend_payload(payload, args)
    if label == "com.ahunter.advisor-premarket":
        return _valid_report_payload(payload, args, "advisor.scheduler.premarket", 8, 30)
    if label == "com.ahunter.advisor-review":
        return _valid_report_payload(payload, args, "advisor.scheduler.review", 22, 30)
    return False


def render_launchd_template(path: Path, *, repo_root: Path, python: Path) -> str:
    content = path.read_text(encoding="utf-8")
    return (
        content.replace(REPO_ROOT_PLACEHOLDER, str(repo_root))
        .replace(PYTHON_PLACEHOLDER, str(python))
    )


def _has_valid_common_fields(payload: dict) -> bool:
    if any(_contains_shell_interpolation(value) for value in _walk_values(payload)):
        return False
    if not all(isinstance(payload[key], str) and payload[key] for key in REQUIRED_KEYS - {"ProgramArguments"}):
        return False
    args = payload["ProgramArguments"]
    return isinstance(args, list) and bool(args) and all(isinstance(item, str) and item for item in args)


def _valid_api_payload(payload: dict, args: list[str]) -> bool:
    return (
        payload.get("KeepAlive") is True
        and args[:3] == [PYTHON_PLACEHOLDER, "-m", "uvicorn"]
        and "advisor.web.api:app" in args
        and "--host" in args
        and "127.0.0.1" in args
        and "--port" in args
    )


def _valid_frontend_payload(payload: dict, args: list[str]) -> bool:
    return (
        payload.get("KeepAlive") is True
        and payload.get("WorkingDirectory") == REPO_ROOT_PLACEHOLDER
        and payload.get("StandardOutPath") == f"{REPO_ROOT_PLACEHOLDER}/logs/advisor-frontend.out.log"
        and payload.get("StandardErrorPath") == f"{REPO_ROOT_PLACEHOLDER}/logs/advisor-frontend.err.log"
        and _has_frontend_node24_path(payload)
        and args
        == [
            NODE24,
            NPM_CLI,
            "--prefix",
            f"{REPO_ROOT_PLACEHOLDER}/frontend",
            "run",
            "dev",
        ]
    )


def _has_frontend_node24_path(payload: dict) -> bool:
    environment = payload.get("EnvironmentVariables")
    if not isinstance(environment, dict):
        return False
    path = environment.get("PATH")
    return isinstance(path, str) and path.split(":")[0] == NODE24_BIN


def _valid_report_payload(payload: dict, args: list[str], module: str, hour: int, minute: int) -> bool:
    return (
        args[:3] == [PYTHON_PLACEHOLDER, "-m", module]
        and "--output-dir" in args
        and "reports" in args
        and payload.get("StartCalendarInterval") == {"Hour": hour, "Minute": minute}
    )


def _contains_shell_interpolation(value: object) -> bool:
    return isinstance(value, str) and "$" in value


def _walk_values(value: object):
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_values(item)
    else:
        yield value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate or render A Hunter advisor launchd templates.")
    parser.add_argument("template", type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--python", type=Path, default=Path(".venv311/bin/python"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not validate_launchd_template(args.template):
        raise SystemExit(1)
    if args.output is not None:
        rendered = render_launchd_template(
            args.template,
            repo_root=args.repo_root.resolve(),
            python=_absolute_without_symlink_resolution(args.python),
        )
        _ensure_render_directories(rendered, args.output)
        args.output.write_text(rendered, encoding="utf-8")
    print(f"valid launchd template: {args.template}")
    return 0


def manage_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install or load A Hunter advisor launchd agents.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    install_parser = subparsers.add_parser("install", help="Render advisor launchd templates into LaunchAgents.")
    install_parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    install_parser.add_argument("--python", type=Path)
    install_parser.add_argument("--launch-agents-dir", type=Path, default=Path.home() / "Library" / "LaunchAgents")
    install_parser.add_argument("--load", action="store_true", help="Load installed agents with launchctl bootstrap.")
    args = parser.parse_args(argv)
    if args.command == "install":
        installed = install_launch_agents(
            repo_root=args.repo_root,
            python=args.python,
            launch_agents_dir=args.launch_agents_dir,
            load=args.load,
        )
        for path in installed:
            print(path)
    return 0


def install_launch_agents(
    *,
    repo_root: Path,
    python: Path | None = None,
    launch_agents_dir: Path,
    load: bool = False,
) -> list[Path]:
    rendered_outputs = []
    resolved_repo_root = repo_root.resolve()
    python_path = (
        resolved_repo_root / ".venv311/bin/python"
        if python is None
        else _absolute_without_symlink_resolution(python)
    )
    template_dir = resolved_repo_root / "config" / "launchd"
    for template_name in LAUNCHD_TEMPLATE_NAMES:
        template = template_dir / template_name
        if not validate_launchd_template(template):
            raise SystemExit(1)
        output = launch_agents_dir / template_name.removesuffix(".template")
        rendered = render_launchd_template(
            template,
            repo_root=resolved_repo_root,
            python=python_path,
        )
        rendered_outputs.append((output, rendered))

    installed = []
    for output, rendered in rendered_outputs:
        _ensure_render_directories(rendered, output)
        output.write_text(rendered, encoding="utf-8")
        installed.append(output)
    if load:
        load_launch_agents(installed)
    return installed


def load_launch_agents(
    plists: Sequence[Path],
    *,
    runner: Callable[[list[str]], object] | None = None,
) -> None:
    if runner is None:
        runner = _run_launchctl
    domain = f"gui/{os.getuid()}"
    for plist_path in plists:
        runner(["launchctl", "bootstrap", domain, str(plist_path)])


def _run_launchctl(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True)


def _absolute_without_symlink_resolution(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _ensure_render_directories(rendered: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = plistlib.loads(rendered.encode("utf-8"))
    for key in ("StandardOutPath", "StandardErrorPath"):
        path = payload.get(key)
        if isinstance(path, str) and path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
