from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
import json
import os
from pathlib import Path
import re
import tempfile

from advisor import paths as advisor_paths
from advisor.quality import QualityResult


_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_QUALITY_SEVERITIES = frozenset({"blocking", "warning", "info"})


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
    if not quality_results or not all(_valid_quality_result(result) for result in quality_results):
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
        "blocked" if any(result.blocking_failure for result in results) else "passed",
        [asdict(result) for result in results],
    )


def _valid_quality_result(result: object) -> bool:
    return (
        isinstance(result, QualityResult)
        and isinstance(result.check_name, str)
        and bool(result.check_name.strip())
        and isinstance(result.severity, str)
        and result.severity in _QUALITY_SEVERITIES
        and type(result.passed) is bool
        and isinstance(result.details, str)
        and bool(result.details.strip())
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
    report_date: str,
    report_type: str,
    *,
    run_id: str | None,
    rerun_reason: str | None,
    supersedes: str | None,
) -> tuple[ReportPaths, str, dict | None]:
    report_directory = _resolve_report_directory(output_dir, report_date)
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
        _validate_predecessor_archive(
            report_directory,
            report_type,
            report_date,
            supersedes,
        )
    return (
        ReportPaths(
            markdown_path=report_directory / f"{report_type}{suffix}.md",
            json_path=report_directory / f"{report_type}{suffix}.json",
        ),
        resolved_run_id,
        supersession,
    )


def ensure_archive_is_new(paths: ReportPaths) -> None:
    if paths.markdown_path.is_symlink() or paths.json_path.is_symlink():
        raise ValueError("symlinked report path")
    if paths.markdown_path.exists() or paths.json_path.exists():
        raise FileExistsError("report archive already exists")


def load_premarket_link(
    report_directory: Path,
    report_date: str,
    premarket_run_id: str,
    morning_advice: Sequence[AdviceItem],
) -> dict:
    _validate_run_id(premarket_run_id)
    suffix = "" if premarket_run_id == "initial" else f".{premarket_run_id}"
    archive_path = report_directory / f"premarket{suffix}.json"
    if archive_path.is_symlink():
        raise ValueError("symlinked premarket archive")
    if not archive_path.is_file():
        raise ValueError("premarket archive not found")
    try:
        payload = json.loads(archive_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid premarket archive") from error

    expected_advice = [item.to_dict() for item in morning_advice]
    expected_ids = [item.advice_id for item in morning_advice]
    expected_metadata = {
        "report_type": "premarket",
        "report_date": report_date,
        "report_time": "08:30",
        "run_id": premarket_run_id,
        "quality_status": "passed",
    }
    if (
        not isinstance(payload, dict)
        or any(payload.get(key) != value for key, value in expected_metadata.items())
        or payload.get("advice_ids") != expected_ids
        or payload.get("advice") != expected_advice
    ):
        if isinstance(payload, dict) and (
            payload.get("advice_ids") != expected_ids or payload.get("advice") != expected_advice
        ):
            raise ValueError("morning advice does not match premarket archive")
        raise ValueError("invalid premarket archive")
    return {
        "json_file": archive_path.name,
        "report_date": report_date,
        "report_time": "08:30",
        "report_type": "premarket",
        "run_id": premarket_run_id,
    }


def atomic_write_pair(paths: ReportPaths, markdown_content: str, json_content: str) -> None:
    if paths.markdown_path.parent != paths.json_path.parent:
        raise ValueError("report files must share an output directory")

    output_dir = paths.markdown_path.parent
    claim_path = output_dir / f".{paths.markdown_path.stem}.lock"
    claim_identity = _create_publication_claim(claim_path)
    markdown_temp: Path | None = None
    json_temp: Path | None = None
    markdown_identity: tuple[int, int] | None = None
    json_identity: tuple[int, int] | None = None
    markdown_published = False
    json_published = False
    try:
        markdown_temp = _write_temporary_file(output_dir, markdown_content)
        json_temp = _write_temporary_file(output_dir, json_content)
        markdown_identity = _path_identity(markdown_temp)
        json_identity = _path_identity(json_temp)
        os.link(markdown_temp, paths.markdown_path)
        markdown_published = True
        os.link(json_temp, paths.json_path)
        json_published = True
        _fsync_directory(output_dir)
    except Exception:
        if json_published and json_identity is not None:
            _unlink_if_identity(paths.json_path, json_identity)
        if markdown_published and markdown_identity is not None:
            _unlink_if_identity(paths.markdown_path, markdown_identity)
        _fsync_directory(output_dir)
        raise
    finally:
        for temporary_path in (markdown_temp, json_temp):
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        _unlink_if_identity(claim_path, claim_identity)


def _create_publication_claim(claim_path: Path) -> tuple[int, int]:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(claim_path, flags, 0o600)
    except FileExistsError as error:
        raise FileExistsError("report publication already claimed") from error
    try:
        stat_result = os.fstat(descriptor)
        os.fsync(descriptor)
        return stat_result.st_dev, stat_result.st_ino
    finally:
        os.close(descriptor)


def _path_identity(path: Path) -> tuple[int, int]:
    stat_result = path.stat(follow_symlinks=False)
    return stat_result.st_dev, stat_result.st_ino


def _unlink_if_identity(path: Path, expected_identity: tuple[int, int]) -> None:
    try:
        if _path_identity(path) == expected_identity:
            path.unlink()
    except FileNotFoundError:
        return


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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


def validate_run_id(run_id: str) -> None:
    _validate_run_id(run_id)


def _validate_predecessor_archive(
    report_directory: Path,
    report_type: str,
    report_date: str,
    predecessor_run_id: str,
) -> None:
    suffix = "" if predecessor_run_id == "initial" else f".{predecessor_run_id}"
    predecessor_path = report_directory / f"{report_type}{suffix}.json"
    if predecessor_path.is_symlink():
        raise ValueError("symlinked predecessor archive")
    if not predecessor_path.is_file():
        raise ValueError("predecessor archive not found")
    try:
        payload = json.loads(predecessor_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid predecessor archive") from error
    if not isinstance(payload, dict) or any(
        payload.get(key) != value
        for key, value in {
            "report_type": report_type,
            "report_date": report_date,
            "run_id": predecessor_run_id,
        }.items()
    ):
        raise ValueError("invalid predecessor archive")


def _resolve_report_directory(output_dir: Path, report_date: str) -> Path:
    try:
        parsed_date = date.fromisoformat(report_date)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid report_date") from error
    if parsed_date.isoformat() != report_date:
        raise ValueError("invalid report_date")

    supplied_root = Path(output_dir)
    if ".." in supplied_root.parts:
        raise ValueError("unsafe output_dir")
    configured_root = Path(advisor_paths.reports_dir())
    supplied_absolute = Path(os.path.abspath(supplied_root))
    configured_absolute = Path(os.path.abspath(configured_root))
    if supplied_absolute != configured_absolute:
        raise ValueError("output_dir must equal configured reports root")
    if _has_symlink_component(configured_absolute):
        raise ValueError("symlinked reports root")

    configured_absolute.mkdir(parents=True, exist_ok=True)
    if _has_symlink_component(configured_absolute):
        raise ValueError("symlinked reports root")
    report_directory = configured_absolute / report_date
    if report_directory.is_symlink():
        raise ValueError("symlinked report date path")
    report_directory.mkdir(exist_ok=True)
    if report_directory.is_symlink():
        raise ValueError("symlinked report date path")
    return report_directory


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
    return False
