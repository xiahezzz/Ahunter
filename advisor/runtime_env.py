"""Bootstrap the self-contained A Hunter runtime."""

from __future__ import annotations

import argparse
import subprocess
import venv
from pathlib import Path
from typing import Callable, Sequence


RUNTIME_ENV_NAME = ".venv-runtime"
CommandRunner = Callable[[list[str]], None]
RUNTIME_SMOKE_CHECK = "from advisor.research.catalog import load_catalog; print(len(load_catalog().agents))"


def bootstrap_runtime(
    *,
    repo_root: Path,
    runtime_dir: Path | None = None,
    runner: CommandRunner | None = None,
) -> Path:
    resolved_root = _require_python_project(repo_root)
    resolved_runtime = (resolved_root / RUNTIME_ENV_NAME).resolve() if runtime_dir is None else runtime_dir.expanduser().resolve()
    runtime_python = resolved_runtime / "bin" / "python"
    if not runtime_python.is_file():
        venv.EnvBuilder(with_pip=True).create(resolved_runtime)
    if not runtime_python.is_file():
        raise RuntimeError(f"runtime Python was not created at {runtime_python}")
    run = runner or _run_checked
    run([str(runtime_python), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(runtime_python), "-m", "pip", "install", "--editable", str(resolved_root)])
    run([str(runtime_python), "-m", "pip", "check"])
    run([str(runtime_python), "-c", RUNTIME_SMOKE_CHECK])
    return runtime_python


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the self-contained A Hunter runtime environment.")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--runtime-dir", type=Path)
    args = parser.parse_args(argv)
    runtime_python = bootstrap_runtime(repo_root=args.repo_root, runtime_dir=args.runtime_dir)
    print(f"runtime Python: {runtime_python}")
    return 0


def _require_python_project(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not (resolved / "pyproject.toml").is_file():
        raise FileNotFoundError(f"A Hunter project is unavailable at {resolved}")
    return resolved


def _run_checked(command: list[str]) -> None:
    subprocess.run(command, check=True)


if __name__ == "__main__":
    raise SystemExit(main())
