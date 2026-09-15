from pathlib import Path

from advisor.runtime_env import bootstrap_runtime


def test_bootstrap_runtime_installs_only_this_project(tmp_path: Path):
    repo_root = tmp_path / "a-hunter"
    runtime_dir = repo_root / ".venv-runtime"
    runtime_python = runtime_dir / "bin" / "python"
    repo_root.mkdir(parents=True)
    (repo_root / "pyproject.toml").write_text("[project]\nname = 'a-hunter'\n", encoding="utf-8")
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_text("", encoding="utf-8")
    calls = []

    result = bootstrap_runtime(repo_root=repo_root, runtime_dir=runtime_dir, runner=calls.append)

    assert result == runtime_python
    assert calls[0] == [str(runtime_python), "-m", "pip", "install", "--upgrade", "pip"]
    assert calls[1] == [str(runtime_python), "-m", "pip", "install", "--editable", str(repo_root.resolve())]
    assert calls[2] == [str(runtime_python), "-m", "pip", "check"]
    assert calls[3][:2] == [str(runtime_python), "-c"]


def test_bootstrap_runtime_rejects_missing_project(tmp_path: Path):
    try:
        bootstrap_runtime(repo_root=tmp_path / "missing", runtime_dir=tmp_path / ".venv-runtime", runner=lambda command: None)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("missing project should fail before creating an environment")
