"""Atomic, local-only ownership of the RID Authorization Set."""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode


MAX_CONFIG_BYTES = 64 * 1024
MAX_RIDS = 1_000
MAX_SAFE_INTEGER = 2**53 - 1


class RidAuthorizationError(ValueError):
    """Base error with intentionally non-sensitive messages."""


class RidAuthorizationValidationError(RidAuthorizationError):
    pass


class RidAuthorizationConflict(RidAuthorizationError):
    pass


class RidAuthorizationUnavailable(RidAuthorizationError):
    pass


@dataclass(frozen=True)
class RidAuthorization:
    rids: tuple[int, ...]
    version: str

    @property
    def collection_enabled(self) -> bool:
        return bool(self.rids)

    def as_dict(self) -> dict[str, object]:
        return {
            "rids": list(self.rids),
            "version": self.version,
            "collection_enabled": self.collection_enabled,
        }


class RidAuthorizationStore:
    """The sole mutation seam for the authoritative allowed-rids YAML file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def read(self) -> RidAuthorization:
        raw = _read_authoritative_bytes(self.path)
        return RidAuthorization(
            rids=_parse_authorized_rids(raw),
            version=hashlib.sha256(raw).hexdigest(),
        )

    def replace(self, rids: Iterable[int], *, expected_version: str) -> RidAuthorization:
        canonical_rids = _validate_rids(rids)
        if not isinstance(expected_version, str) or len(expected_version) != 64 or any(
            value not in "0123456789abcdef" for value in expected_version
        ):
            raise RidAuthorizationValidationError("invalid version")

        try:
            with _locked_sibling(self.path):
                current = self.read()
                if current.version != expected_version:
                    raise RidAuthorizationConflict("version conflict")
                payload = _canonical_yaml(canonical_rids)
                _atomic_replace(self.path, payload)
                return RidAuthorization(
                    rids=canonical_rids,
                    version=hashlib.sha256(payload).hexdigest(),
                )
        except RidAuthorizationError:
            raise
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise RidAuthorizationUnavailable("configuration unavailable") from error


def parse_authorized_rids(raw: bytes | str) -> tuple[int, ...]:
    """Shared fixture seam: strict parser without reading a real file."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, bytes) or len(raw) > MAX_CONFIG_BYTES:
        raise RidAuthorizationValidationError("invalid configuration")
    return _parse_authorized_rids(raw)


def _read_authoritative_bytes(path: Path) -> bytes:
    descriptor: int | None = None
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or before.st_size > MAX_CONFIG_BYTES:
            raise RidAuthorizationUnavailable("configuration unavailable")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_CONFIG_BYTES:
            raise RidAuthorizationUnavailable("configuration unavailable")
        chunks: list[bytes] = []
        remaining = MAX_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_CONFIG_BYTES:
            raise RidAuthorizationUnavailable("configuration unavailable")
        return raw
    except RidAuthorizationError:
        raise
    except OSError as error:
        raise RidAuthorizationUnavailable("configuration unavailable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _parse_authorized_rids(raw: bytes) -> tuple[int, ...]:
    try:
        text = raw.decode("utf-8")
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise RidAuthorizationValidationError("invalid configuration") from error
    if not isinstance(root, MappingNode) or len(root.value) != 1:
        raise RidAuthorizationValidationError("invalid configuration")
    key, value = root.value[0]
    if not isinstance(key, ScalarNode) or key.value != "allowed_rids" or not isinstance(value, SequenceNode):
        raise RidAuthorizationValidationError("invalid configuration")
    if len(value.value) > MAX_RIDS:
        raise RidAuthorizationValidationError("invalid configuration")

    rids: list[int] = []
    for item in value.value:
        # Match Node's strict decimal contract.  YAML's typed value alone is
        # insufficient because `1.0` and `0x1` can both become Python ints.
        if (
            not isinstance(item, ScalarNode)
            or item.tag != "tag:yaml.org,2002:int"
            or not item.style is None
            or not item.value.isascii()
            or not item.value.isdecimal()
            or item.value.startswith("0")
        ):
            raise RidAuthorizationValidationError("invalid configuration")
        parsed = int(item.value)
        if parsed <= 0 or parsed > MAX_SAFE_INTEGER:
            raise RidAuthorizationValidationError("invalid configuration")
        rids.append(parsed)
    return _validate_rids(rids)


def _validate_rids(rids: Iterable[int]) -> tuple[int, ...]:
    if isinstance(rids, (str, bytes)):
        raise RidAuthorizationValidationError("invalid RIDs")
    try:
        values = tuple(rids)
    except TypeError as error:
        raise RidAuthorizationValidationError("invalid RIDs") from error
    if len(values) > MAX_RIDS or any(
        type(value) is not int or value <= 0 or value > MAX_SAFE_INTEGER for value in values
    ):
        raise RidAuthorizationValidationError("invalid RIDs")
    if len(set(values)) != len(values):
        raise RidAuthorizationValidationError("invalid RIDs")
    return tuple(sorted(values))


def _canonical_yaml(rids: tuple[int, ...]) -> bytes:
    if not rids:
        return b"allowed_rids: []\n"
    return ("allowed_rids:\n" + "".join(f"  - {value}\n" for value in rids)).encode("utf-8")


class _locked_sibling:
    def __init__(self, authoritative_path: Path) -> None:
        self.authoritative_path = authoritative_path
        self.descriptor: int | None = None

    def __enter__(self) -> _locked_sibling:
        parent = self.authoritative_path.parent
        parent_stat = parent.lstat()
        if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
            raise RidAuthorizationUnavailable("configuration unavailable")
        lock_path = parent / f".{self.authoritative_path.name}.lock"
        lock_stat = lock_path.lstat() if lock_path.exists() else None
        if lock_stat is not None and (stat.S_ISLNK(lock_stat.st_mode) or not stat.S_ISREG(lock_stat.st_mode)):
            raise RidAuthorizationUnavailable("configuration unavailable")
        self.descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        if not stat.S_ISREG(os.fstat(self.descriptor).st_mode):
            raise RidAuthorizationUnavailable("configuration unavailable")
        fcntl.flock(self.descriptor, fcntl.LOCK_EX)
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        if self.descriptor is not None:
            try:
                fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            finally:
                os.close(self.descriptor)
                self.descriptor = None


def _atomic_replace(path: Path, payload: bytes) -> None:
    descriptor: int | None = None
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary_name, path)
        temporary_name = None
        # The pre-replace fsync is the failure point that must preserve the old
        # file.  A directory fsync is best-effort on local filesystems; a late
        # error cannot be honestly reported as a failed write after replacement.
        try:
            directory = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory)
        except OSError:
            pass
        finally:
            os.close(directory)
    except OSError as error:
        raise RidAuthorizationUnavailable("configuration unavailable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
