from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import os
from pathlib import Path
import re
import tempfile

from advisor.quality import QualityResult


_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


@dataclass(frozen=True)
class AdviceItem:
    advice_id: str
    code: str
    action: str
    confidence: float
    rationale: str
    evidence_ids: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ReviewItem:
    review_id: str
    advice_id: str
    outcome: str
    review_text: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ReportPaths:
    markdown_path: Path
    json_path: Path


def normalize_quality_results(
    quality_results: Sequence[QualityResult] | None,
) -> tuple[str, list[dict]]:
    if not quality_results or not all(isinstance(result, QualityResult) for result in quality_results):
        results = [
            QualityResult(
                check_name="quality_input",
                severity="blocking",
                passed=False,
                details="missing or ambiguous quality input",
            )
        ]
    else:
        results = list(quality_results)
    return (
        "passed" if all(result.passed for result in results) else "blocked",
        [asdict(result) for result in results],
    )


def normalize_context(context: Mapping[str, object] | None) -> dict[str, list[str]]:
    if context is None:
        return {}
    if not isinstance(context, Mapping):
        raise ValueError("context must be a mapping")

    normalized: dict[str, list[str]] = {}
    for key, value in context.items():
        if not isinstance(key, str):
            raise ValueError("context keys must be strings")
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, Sequence) or isinstance(values, (bytes, bytearray)):
            raise ValueError(f"context value for {key} must be a string or sequence of strings")
        if not all(isinstance(item, str) for item in values):
            raise ValueError(f"context value for {key} must contain only strings")
        normalized[key] = list(values)
    return normalized


def validate_unique_ids(items: Sequence[AdviceItem] | Sequence[ReviewItem], field_name: str) -> None:
    seen: set[str] = set()
    for item in items:
        item_id = getattr(item, field_name)
        if item_id in seen:
            raise ValueError(f"duplicate {field_name}: {item_id}")
        seen.add(item_id)


def report_paths(
    output_dir: Path,
    report_type: str,
    *,
    run_id: str | None,
    rerun_reason: str | None,
    supersedes: str | None,
) -> tuple[ReportPaths, str, dict | None]:
    if run_id is None:
        if rerun_reason is not None or supersedes is not None:
            raise ValueError("rerun metadata requires a run_id")
        suffix = ""
        resolved_run_id = "initial"
        supersession = None
    else:
        _validate_run_id(run_id)
        if not rerun_reason or not supersedes:
            raise ValueError("versioned reruns require rerun_reason and supersedes")
        _validate_run_id(supersedes)
        if run_id == supersedes:
            raise ValueError("run_id must differ from supersedes")
        suffix = f".{run_id}"
        resolved_run_id = run_id
        supersession = {"reason": rerun_reason, "supersedes": supersedes}
    return (
        ReportPaths(
            markdown_path=output_dir / f"{report_type}{suffix}.md",
            json_path=output_dir / f"{report_type}{suffix}.json",
        ),
        resolved_run_id,
        supersession,
    )


def ensure_archive_is_new(paths: ReportPaths) -> None:
    if paths.markdown_path.exists() or paths.json_path.exists():
        raise FileExistsError("report archive already exists")


def atomic_write_pair(paths: ReportPaths, markdown_content: str, json_content: str) -> None:
    if paths.markdown_path.parent != paths.json_path.parent:
        raise ValueError("report files must share an output directory")

    output_dir = paths.markdown_path.parent
    markdown_temp: Path | None = None
    json_temp: Path | None = None
    markdown_replaced = False
    try:
        markdown_temp = _write_temporary_file(output_dir, markdown_content)
        json_temp = _write_temporary_file(output_dir, json_content)
        os.replace(markdown_temp, paths.markdown_path)
        markdown_temp = None
        markdown_replaced = True
        os.replace(json_temp, paths.json_path)
        json_temp = None
    except Exception:
        if markdown_replaced:
            paths.markdown_path.unlink(missing_ok=True)
        raise
    finally:
        for temporary_path in (markdown_temp, json_temp):
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def _write_temporary_file(output_dir: Path, content: str) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".report-", suffix=".tmp", dir=output_dir)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def _validate_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError("unsafe run_id")
