from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from advisor import paths as advisor_paths
from advisor.quality import QualityResult


_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_REPORT_TYPES = frozenset({"premarket", "review", "failure"})
_MARKER_NAME_RE = re.compile(
    r"(?P<report_type>premarket|review|failure)(?:\.(?P<run_id>[A-Za-z0-9][A-Za-z0-9_-]{0,63}))?\.complete\.json\Z"
)
_MAX_REPORT_MARKER_BYTES = 64 * 1024
_MAX_REPORT_JSON_BYTES = 2 * 1024 * 1024
_MAX_REPORT_MARKDOWN_BYTES = 2 * 1024 * 1024
_MAX_ARCHIVE_CANDIDATES = 500
_QUALITY_SEVERITIES = frozenset({"blocking", "warning", "info"})
_NATIVE_DIR_FD_SUPPORT = all(
    function in os.supports_dir_fd for function in (os.open, os.mkdir, os.link, os.stat, os.unlink, os.rmdir)
)
_NATIVE_LINK_NOFOLLOW_SUPPORT = os.link in os.supports_follow_symlinks


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


def load_premarket_link(
    report_directory: Path,
    report_date: str,
    premarket_run_id: str,
    morning_advice: Sequence[AdviceItem],
) -> dict:
    _validate_run_id(premarket_run_id)
    names = _archive_names("premarket", premarket_run_id)
    root_fd, date_fd = _open_report_fds(report_directory.parent, report_date, create_date=False)
    try:
        if not _any_archive_entry(date_fd, names):
            raise ValueError("premarket archive not found")
        try:
            _validate_complete_archive_fd(date_fd, names, "premarket", report_date, premarket_run_id)
            payload = json.loads(_read_regular_bytes(date_fd, names["json"]).decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise ValueError("invalid premarket archive") from error
    finally:
        os.close(date_fd)
        os.close(root_fd)

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
        "json_file": names["json"],
        "report_date": report_date,
        "report_time": "08:30",
        "report_type": "premarket",
        "run_id": premarket_run_id,
    }


def atomic_write_pair(paths: ReportPaths, markdown_content: str, json_content: str) -> None:
    if paths.markdown_path.parent != paths.json_path.parent:
        raise ValueError("report files must share an output directory")

    payload = json.loads(json_content)
    report_type = payload["report_type"]
    report_date = payload["report_date"]
    run_id = payload["run_id"]
    names = _archive_names(report_type, run_id)
    report_directory = paths.markdown_path.parent
    if paths.markdown_path.name != names["markdown"] or paths.json_path.name != names["json"]:
        raise ValueError("report paths do not match report metadata")

    root_fd, date_fd = _open_report_fds(report_directory.parent, report_date, create_date=True)
    try:
        _publication_hook("date_fd_opened", report_date=report_date, date_fd=date_fd)
        _verify_date_binding(root_fd, date_fd, report_date)
        if _any_archive_entry(date_fd, names):
            if any(_entry_is_symlink(date_fd, names[key]) for key in ("markdown", "json", "marker")):
                raise ValueError("symlinked report path")
            try:
                _validate_complete_archive_fd(date_fd, names, report_type, report_date, run_id)
            except ValueError:
                if not _entry_exists(date_fd, names["claim"]):
                    raise ValueError("conflicting incomplete report archive")
            else:
                raise FileExistsError("report archive already exists")

        while True:
            transaction_id = uuid.uuid4().hex
            stage_name = f".txn-{paths.markdown_path.stem}-{transaction_id}"
            candidate_claim = {
                "pid": os.getpid(),
                "report_date": report_date,
                "report_type": report_type,
                "run_id": run_id,
                "stage": stage_name,
                "transaction_id": transaction_id,
            }
            claim_fd, existing_claim, claim = _acquire_claim(date_fd, names["claim"], candidate_claim)
            if not existing_claim:
                break
            try:
                if _recover_claim(date_fd, root_fd, report_date, names, report_type, run_id, claim):
                    return
            finally:
                _release_claim_lock(claim_fd)

        try:
            _publication_hook("claim_acquired", claim=claim, date_fd=date_fd)
            os.mkdir(stage_name, 0o700, dir_fd=date_fd)
            stage_fd = _open_directory(stage_name, dir_fd=date_fd)
            try:
                markdown_bytes = markdown_content.encode("utf-8")
                json_bytes = json_content.encode("utf-8")
                manifest_bytes = _completion_manifest(paths, markdown_bytes, json_bytes, transaction_id)
                _write_exclusive_file(stage_fd, names["markdown"], markdown_bytes)
                _write_exclusive_file(stage_fd, names["json"], json_bytes)
                _write_exclusive_file(stage_fd, names["marker"], manifest_bytes)
                os.fsync(stage_fd)
                os.fsync(date_fd)
                _publication_hook("stage_manifest_durable", stage=stage_name, stage_fd=stage_fd)
                _verify_date_binding(root_fd, date_fd, report_date)
                os.link(
                    names["markdown"],
                    names["markdown"],
                    src_dir_fd=stage_fd,
                    dst_dir_fd=date_fd,
                    follow_symlinks=False,
                )
                _publication_hook("after_markdown_link", stage=stage_name, date_fd=date_fd)
                os.link(
                    names["json"],
                    names["json"],
                    src_dir_fd=stage_fd,
                    dst_dir_fd=date_fd,
                    follow_symlinks=False,
                )
                _publication_hook("after_json_link", stage=stage_name, date_fd=date_fd)
                os.link(
                    names["marker"],
                    names["marker"],
                    src_dir_fd=stage_fd,
                    dst_dir_fd=date_fd,
                    follow_symlinks=False,
                )
                _publication_hook("after_marker_link", stage=stage_name, date_fd=date_fd)
                os.fsync(date_fd)
                _validate_complete_archive_fd(date_fd, names, report_type, report_date, run_id)
            finally:
                os.close(stage_fd)
            _unlink_hidden(date_fd, names["claim"])
            os.fsync(date_fd)
            _cleanup_hidden_stage(date_fd, stage_name, names)
        finally:
            _release_claim_lock(claim_fd)
    finally:
        os.close(date_fd)
        os.close(root_fd)


def _validate_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError("unsafe run_id")


def validate_run_id(run_id: str) -> None:
    _validate_run_id(run_id)


def read_verified_archive(
    output_dir: Path,
    report_date: str,
    report_type: str,
    run_id: str = "initial",
) -> dict:
    """Read an immutable report archive only after its completion marker verifies it."""
    _require_filesystem_capabilities()
    _validate_report_date(report_date)
    _validate_report_type(report_type)
    _validate_run_id(run_id)
    root = _validated_root_path(output_dir)
    root_fd, date_fd = _open_existing_report_fds(root, report_date)
    try:
        names = _archive_names(report_type, run_id)
        contents = _read_archive_contents(
            date_fd,
            names,
            marker_limit=_MAX_REPORT_MARKER_BYTES,
            json_limit=_MAX_REPORT_JSON_BYTES,
            markdown_limit=_MAX_REPORT_MARKDOWN_BYTES,
        )
        _reader_hook("buffers_captured", report_date=report_date, report_type=report_type, run_id=run_id)
        _validate_complete_archive_contents(contents, names, report_type, report_date, run_id)
        try:
            payload = json.loads(contents["json"].decode("utf-8"))
            markdown = contents["markdown"].decode("utf-8")
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid report archive") from error
    except (OSError, ValueError) as error:
        raise ValueError("invalid report archive") from error
    finally:
        os.close(date_fd)
        os.close(root_fd)

    if not isinstance(payload, dict):
        raise ValueError("invalid report archive")
    return {
        "report_date": report_date,
        "report_type": report_type,
        "run_id": run_id,
        "json": payload,
        "markdown": markdown,
    }


def list_verified_archives(output_dir: Path) -> list[dict]:
    """Return metadata for complete, immutable report archives without writing to disk."""
    _require_filesystem_capabilities()
    root = _validated_root_path(output_dir)
    root_fd = _open_existing_root_fd(root)
    try:
        report_dates = sorted(os.listdir(root_fd), reverse=True)
    finally:
        os.close(root_fd)

    archives: list[dict] = []
    candidate_count = 0
    for report_date in report_dates[:_MAX_ARCHIVE_CANDIDATES]:
        try:
            _validate_report_date(report_date)
        except ValueError:
            continue
        try:
            root_fd, date_fd = _open_existing_report_fds(root, report_date)
            try:
                markers = sorted(os.listdir(date_fd))
            finally:
                os.close(date_fd)
                os.close(root_fd)
        except (OSError, ValueError):
            continue
        for marker_name in markers[:_MAX_ARCHIVE_CANDIDATES]:
            match = _MARKER_NAME_RE.fullmatch(marker_name)
            if match is None:
                continue
            candidate_count += 1
            if candidate_count > _MAX_ARCHIVE_CANDIDATES:
                return _sort_archives(archives)
            run_id = match.group("run_id") or "initial"
            try:
                archive = read_verified_archive(root, report_date, match.group("report_type"), run_id)
            except ValueError:
                continue
            archives.append(
                {
                    "report_date": archive["report_date"],
                    "report_type": archive["report_type"],
                    "run_id": archive["run_id"],
                    "quality_status": archive["json"].get("quality_status", "unknown"),
                }
            )
    return _sort_archives(archives)


def _sort_archives(archives: list[dict]) -> list[dict]:
    return sorted(
        archives,
        key=lambda archive: (archive["report_date"], archive["report_type"], archive["run_id"]),
        reverse=True,
    )


def _validate_predecessor_archive(
    report_directory: Path,
    report_type: str,
    report_date: str,
    predecessor_run_id: str,
) -> None:
    names = _archive_names(report_type, predecessor_run_id)
    root_fd, date_fd = _open_report_fds(report_directory.parent, report_date, create_date=False)
    try:
        if not _any_archive_entry(date_fd, names):
            raise ValueError("predecessor archive not found")
        if any(_entry_is_symlink(date_fd, names[key]) for key in ("markdown", "json", "marker")):
            raise ValueError("symlinked predecessor archive")
        try:
            _validate_complete_archive_fd(date_fd, names, report_type, report_date, predecessor_run_id)
            payload = json.loads(_read_regular_bytes(date_fd, names["json"]).decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise ValueError("invalid predecessor archive") from error
    finally:
        os.close(date_fd)
        os.close(root_fd)
    if not isinstance(payload, dict) or any(
        payload.get(key) != value
        for key, value in {
            "report_type": report_type,
            "report_date": report_date,
            "run_id": predecessor_run_id,
        }.items()
    ):
        raise ValueError("invalid predecessor archive")


def _completion_manifest(
    paths: ReportPaths,
    markdown_content: bytes,
    json_content: bytes,
    transaction_id: str,
) -> bytes:
    payload = json.loads(json_content)
    marker = {
        "files": {
            "json": {
                "name": paths.json_path.name,
                "sha256": hashlib.sha256(json_content).hexdigest(),
            },
            "markdown": {
                "name": paths.markdown_path.name,
                "sha256": hashlib.sha256(markdown_content).hexdigest(),
            },
        },
        "report_date": payload["report_date"],
        "report_time": payload["report_time"],
        "report_type": payload["report_type"],
        "run_id": payload["run_id"],
        "schema_version": 1,
        "transaction_id": transaction_id,
    }
    return _json_bytes(marker)


def _validate_complete_archive_fd(
    date_fd: int,
    names: dict[str, str],
    report_type: str,
    report_date: str,
    run_id: str,
) -> dict:
    contents = _read_archive_contents(date_fd, names)
    return _validate_complete_archive_contents(contents, names, report_type, report_date, run_id)


def _read_archive_contents(
    date_fd: int,
    names: dict[str, str],
    *,
    marker_limit: int | None = None,
    json_limit: int | None = None,
    markdown_limit: int | None = None,
) -> dict[str, bytes]:
    contents = {}
    for key in ("markdown", "json", "marker"):
        try:
            limit = {"marker": marker_limit, "json": json_limit, "markdown": markdown_limit}[key]
            contents[key] = _read_regular_bytes(date_fd, names[key], max_bytes=limit)
        except FileNotFoundError as error:
            raise ValueError("incomplete report archive") from error
    return contents


def _validate_complete_archive_contents(
    contents: dict[str, bytes],
    names: dict[str, str],
    report_type: str,
    report_date: str,
    run_id: str,
) -> dict:
    try:
        marker = json.loads(contents["marker"].decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid completion marker") from error
    expected_files = {
        "json": {"name": names["json"], "sha256": hashlib.sha256(contents["json"]).hexdigest()},
        "markdown": {
            "name": names["markdown"],
            "sha256": hashlib.sha256(contents["markdown"]).hexdigest(),
        },
    }
    if not isinstance(marker, dict) or any(
        marker.get(key) != value
        for key, value in {
            "report_date": report_date,
            "report_time": "08:30" if report_type == "premarket" else "22:30",
            "report_type": report_type,
            "run_id": run_id,
            "files": expected_files,
            "schema_version": 1,
        }.items()
    ) or not isinstance(marker.get("transaction_id"), str) or not re.fullmatch(
        r"[a-f0-9]{32}", marker["transaction_id"]
    ):
        raise ValueError("completion marker mismatch")
    return marker


def _resolve_report_directory(output_dir: Path, report_date: str) -> Path:
    _require_filesystem_capabilities()
    _validate_report_date(report_date)
    configured_root = _validated_root_path(output_dir)
    root_fd, date_fd = _open_report_fds(configured_root, report_date, create_date=True)
    os.close(date_fd)
    os.close(root_fd)
    return configured_root / report_date


def _archive_names(report_type: str, run_id: str) -> dict[str, str]:
    suffix = "" if run_id == "initial" else f".{run_id}"
    stem = f"{report_type}{suffix}"
    return {
        "claim": f".{stem}.claim",
        "json": f"{stem}.json",
        "markdown": f"{stem}.md",
        "marker": f"{stem}.complete.json",
    }


def _validate_report_date(report_date: str) -> None:
    try:
        parsed_date = date.fromisoformat(report_date)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid report_date") from error
    if parsed_date.isoformat() != report_date:
        raise ValueError("invalid report_date")


def _validate_report_type(report_type: str) -> None:
    if report_type not in _REPORT_TYPES:
        raise ValueError("unsupported report type")


def _validated_root_path(output_dir: Path) -> Path:
    supplied_root = Path(output_dir)
    if ".." in supplied_root.parts:
        raise ValueError("unsafe output_dir")
    configured_root = Path(os.path.abspath(advisor_paths.reports_dir()))
    if Path(os.path.abspath(supplied_root)) != configured_root:
        raise ValueError("output_dir must equal configured reports root")
    return configured_root


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _open_directory(name: str | Path, *, dir_fd: int | None = None) -> int:
    return os.open(name, _directory_flags(), dir_fd=dir_fd)


def _open_root_fd(root: Path) -> int:
    current_fd = _open_directory(root.anchor)
    try:
        for part in root.parts[1:]:
            try:
                next_fd = _open_directory(part, dir_fd=current_fd)
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=current_fd)
                next_fd = _open_directory(part, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except OSError as error:
        os.close(current_fd)
        raise ValueError("symlinked reports root") from error


def _open_existing_root_fd(root: Path) -> int:
    try:
        return _open_directory(root)
    except OSError as error:
        raise ValueError("reports root unavailable") from error


def _open_existing_report_fds(output_dir: Path, report_date: str) -> tuple[int, int]:
    root_fd = _open_existing_root_fd(output_dir)
    try:
        date_fd = _open_directory(report_date, dir_fd=root_fd)
        _verify_date_binding(root_fd, date_fd, report_date)
        return root_fd, date_fd
    except OSError as error:
        os.close(root_fd)
        raise ValueError("report archive unavailable") from error


def _open_report_fds(output_dir: Path, report_date: str, *, create_date: bool) -> tuple[int, int]:
    _require_filesystem_capabilities()
    _validate_report_date(report_date)
    root = _validated_root_path(output_dir)
    root_fd = _open_root_fd(root)
    try:
        if create_date:
            try:
                os.mkdir(report_date, 0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
        date_fd = _open_directory(report_date, dir_fd=root_fd)
        _verify_date_binding(root_fd, date_fd, report_date)
        return root_fd, date_fd
    except OSError as error:
        os.close(root_fd)
        raise ValueError("symlinked report date path") from error


def _verify_date_binding(root_fd: int, date_fd: int, report_date: str) -> None:
    try:
        path_stat = os.stat(report_date, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError as error:
        raise ValueError("report date path changed") from error
    fd_stat = os.fstat(date_fd)
    if not stat.S_ISDIR(path_stat.st_mode) or (path_stat.st_dev, path_stat.st_ino) != (
        fd_stat.st_dev,
        fd_stat.st_ino,
    ):
        raise ValueError("report date path changed")


def _write_exclusive_file(directory_fd: int, name: str, content: bytes) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_regular_bytes(directory_fd: int, name: str, *, max_bytes: int | None = None) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        entry_stat = os.fstat(descriptor)
        if not stat.S_ISREG(entry_stat.st_mode):
            raise ValueError("archive entry is not a regular file")
        if max_bytes is not None and entry_stat.st_size > max_bytes:
            raise ValueError("archive entry exceeds size limit")
        chunks = []
        total = 0
        while chunk := os.read(descriptor, 64 * 1024):
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise ValueError("archive entry exceeds size limit")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _entry_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _entry_is_symlink(directory_fd: int, name: str) -> bool:
    try:
        return stat.S_ISLNK(os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mode)
    except FileNotFoundError:
        return False


def _any_archive_entry(directory_fd: int, names: dict[str, str]) -> bool:
    return any(_entry_exists(directory_fd, names[key]) for key in ("markdown", "json", "marker"))


def _acquire_claim(
    date_fd: int,
    canonical_name: str,
    candidate_claim: dict,
) -> tuple[int, bool, dict]:
    private_name = f"{canonical_name}.{candidate_claim['transaction_id']}.private"
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW
    private_fd = os.open(private_name, flags, 0o600, dir_fd=date_fd)
    try:
        fcntl.flock(private_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _write_locked_claim(private_fd, candidate_claim)
        try:
            os.link(
                private_name,
                canonical_name,
                src_dir_fd=date_fd,
                dst_dir_fd=date_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            _unlink_hidden(date_fd, private_name)
            _release_claim_lock(private_fd)
            return _open_existing_claim(date_fd, canonical_name)
        _unlink_hidden(date_fd, private_name)
        os.fsync(date_fd)
        return private_fd, False, candidate_claim
    except BaseException:
        _unlink_hidden(date_fd, private_name)
        try:
            _release_claim_lock(private_fd)
        except OSError:
            pass
        raise


def _open_existing_claim(date_fd: int, canonical_name: str) -> tuple[int, bool, dict]:
    flags = os.O_RDWR | os.O_NOFOLLOW
    claim_fd = os.open(canonical_name, flags, dir_fd=date_fd)
    try:
        if not stat.S_ISREG(os.fstat(claim_fd).st_mode):
            raise ValueError("invalid publication claim")
        try:
            fcntl.flock(claim_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise FileExistsError("active publication claim") from error
        claim = json.loads(_read_locked_claim(claim_fd).decode("utf-8"))
        return claim_fd, True, claim
    except BaseException:
        try:
            _release_claim_lock(claim_fd)
        except OSError:
            pass
        raise


def _write_locked_claim(claim_fd: int, claim: dict) -> None:
    content = _json_bytes(claim)
    os.ftruncate(claim_fd, 0)
    os.lseek(claim_fd, 0, os.SEEK_SET)
    view = memoryview(content)
    while view:
        written = os.write(claim_fd, view)
        view = view[written:]
    os.fsync(claim_fd)


def _read_locked_claim(claim_fd: int) -> bytes:
    os.lseek(claim_fd, 0, os.SEEK_SET)
    chunks = []
    while chunk := os.read(claim_fd, 64 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _release_claim_lock(claim_fd: int) -> None:
    try:
        fcntl.flock(claim_fd, fcntl.LOCK_UN)
    finally:
        os.close(claim_fd)


def _recover_claim(
    date_fd: int,
    root_fd: int,
    report_date: str,
    names: dict[str, str],
    report_type: str,
    run_id: str,
    claim: dict,
) -> bool:
    if not isinstance(claim, dict) or any(
        claim.get(key) != value
        for key, value in {"report_date": report_date, "report_type": report_type, "run_id": run_id}.items()
    ):
        raise ValueError("invalid publication claim")
    pid = claim.get("pid")
    stage_name = claim.get("stage")
    transaction_id = claim.get("transaction_id")
    if type(pid) is not int or pid <= 0 or not isinstance(stage_name, str) or not isinstance(transaction_id, str):
        raise ValueError("invalid publication claim")
    expected_stage = f".txn-{names['markdown'].removesuffix('.md')}-{transaction_id}"
    if not re.fullmatch(r"[a-f0-9]{32}", transaction_id) or stage_name != expected_stage:
        raise ValueError("invalid publication claim")

    try:
        stage_fd = _open_directory(stage_name, dir_fd=date_fd)
    except (FileNotFoundError, OSError):
        _unlink_hidden(date_fd, names["claim"])
        os.fsync(date_fd)
        return False
    try:
        if not _entry_exists(stage_fd, names["marker"]):
            _cleanup_hidden_stage(date_fd, stage_name, names)
            _unlink_hidden(date_fd, names["claim"])
            os.fsync(date_fd)
            return False
        marker = _validate_complete_archive_fd(stage_fd, names, report_type, report_date, run_id)
        if marker.get("transaction_id") != transaction_id:
            raise ValueError("invalid staged transaction")
        _verify_date_binding(root_fd, date_fd, report_date)
        _repair_final_link(stage_fd, date_fd, names["markdown"], marker["files"]["markdown"]["sha256"])
        _repair_final_link(stage_fd, date_fd, names["json"], marker["files"]["json"]["sha256"])
        if _entry_exists(date_fd, names["marker"]):
            _validate_complete_archive_fd(date_fd, names, report_type, report_date, run_id)
        else:
            os.link(
                names["marker"],
                names["marker"],
                src_dir_fd=stage_fd,
                dst_dir_fd=date_fd,
                follow_symlinks=False,
            )
        os.fsync(date_fd)
        _validate_complete_archive_fd(date_fd, names, report_type, report_date, run_id)
    finally:
        os.close(stage_fd)
    _unlink_hidden(date_fd, names["claim"])
    os.fsync(date_fd)
    _cleanup_hidden_stage(date_fd, stage_name, names)
    return True


def _repair_final_link(stage_fd: int, date_fd: int, name: str, expected_hash: str) -> None:
    if _entry_exists(date_fd, name):
        try:
            existing = _read_regular_bytes(date_fd, name)
        except (OSError, ValueError) as error:
            raise ValueError("conflicting recovery destination") from error
        if hashlib.sha256(existing).hexdigest() != expected_hash:
            raise ValueError("conflicting recovery destination")
        return
    os.link(name, name, src_dir_fd=stage_fd, dst_dir_fd=date_fd, follow_symlinks=False)


def _cleanup_hidden_stage(date_fd: int, stage_name: str, names: dict[str, str]) -> None:
    try:
        stage_fd = _open_directory(stage_name, dir_fd=date_fd)
    except OSError:
        return
    try:
        for key in ("markdown", "json", "marker"):
            _unlink_stage_entry(stage_fd, names[key])
    finally:
        os.close(stage_fd)
    try:
        os.rmdir(stage_name, dir_fd=date_fd)
    except OSError:
        pass


def _unlink_hidden(directory_fd: int, name: str) -> None:
    if not name.startswith("."):
        raise ValueError("refusing to unlink public report path")
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


def _unlink_stage_entry(stage_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=stage_fd)
    except FileNotFoundError:
        pass


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _publication_hook(_event: str, **_context) -> None:
    return None


def _reader_hook(_event: str, **_context) -> None:
    return None


def _require_filesystem_capabilities() -> None:
    required_constants = ("O_DIRECTORY", "O_NOFOLLOW")
    if (
        fcntl is None
        or not callable(getattr(fcntl, "flock", None))
        or not all(hasattr(fcntl, name) for name in ("LOCK_EX", "LOCK_NB", "LOCK_UN"))
        or not all(hasattr(os, name) for name in required_constants)
        or not callable(getattr(os, "link", None))
        or not _NATIVE_DIR_FD_SUPPORT
        or not _NATIVE_LINK_NOFOLLOW_SUPPORT
    ):
        raise RuntimeError("required report filesystem capabilities unavailable")
