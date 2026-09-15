from datetime import datetime, timezone
import gzip
import json
from pathlib import Path

import pytest

from advisor.research.capsules import build_capsule
from advisor.research.artifacts import ArtifactStore
from advisor.research.contracts import ResearchBoundary, ResearchFinding, ResearchSubject, canonical_json
from advisor.research.query import CapsuleQuery, QueryDenied


def test_capsule_uses_an_owner_private_parent_outside_shared_temporary_storage():
    capsule = build_capsule(
        label="agent",
        instructions="inspect data",
        subject=ResearchSubject(code="600519"),
        boundary=ResearchBoundary(as_of=datetime(2026, 8, 6, tzinfo=timezone.utc)),
        inputs={"bars@1": {"rows": []}},
        output_schema={},
        query_budget=0,
        max_result_rows=1,
    )
    expected_parent = Path.home().resolve() / "Library" / "Caches" / "AHunter" / "ResearchCapsules"
    try:
        assert capsule.root.parent == expected_parent
        assert expected_parent.stat().st_mode & 0o077 == 0
        assert capsule.root.stat().st_mode & 0o077 == 0
    finally:
        capsule.cleanup()


def test_capsule_query_is_declared_read_only_and_audited(tmp_path: Path):
    capsule = build_capsule(
        label="agent",
        instructions="inspect data",
        subject=ResearchSubject(code="600519"),
        boundary=ResearchBoundary(as_of=datetime(2026, 8, 6, tzinfo=timezone.utc)),
        inputs={"bars@1": {"rows": [{"close": 10}, {"close": 11}]}},
        output_schema={"type": "object"},
        query_budget=2,
        max_result_rows=10,
    )
    try:
        query = CapsuleQuery(capsule.root)
        assert query.execute("bars@1", "rows")[1]["close"] == 11
        assert query.execute("bars@1", "slice", start=0, limit=1)[0]["close"] == 10
        assert len(capsule.query_log_path.read_text(encoding="utf-8").splitlines()) == 2
        with pytest.raises(QueryDenied):
            query.execute("unknown@1")
    finally:
        capsule.cleanup()
    assert not capsule.root.exists()


def test_capsule_query_enforces_budget(tmp_path: Path):
    capsule = build_capsule(
        label="agent",
        instructions="inspect data",
        subject=ResearchSubject(code="600519"),
        boundary=ResearchBoundary(as_of=datetime(2026, 8, 6, tzinfo=timezone.utc)),
        inputs={"bars@1": {"rows": []}},
        output_schema={},
        query_budget=0,
        max_result_rows=10,
    )
    try:
        with pytest.raises(QueryDenied, match="budget"):
            CapsuleQuery(capsule.root).execute("bars@1")
    finally:
        capsule.cleanup()


def test_capsule_query_budget_survives_new_query_process(tmp_path: Path):
    capsule = build_capsule(
        label="agent",
        instructions="inspect data",
        subject=ResearchSubject(code="600519"),
        boundary=ResearchBoundary(as_of=datetime(2026, 8, 6, tzinfo=timezone.utc)),
        inputs={"bars@1": {"rows": [{"close": 10}]}},
        output_schema={},
        query_budget=1,
        max_result_rows=10,
    )
    try:
        CapsuleQuery(capsule.root).execute("bars@1", "rows")
        resumed = CapsuleQuery(capsule.root)
        assert resumed.audit.query_count == 1
        with pytest.raises(QueryDenied, match="budget"):
            resumed.execute("bars@1")
    finally:
        capsule.cleanup()


def test_capsule_hard_links_a_compressed_query_artifact_without_copying_it(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    source = tmp_path / "query.jsonl.gz"
    with source.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            for row in ({"close": 10}, {"close": 11}):
                compressed.write(canonical_json({"kind": "bar", "row": row}) + b"\n")
    backing = store.adopt_file(source)
    capsule = build_capsule(
        label="agent",
        instructions="inspect data",
        subject=ResearchSubject(code="600519"),
        boundary=ResearchBoundary(as_of=datetime(2026, 8, 6, tzinfo=timezone.utc)),
        inputs={
            "bars@1": {
                "product": "bars@1",
                "artifact_hash": "a" * 64,
                "payload": {
                    "__a_hunter_query_artifact__": backing.content_hash,
                    "__a_hunter_query_format__": "ndjson-v1",
                    "summary": {"row_count": 2},
                },
            }
        },
        output_schema={},
        query_budget=1,
        max_result_rows=10,
        artifact_store=store,
    )
    try:
        linked = capsule.root / "query-data" / "bars__1.json"
        assert linked.stat().st_ino == store._path_for(backing.content_hash).stat().st_ino
        assert CapsuleQuery(capsule.root).execute("bars@1", "rows")[1]["close"] == 11
        visible = json.loads((capsule.root / "products" / "bars__1.json").read_text())
        assert visible["payload"] == {"row_count": 2}
    finally:
        capsule.cleanup()


def test_capsule_records_evidence_index_without_adding_unlisted_products():
    capsule = build_capsule(
        label="agent",
        instructions="inspect data",
        subject=ResearchSubject(code="600519"),
        boundary=ResearchBoundary(as_of=datetime(2026, 8, 6, tzinfo=timezone.utc)),
        inputs={"bars@1": {"rows": [{"close": 10}]}},
        evidence_index={"bars@1": {"evidence_id": "product:bars"}},
        output_schema={},
        query_budget=1,
        max_result_rows=10,
    )
    try:
        manifest = json.loads(capsule.manifest_path.read_text(encoding="utf-8"))
        assert manifest["declared_products"] == ["bars@1"]
        assert manifest["evidence_index"] == {"bars@1": {"evidence_id": "product:bars"}}
    finally:
        capsule.cleanup()


def test_capsule_rejects_noncanonical_or_path_like_product_references():
    with pytest.raises(ValueError, match="versioned"):
        build_capsule(
            label="agent",
            instructions="inspect data",
            subject=ResearchSubject(code="600519"),
            boundary=ResearchBoundary(as_of=datetime(2026, 8, 6, tzinfo=timezone.utc)),
            inputs={"../outside@1": {"secret": True}},
            output_schema={},
            query_budget=1,
            max_result_rows=10,
        )


def test_capsule_normalizes_nested_object_schemas_for_structured_outputs():
    capsule = build_capsule(
        label="agent",
        instructions="inspect data",
        subject=ResearchSubject(code="600519"),
        boundary=ResearchBoundary(as_of=datetime(2026, 8, 6, tzinfo=timezone.utc)),
        inputs={},
        output_schema={
            "type": "object",
            "properties": {
                "details": {
                    "type": "object",
                    "properties": {"trend": {"type": "string"}},
                }
            },
        },
        query_budget=0,
        max_result_rows=1,
    )
    try:
        schema = json.loads(capsule.root.joinpath("output-schema.json").read_text(encoding="utf-8"))
        assert schema["additionalProperties"] is False
        assert schema["required"] == ["details"]
        assert schema["properties"]["details"]["additionalProperties"] is False
        assert schema["properties"]["details"]["required"] == ["trend"]
    finally:
        capsule.cleanup()


def test_capsule_removes_pydantic_defaults_from_real_research_finding_schema():
    capsule = build_capsule(
        label="agent",
        instructions="inspect data",
        subject=ResearchSubject(code="600519"),
        boundary=ResearchBoundary(as_of=datetime(2026, 8, 6, tzinfo=timezone.utc)),
        inputs={},
        output_schema=ResearchFinding.model_json_schema(),
        query_budget=0,
        max_result_rows=1,
    )
    try:
        schema = json.loads(capsule.root.joinpath("output-schema.json").read_text(encoding="utf-8"))
        assert not _contains_key(schema, "default")
        assert schema["$defs"]["ResearchSubject"]["properties"]["scope"] == {
            "$ref": "#/$defs/ResearchScope"
        }
    finally:
        capsule.cleanup()


def test_capsule_rejects_array_schema_without_items_for_structured_outputs():
    with pytest.raises(ValueError, match="array schema requires items"):
        build_capsule(
            label="agent",
            instructions="inspect data",
            subject=ResearchSubject(code="600519"),
            boundary=ResearchBoundary(as_of=datetime(2026, 8, 6, tzinfo=timezone.utc)),
            inputs={},
            output_schema={
                "type": "object",
                "properties": {"items": {"type": "array"}},
            },
            query_budget=0,
            max_result_rows=1,
        )


def _contains_key(value: object, target: str) -> bool:
    if isinstance(value, dict):
        return target in value or any(_contains_key(child, target) for child in value.values())
    if isinstance(value, list):
        return any(_contains_key(child, target) for child in value)
    return False
