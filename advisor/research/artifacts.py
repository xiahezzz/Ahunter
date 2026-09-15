from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any
import uuid

from advisor.research.contracts import canonical_json


@dataclass(frozen=True)
class ArtifactRef:
    content_hash: str
    byte_size: int
    media_type: str


class ArtifactStore:
    """Immutable content-addressed files for research inputs and outputs."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._sync_directory(self.root.parent)
        self._staging_root = self.root / ".staging"
        self._staging_root.mkdir(exist_ok=True)
        with (self._staging_root / ".reaper.lock").open("a+b") as reaper_lock:
            fcntl.flock(reaper_lock.fileno(), fcntl.LOCK_EX)
            self._reap_orphaned_staging()
            self.staging_dir = self._staging_root / f"owner-{os.getpid()}-{uuid.uuid4().hex}"
            self.staging_dir.mkdir()
            self._staging_lock = (self.staging_dir / ".lock").open("a+b")
            fcntl.flock(self._staging_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(reaper_lock.fileno(), fcntl.LOCK_UN)

    def close(self) -> None:
        lock = getattr(self, "_staging_lock", None)
        if lock is None:
            return
        shutil.rmtree(self.staging_dir, ignore_errors=True)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
        self._staging_lock = None

    def _reap_orphaned_staging(self) -> None:
        for candidate in self._staging_root.glob("owner-*"):
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            lock_path = candidate / ".lock"
            try:
                with lock_path.open("a+b") as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    shutil.rmtree(candidate)
            except (BlockingIOError, FileNotFoundError, OSError):
                continue

    def put_json(self, value: Any, *, media_type: str = "application/json") -> ArtifactRef:
        return self.put_bytes(canonical_json(value), media_type=media_type)

    def put_text(self, value: str, *, media_type: str = "text/plain; charset=utf-8") -> ArtifactRef:
        if not isinstance(value, str):
            raise TypeError("artifact text must be str")
        return self.put_bytes(value.encode("utf-8"), media_type=media_type)

    def put_bytes(self, value: bytes, *, media_type: str = "application/octet-stream") -> ArtifactRef:
        if not isinstance(value, bytes):
            raise TypeError("artifact bytes must be bytes")
        digest = hashlib.sha256(value).hexdigest()
        target = self._path_for(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file() or self._hash_file(target) != digest:
                raise ValueError("artifact path already contains different content")
            self._sync_directory(target.parent)
            self._sync_directory(self.root)
            return ArtifactRef(digest, len(value), media_type)

        fd, temporary = tempfile.mkstemp(prefix=f".{digest}.", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            if target.exists() or target.is_symlink():
                if target.is_symlink() or not target.is_file() or self._hash_file(target) != digest:
                    raise ValueError("artifact path raced with different content")
                Path(temporary).unlink(missing_ok=True)
            else:
                os.replace(temporary, target)
            self._sync_directory(target.parent)
            self._sync_directory(self.root)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return ArtifactRef(digest, len(value), media_type)

    def adopt_file(
        self,
        source: Path,
        *,
        media_type: str = "application/octet-stream",
    ) -> ArtifactRef:
        """Adopt a completed file without ever materializing it in memory.

        The source is copied into this Store's locked staging area while its
        digest is computed, then the private staged inode is atomically linked
        into place. This removes source-path TOCTOU without buffering bytes.
        """

        source = source.expanduser()
        if source.is_symlink() or not source.is_file():
            raise ValueError("artifact source must be a regular file")
        source = source.resolve()
        fd, temporary = tempfile.mkstemp(prefix=".adopt-", dir=self.staging_dir)
        staged = Path(temporary)
        digest_builder = hashlib.sha256()
        byte_size = 0
        try:
            with source.open("rb") as input_handle, os.fdopen(fd, "wb") as output_handle:
                for chunk in iter(lambda: input_handle.read(1024 * 1024), b""):
                    digest_builder.update(chunk)
                    byte_size += len(chunk)
                    output_handle.write(chunk)
                output_handle.flush()
                os.fsync(output_handle.fileno())
            digest = digest_builder.hexdigest()
            target = self._path_for(digest)
            if source == target:
                self._sync_directory(target.parent)
                self._sync_directory(self.root)
                return ArtifactRef(digest, byte_size, media_type)
            target.parent.mkdir(parents=True, exist_ok=True)
            created = False
            try:
                os.link(staged, target)
                created = True
            except FileExistsError:
                if target.is_symlink() or not target.is_file() or self._hash_file(target) != digest:
                    raise ValueError("artifact path raced with different content") from None
            if target.is_symlink() or not target.is_file() or self._hash_file(target) != digest:
                if created:
                    target.unlink(missing_ok=True)
                raise ValueError("sealed artifact content hash mismatch")
            self._sync_directory(target.parent)
            self._sync_directory(self.root)
            source.unlink(missing_ok=True)
            return ArtifactRef(digest, byte_size, media_type)
        finally:
            staged.unlink(missing_ok=True)

    def read_bytes(self, content_hash: str) -> bytes:
        target = self._path_for(content_hash)
        if not target.is_file() or target.is_symlink():
            raise FileNotFoundError(content_hash)
        value = target.read_bytes()
        if hashlib.sha256(value).hexdigest() != content_hash:
            raise ValueError("artifact content hash mismatch")
        return value

    def read_json(self, content_hash: str) -> Any:
        value = self.read_bytes(content_hash)
        return json.loads(value.decode("utf-8"))

    def verify(self, content_hash: str) -> bool:
        try:
            target = self._path_for(content_hash)
            if not target.is_file() or target.is_symlink():
                return False
            return self._hash_file(target) == content_hash
        except (OSError, ValueError):
            return False

    def _path_for(self, content_hash: str) -> Path:
        if not isinstance(content_hash, str) or len(content_hash) != 64 or any(
            char not in "0123456789abcdef" for char in content_hash
        ):
            raise ValueError("content_hash must be a lowercase SHA-256 hex digest")
        target = (self.root / content_hash[:2] / content_hash[2:]).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("artifact path escaped store")
        return target

    @staticmethod
    def _sync_directory(path: Path) -> None:
        """Persist the directory entry before a database can commit its reference."""
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
