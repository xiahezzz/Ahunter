"""Seal candidate bytes into the existing ArtifactStore, independently of lineage.

Registration never executes source code. Runtime sandboxing remains mandatory
when loading a package; a manifest is not an execution permission grant.
"""
from pathlib import Path, PurePosixPath
import base64
import json

from pydantic import Field

from .contracts import CandidateFile, CandidatePackage, Contract, Name
from .resolution import digest, encode


class CandidateTuningConfig(Contract):
    # Only research choices; account, clock, budget, model and evaluation are host-owned.
    query_strategy: str | None = Field(default=None, min_length=1)
    delegation_strategy: str | None = Field(default=None, min_length=1)
    memory_strategy: str | None = Field(default=None, min_length=1)


class CandidateInput(Contract):
    source_paths: tuple[str, ...] = Field(min_length=1)
    prompt_paths: tuple[str, ...] = Field(min_length=1)
    dependency_lock_paths: tuple[str, ...] = Field(min_length=1)
    contract_paths: tuple[str, ...] = Field(min_length=1)
    entrypoint: str
    input_contract: Name
    output_contract: Name
    allowed_config: CandidateTuningConfig


def _source_path(root: Path, name: str) -> Path:
    relative = _relative_path(name)
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("candidate source cannot contain symlinks")
    if not path.resolve().is_relative_to(root) or not path.is_file():
        raise ValueError("candidate source must be a regular file within its root")
    return path


def _relative_path(name):
    relative = PurePosixPath(name)
    if not name or "\x00" in name or "\\" in name or relative.is_absolute() or any(part in {".", ".."} for part in name.split("/")):
        raise ValueError("candidate paths must be normalized relative paths")
    if relative.as_posix() != name:
        raise ValueError("candidate paths must be normalized relative paths")
    return relative


def _manifest_paths(request):
    kinds = (("source", request.source_paths), ("prompt", request.prompt_paths),
             ("dependency_lock", request.dependency_lock_paths), ("contract", request.contract_paths))
    paths = [(name, kind) for kind, names in kinds for name in names]
    if len({name for name, _ in paths}) != len(paths):
        raise ValueError("candidate files must have unique manifest paths")
    if request.entrypoint not in request.source_paths:
        raise ValueError("entrypoint must name a sealed source file")
    names = {name for name, _ in paths}
    for name in names:
        relative = _relative_path(name)
        if any(parent.as_posix() in names for parent in relative.parents):
            raise ValueError("candidate file cannot also be a directory")
    return sorted(paths)


def seal_candidate(root: Path, raw: dict, *, artifacts) -> CandidatePackage:
    request = CandidateInput.model_validate(raw)
    root = root.resolve(strict=True)
    # Validate the entire manifest before any durable writes. Capture source bytes
    # once so later host-file edits cannot alter the package content or identity.
    captured = [(name, kind, _source_path(root, name).read_bytes()) for name, kind in _manifest_paths(request)]
    return _seal(request, captured, artifacts)


def decode_candidate_bytes(value):
    """Canonical base64 is a byte transport, never a host path or a code loader."""
    if not isinstance(value, str):
        raise ValueError("candidate file bytes must use base64 strings")
    data = base64.b64decode(value, validate=True)
    if base64.b64encode(data).decode("ascii") != value:
        raise ValueError("candidate file bytes must use canonical base64")
    return data


def seal_candidate_upload(raw, files_base64, *, artifacts):
    request = CandidateInput.model_validate(raw)
    paths = _manifest_paths(request)
    if not isinstance(files_base64, dict) or set(files_base64) != {name for name, _ in paths}:
        raise ValueError("upload must contain exactly every declared candidate file")
    captured = [(name, kind, decode_candidate_bytes(files_base64[name])) for name, kind in paths]
    return _seal(request, captured, artifacts)


def _seal(request, captured, artifacts):
    files = []
    for name, kind, data in captured:
        ref = artifacts.put_bytes(data)
        files.append(CandidateFile(path=name, kind=kind, content_hash=ref.content_hash, byte_size=ref.byte_size))
    manifest = {"schema_version": 1, "files": files, "entrypoint": request.entrypoint,
                "allowed_config_json": encode(request.allowed_config.model_dump(exclude_none=True)),
                "input_contract": request.input_contract, "output_contract": request.output_contract}
    sealed = artifacts.put_bytes(encode(manifest).encode("utf-8"), media_type="application/json")
    return CandidatePackage(package_hash=sealed.content_hash, **manifest)


def read_candidate(package_hash: str, *, artifacts) -> CandidatePackage:
    manifest = artifacts.read_json(package_hash)
    if digest(manifest) != package_hash:
        raise ValueError("candidate manifest is not canonical")
    package = CandidatePackage(package_hash=package_hash, **manifest)
    CandidateTuningConfig.model_validate(json.loads(package.allowed_config_json))
    paths = [item.path for item in package.files]
    if len(paths) != len(set(paths)) or package.entrypoint not in {f.path for f in package.files if f.kind == "source"}:
        raise ValueError("invalid candidate file manifest")
    if {f.kind for f in package.files} != {"source", "prompt", "dependency_lock", "contract"}:
        raise ValueError("candidate manifest is missing required file kinds")
    for item in package.files:
        name = _relative_path(item.path)
        if any(parent.as_posix() in paths for parent in name.parents):
            raise ValueError("candidate file cannot also be a directory")
        if len(artifacts.read_bytes(item.content_hash)) != item.byte_size:
            raise ValueError("candidate artifact size mismatch")
    return package
