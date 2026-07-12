from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def data_dir() -> Path:
    return repo_root() / "data"


def reports_dir() -> Path:
    return repo_root() / "reports"


def advisor_data_dir() -> Path:
    return data_dir() / "advisor"
