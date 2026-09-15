from pathlib import Path

import pytest

from advisor.research.artifacts import ArtifactStore


def test_artifact_store_is_content_addressed_and_idempotent(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    first = store.put_json({"b": 2, "a": 1})
    second = store.put_json({"a": 1, "b": 2})

    assert first == second
    assert store.read_json(first.content_hash) == {"a": 1, "b": 2}
    assert store.verify(first.content_hash)


def test_artifact_store_rejects_invalid_hash_and_corruption(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.put_text("safe")
    target = store._path_for(ref.content_hash)
    target.write_text("changed", encoding="utf-8")

    assert not store.verify(ref.content_hash)
    with pytest.raises(ValueError):
        store.read_bytes(ref.content_hash)
    with pytest.raises(ValueError):
        store.read_bytes("../outside")


def test_artifact_store_adopts_a_large_file_without_loading_it_into_memory(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    source = tmp_path / "query.jsonl.gz"
    source.write_bytes(b"streamed-query-data" * 10_000)

    ref = store.adopt_file(source, media_type="application/vnd.a-hunter.query-product+gzip")

    assert not source.exists()
    assert ref.byte_size == len(b"streamed-query-data" * 10_000)
    assert ref.media_type == "application/vnd.a-hunter.query-product+gzip"
    assert store.read_bytes(ref.content_hash).startswith(b"streamed-query-data")


def test_artifact_store_reaps_only_unlocked_crash_staging(tmp_path: Path):
    root = tmp_path / "artifacts"
    active = ArtifactStore(root)
    orphan = root / ".staging" / "owner-crashed"
    orphan.mkdir()
    (orphan / ".lock").write_bytes(b"")
    (orphan / ".whole-market-query-orphan.jsonl.gz").write_bytes(b"partial")

    restarted = ArtifactStore(root)
    try:
        assert active.staging_dir.is_dir()
        assert not orphan.exists()
    finally:
        restarted.close()
        active.close()
