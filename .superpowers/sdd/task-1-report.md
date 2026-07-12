# Task 1 Report: Python Advisor Project Skeleton

## Outcome

Implemented the minimal Python advisor skeleton required for Task 1:

- `advisor.paths.repo_root()`
- `advisor.paths.data_dir()`
- `advisor.paths.reports_dir()`
- `advisor.paths.advisor_data_dir()`
- project metadata in `pyproject.toml`
- package version in `advisor/__init__.py`

## RED

Added `tests/advisor/test_paths.py` first, before any implementation.

Targeted command:

```bash
.venv311/bin/python -m pytest tests/advisor/test_paths.py -q
```

Observed failure:

```text
ModuleNotFoundError: No module named 'advisor'
```

That was the expected red state: the test failed during import because the package did not exist yet.

## GREEN

Added:

- `pyproject.toml`
- `advisor/__init__.py`
- `advisor/paths.py`

Re-ran the same targeted test:

```bash
.venv311/bin/python -m pytest tests/advisor/test_paths.py -q
```

Result:

```text
2 passed in 0.01s
```

## Changed Files

- `pyproject.toml`
- `advisor/__init__.py`
- `advisor/paths.py`
- `tests/advisor/test_paths.py`

## Tests Run

- `.venv311/bin/python -m pytest tests/advisor/test_paths.py -q`

## Self-Review

- The implementation stays inside the task scope and does not touch collector code.
- Path helpers are simple, deterministic, and rooted at the repository top level.
- `pyproject.toml` matches the brief, including pytest configuration and script entry points.
- No extra behavior was added beyond the task skeleton.
