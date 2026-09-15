from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from advisor.research.artifacts import ArtifactStore
from advisor.research.codex.schema import compile_output_schema
from advisor.research.contracts import ResearchBoundary, ResearchSubject, VersionRef, canonical_json


_CAPSULE_HOME = "Library/Caches/AHunter"
_CAPSULE_DIRECTORY = "ResearchCapsules"


@dataclass
class RunCapsule:
    root: Path
    input_hash: str
    declared_products: tuple[str, ...]
    _temporary: tempfile.TemporaryDirectory[str] | None = None
    manifest_hash: str | None = None

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def query_log_path(self) -> Path:
        return self.root / "query-log.jsonl"

    def cleanup(self) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
        elif self.root.exists():
            shutil.rmtree(self.root)


def build_capsule(
    *,
    label: str,
    instructions: str,
    subject: ResearchSubject,
    boundary: ResearchBoundary,
    inputs: dict[str, Any],
    evidence_index: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    output_schema: dict[str, Any],
    query_budget: int | None = None,
    max_result_rows: int | None = None,
    artifact_store: ArtifactStore | None = None,
    max_result_bytes: int | None = None,
) -> RunCapsule:
    if not label or not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("capsule label and instructions are required")
    if any(value is not None and (type(value) is not int or value < minimum)
           for value, minimum in ((query_budget, 0), (max_result_rows, 1), (max_result_bytes, 1))):
        raise ValueError("invalid capsule query limits")
    output_schema = compile_output_schema(output_schema)
    temporary = tempfile.TemporaryDirectory(
        prefix="a-hunter-capsule-",
        dir=_private_capsule_parent(),
    )
    root = Path(temporary.name).resolve()
    (root / "products").mkdir()
    (root / "query-data").mkdir()
    query_backed_products: list[str] = []
    query_backed_formats: dict[str, str] = {}
    hash_inputs: dict[str, Any] = {}
    for product_ref, payload in sorted(inputs.items()):
        if not isinstance(product_ref, str):
            raise ValueError("capsule input product references must be versioned")
        try:
            canonical_ref = str(VersionRef.parse(product_ref))
        except (TypeError, ValueError) as error:
            raise ValueError("capsule input product references must be versioned") from error
        if canonical_ref != product_ref:
            raise ValueError("capsule input product references must be canonical")
        query_payload = None
        query_artifact = None
        visible_payload = payload
        # A query-backed Product keeps its full deterministic dataset outside
        # the Agent-visible input envelope.  CapsuleQuery is still restricted
        # to this one immutable file and records every query operation.
        if isinstance(payload, dict) and isinstance(payload.get("payload"), dict):
            product_payload = payload["payload"]
            if "__a_hunter_query_payload__" in product_payload:
                query_payload = product_payload["__a_hunter_query_payload__"]
                summary = product_payload.get("summary")
                if not isinstance(summary, dict):
                    raise ValueError("query-backed Product requires a summary object")
                visible_payload = {**payload, "payload": summary}
            elif "__a_hunter_query_artifact__" in product_payload:
                query_artifact = product_payload["__a_hunter_query_artifact__"]
                query_format = product_payload.get("__a_hunter_query_format__", "json")
                summary = product_payload.get("summary")
                if (
                    not isinstance(query_artifact, str)
                    or query_format not in {"json", "ndjson-v1"}
                    or not isinstance(summary, dict)
                ):
                    raise ValueError("query-backed Product requires an Artifact and summary")
                visible_payload = {**payload, "payload": summary}
        (root / "products" / f"{product_ref.replace('@', '__')}.json").write_bytes(canonical_json(visible_payload))
        query_path = root / "query-data" / f"{product_ref.replace('@', '__')}.json"
        if query_artifact is not None:
            if artifact_store is None or not artifact_store.verify(query_artifact):
                raise ValueError("query-backed Product Artifact is unavailable")
            os.link(artifact_store._path_for(query_artifact), query_path)
            query_backed_products.append(product_ref)
            query_backed_formats[product_ref] = query_format
        elif query_payload is not None:
            query_path.write_bytes(canonical_json(query_payload))
            query_backed_products.append(product_ref)
            query_backed_formats[product_ref] = "json"
        hash_inputs[product_ref] = visible_payload
    manifest = {
        "label": label,
        "subject": subject.model_dump(mode="json"),
        "as_of": boundary.as_of.isoformat(),
        "declared_products": sorted(inputs),
        "query_budget": query_budget,
        "max_result_rows": max_result_rows,
        "max_result_bytes": max_result_bytes,
        "query_backed_products": sorted(query_backed_products),
        "query_backed_formats": query_backed_formats,
        "evidence_index": evidence_index or {},
        "context": context or {},
        "output_schema": output_schema,
    }
    (root / "instructions.md").write_text(instructions, encoding="utf-8")
    (root / "output-schema.json").write_bytes(canonical_json(output_schema))
    (root / "manifest.json").write_bytes(canonical_json(manifest))
    (root / "query-log.jsonl").write_text("", encoding="utf-8")
    (root / "query-log.lock").write_bytes(b"")
    input_hash = _sha256(
        canonical_json(
            {
                "subject": subject.model_dump(mode="json"),
                "as_of": boundary.as_of.isoformat(),
                "inputs": hash_inputs,
                "evidence_index": evidence_index or {},
                "context": context or {},
            }
        )
    )
    manifest_hash = None
    if artifact_store is not None:
        manifest_hash = artifact_store.put_json(
            manifest, media_type="application/vnd.a-hunter.run-capsule+json"
        ).content_hash
    return RunCapsule(root, input_hash, tuple(sorted(inputs)), temporary, manifest_hash)


def _sha256(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


def _private_capsule_parent() -> Path:
    home = Path.home().resolve()
    private_root = home / _CAPSULE_HOME
    parent = private_root / _CAPSULE_DIRECTORY
    for directory in (private_root, parent):
        if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
            raise ValueError("private Research Capsule directory is invalid")
        directory.mkdir(mode=0o700, exist_ok=True)
        stat_result = directory.stat()
        if stat_result.st_uid != os.getuid():
            raise ValueError("private Research Capsule directory has an invalid owner")
        directory.chmod(0o700)
    return parent
