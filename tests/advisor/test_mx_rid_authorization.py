from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from advisor.mx.rid_authorization import (
    RidAuthorizationConflict,
    RidAuthorizationStore,
    RidAuthorizationUnavailable,
    RidAuthorizationValidationError,
    parse_authorized_rids,
)
from advisor.web.api import create_app


def _config(tmp_path: Path, contents: str = "allowed_rids: [20025]\n") -> Path:
    filename = tmp_path / "allowed-rids.yaml"
    filename.write_text(contents, encoding="utf-8")
    return filename


def test_read_is_strict_and_does_not_create_missing_authoritative_file(tmp_path: Path):
    missing = RidAuthorizationStore(tmp_path / "missing.yaml")
    with pytest.raises(RidAuthorizationUnavailable):
        missing.read()
    assert not (tmp_path / "missing.yaml").exists()

    for invalid in (
        "allowed_rids: [1.0]\n",
        "allowed_rids: [true]\n",
        "allowed_rids: [1, 1]\n",
        "allowed_rids: [9007199254740992]\n",
        "allowed_rids: []\nextra: true\n",
    ):
        with pytest.raises(RidAuthorizationValidationError):
            parse_authorized_rids(invalid)


def test_replace_uses_version_conflict_and_canonical_atomic_bytes(tmp_path: Path):
    filename = _config(tmp_path, "allowed_rids:\n  - 20025\n")
    first = RidAuthorizationStore(filename)
    second = RidAuthorizationStore(filename)
    initial = first.read()

    updated = first.replace([23200, 20025], expected_version=initial.version)
    assert updated.rids == (20025, 23200)
    assert filename.read_bytes() == b"allowed_rids:\n  - 20025\n  - 23200\n"

    bytes_after_winner = filename.read_bytes()
    with pytest.raises(RidAuthorizationConflict):
        second.replace([23300], expected_version=initial.version)
    assert filename.read_bytes() == bytes_after_winner


def test_replace_rejects_invalid_data_without_changing_file(tmp_path: Path):
    filename = _config(tmp_path)
    store = RidAuthorizationStore(filename)
    before = filename.read_bytes()
    version = store.read().version

    with pytest.raises(RidAuthorizationValidationError):
        store.replace([20025, 20025], expected_version=version)
    assert filename.read_bytes() == before


def test_mx_rid_api_is_strict_local_and_versioned(tmp_path: Path):
    filename = _config(tmp_path)
    client = TestClient(
        create_app(
            state_dir=tmp_path / "state",
            db_path=tmp_path / "advisor.sqlite",
            allowed_rids_path=filename,
        )
    )
    current = client.get("/api/mx/rids")
    assert current.status_code == 200
    assert current.json()["rids"] == [20025]
    assert current.json()["collection_enabled"] is True

    rejected = client.put(
        "/api/mx/rids",
        json={"rids": [23200], "version": current.json()["version"], "extra": "no"},
    )
    assert rejected.status_code == 400
    assert str(tmp_path) not in rejected.text

    cross_origin = client.put(
        "/api/mx/rids",
        json={"rids": [], "version": current.json()["version"]},
        headers={"origin": "https://not-local.example"},
    )
    assert cross_origin.status_code == 400

    updated = client.put(
        "/api/mx/rids",
        json={"rids": [], "version": current.json()["version"]},
    )
    assert updated.status_code == 200
    assert updated.json()["rids"] == []
    assert updated.json()["collection_enabled"] is False
    assert filename.read_text(encoding="utf-8") == "allowed_rids: []\n"

    stale = client.put(
        "/api/mx/rids",
        json={"rids": [23200], "version": current.json()["version"]},
    )
    assert stale.status_code == 409
    assert "allowed_rids" not in stale.text
