from pathlib import Path

from advisor.paths import advisor_data_dir, data_dir, repo_root, reports_dir


def test_repo_root_points_to_project_root():
    root = repo_root()
    assert (root / "agents.md").exists()
    assert (root / "package.json").exists()


def test_standard_data_paths_are_under_repo_root():
    root = repo_root()
    assert data_dir() == root / "data"
    assert reports_dir() == root / "reports"
    assert advisor_data_dir() == root / "data" / "advisor"
    for path in [data_dir(), reports_dir(), advisor_data_dir()]:
        assert isinstance(path, Path)
        assert str(path).startswith(str(root))
