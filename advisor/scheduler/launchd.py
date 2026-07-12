import argparse
import plistlib
from pathlib import Path


REPO_ROOT_PLACEHOLDER = "{{REPO_ROOT}}"
PYTHON_PLACEHOLDER = "{{PYTHON}}"

REQUIRED_KEYS = {
    "Label",
    "ProgramArguments",
    "WorkingDirectory",
    "StandardOutPath",
    "StandardErrorPath",
}


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
    if label == "com.ahunter.advisor-premarket":
        return _valid_report_payload(payload, args, "advisor.scheduler.premarket", 8, 30)
    if label == "com.ahunter.advisor-review":
        return _valid_report_payload(payload, args, "advisor.reporting.review", 22, 30)
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
            python=args.python.resolve(),
        )
        _ensure_render_directories(rendered, args.output)
        args.output.write_text(rendered, encoding="utf-8")
    print(f"valid launchd template: {args.template}")
    return 0


def _ensure_render_directories(rendered: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = plistlib.loads(rendered.encode("utf-8"))
    for key in ("StandardOutPath", "StandardErrorPath"):
        path = payload.get(key)
        if isinstance(path, str) and path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
