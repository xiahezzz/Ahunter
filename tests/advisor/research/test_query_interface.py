from __future__ import annotations

from datetime import datetime, timezone
import gzip
import multiprocessing
from pathlib import Path
import tracemalloc

import pytest

from advisor.research.artifacts import ArtifactStore
from advisor.research.capsules import build_capsule
from advisor.research.contracts import ResearchBoundary, ResearchSubject, canonical_json
from advisor.research.query import CapsuleQuery, QueryDenied


def _concurrent_query(root: str, start, results) -> None:
    start.wait()
    try:
        CapsuleQuery(Path(root)).execute("news@1", "items")
    except QueryDenied:
        results.put("denied")
    else:
        results.put("passed")


def test_query_records_duration_and_bytes_and_enforces_total_result_budget(tmp_path: Path):
    capsule = build_capsule(
        label="agent",
        instructions="inspect data",
        subject=ResearchSubject(code="600519"),
        boundary=ResearchBoundary(as_of=datetime(2026, 8, 6, tzinfo=timezone.utc)),
        inputs={"news@1": {"items": [{"summary": "a" * 40}]}},
        output_schema={},
        query_budget=2,
        max_result_rows=10,
        max_result_bytes=32,
    )
    try:
        with pytest.raises(QueryDenied, match="byte budget"):
            CapsuleQuery(capsule.root).execute("news@1", "items")
    finally:
        capsule.cleanup()


def test_query_audit_persists_result_bytes_and_elapsed_time(tmp_path: Path):
    capsule = build_capsule(
        label="agent",
        instructions="inspect data",
        subject=ResearchSubject(code="600519"),
        boundary=ResearchBoundary(as_of=datetime(2026, 8, 6, tzinfo=timezone.utc)),
        inputs={"news@1": {"items": [{"summary": "ok"}]}},
        output_schema={},
        query_budget=1,
        max_result_rows=10,
        max_result_bytes=1024,
    )
    try:
        CapsuleQuery(capsule.root).execute("news@1", "items")
        record = capsule.query_log_path.read_text(encoding="utf-8").strip()
        assert '"result_bytes"' in record
        assert '"duration_ms"' in record
        resumed = CapsuleQuery(capsule.root)
        assert resumed.audit.returned_bytes > 0
    finally:
        capsule.cleanup()


def test_query_rejects_path_like_references_even_when_not_declared():
    capsule = build_capsule(
        label="agent",
        instructions="inspect data",
        subject=ResearchSubject(code="600519"),
        boundary=ResearchBoundary(as_of=datetime(2026, 8, 6, tzinfo=timezone.utc)),
        inputs={"news@1": {"items": []}},
        output_schema={},
        query_budget=1,
        max_result_rows=10,
    )
    try:
        with pytest.raises(QueryDenied, match="invalid|declared"):
            CapsuleQuery(capsule.root).execute("../outside@1")
    finally:
        capsule.cleanup()


def test_query_budget_is_atomic_across_concurrent_processes():
    capsule = build_capsule(
        label="agent",
        instructions="inspect data",
        subject=ResearchSubject(code="600519"),
        boundary=ResearchBoundary(as_of=datetime(2026, 8, 6, tzinfo=timezone.utc)),
        inputs={"news@1": {"items": [{"summary": "one"}]}},
        output_schema={},
        query_budget=1,
        max_result_rows=10,
    )
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(target=_concurrent_query, args=(str(capsule.root), start, results))
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
        assert sorted(results.get(timeout=2) for _ in processes) == ["denied", "passed"]
        assert len(capsule.query_log_path.read_text(encoding="utf-8").splitlines()) == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()
        capsule.cleanup()


def test_ndjson_query_aggregates_large_inputs_with_bounded_python_memory(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    source = tmp_path / "large-query.jsonl.gz"
    with source.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            for index in range(100_000):
                compressed.write(
                    canonical_json(
                        {
                            "kind": "bar",
                            "row": {
                                "code": f"{index % 5000:06d}",
                                "trade_date": "2026-08-05",
                                "close": index % 100,
                            },
                        }
                    )
                    + b"\n"
                )
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
                    "summary": {"row_count": 100_000},
                },
            }
        },
        output_schema={},
        query_budget=2,
        max_result_rows=10,
        max_result_bytes=1024,
        artifact_store=store,
    )
    try:
        tracemalloc.start()
        tracemalloc.reset_peak()
        result = CapsuleQuery(capsule.root).execute(
            "bars@1", "aggregate", metric="mean", value_field="close"
        )
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert result == {"metric": "mean", "value_field": "close", "count": 100_000, "value": 49.5}
        assert peak < 16 * 1024 * 1024
        ranked = CapsuleQuery(capsule.root).execute(
            "bars@1", "rank", field="close", direction="desc", limit=3
        )
        assert [item["close"] for item in ranked] == [99, 99, 99]
        assert [item["rank"] for item in ranked] == [1, 2, 3]
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        capsule.cleanup()


def test_ndjson_row_query_stops_at_the_budget_before_unbounded_collection(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    source = tmp_path / "budget-query.jsonl.gz"
    with source.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            for index in range(4):
                compressed.write(canonical_json({"kind": "bar", "row": {"close": index}}) + b"\n")
            compressed.write(b"this malformed tail must never be reached\n")
    backing = store.adopt_file(source)
    capsule = build_capsule(
        label="agent",
        instructions="inspect data",
        subject=ResearchSubject(code="600519"),
        boundary=ResearchBoundary(as_of=datetime(2026, 8, 6, tzinfo=timezone.utc)),
        inputs={
            "bars@1": {
                "product": "bars@1",
                "artifact_hash": "b" * 64,
                "payload": {
                    "__a_hunter_query_artifact__": backing.content_hash,
                    "__a_hunter_query_format__": "ndjson-v1",
                    "summary": {"row_count": 4},
                },
            }
        },
        output_schema={},
        query_budget=1,
        max_result_rows=3,
        artifact_store=store,
    )
    try:
        with pytest.raises(QueryDenied, match="row budget"):
            CapsuleQuery(capsule.root).execute("bars@1", "rows")
    finally:
        capsule.cleanup()


def test_ndjson_streaming_operations_are_exact_and_include_absences(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    source = tmp_path / "operations.jsonl.gz"
    records = [
        {"kind": "bar", "row": {"code": "000001", "trade_date": "2026-08-04", "close": 10, "change_pct": -1, "sector": "A"}},
        {"kind": "bar", "row": {"code": "000002", "trade_date": "2026-08-04", "close": 20, "change_pct": 2, "sector": "B"}},
        {"kind": "bar", "row": {"code": "000001", "trade_date": "2026-08-05", "close": 30, "change_pct": 3, "sector": "A"}},
        {"kind": "bar", "row": {"code": "000002", "trade_date": "2026-08-05", "close": 40, "change_pct": 0, "sector": "B"}},
        {"kind": "absence", "row": {"code": "000003", "trade_date": "2026-08-05", "reason": "suspended"}},
    ]
    with source.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            for record in records:
                compressed.write(canonical_json(record) + b"\n")
    backing = store.adopt_file(source)
    capsule = build_capsule(
        label="agent",
        instructions="inspect data",
        subject=ResearchSubject(code="600519"),
        boundary=ResearchBoundary(as_of=datetime(2026, 8, 6, tzinfo=timezone.utc)),
        inputs={
            "bars@1": {
                "product": "bars@1",
                "artifact_hash": "c" * 64,
                "payload": {
                    "__a_hunter_query_artifact__": backing.content_hash,
                    "__a_hunter_query_format__": "ndjson-v1",
                    "summary": {"row_count": 4, "absence_count": 1},
                },
            }
        },
        output_schema={},
        query_budget=4,
        max_result_rows=20,
        max_result_bytes=20_000,
        artifact_store=store,
    )
    try:
        query = CapsuleQuery(capsule.root)
        grouped = query.execute(
            "bars@1", "group_by", group_by="sector", metric="median", value_field="close"
        )
        ranked = query.execute("bars@1", "rank", field="change_pct", direction="desc", limit=2)
        window = query.execute(
            "bars@1",
            "window_compare",
            field="close",
            metric="mean",
            current_start="2026-08-05",
            current_end="2026-08-05",
            previous_start="2026-08-04",
            previous_end="2026-08-04",
        )
        absences = query.execute("bars@1", "absences")

        assert grouped["groups"] == [
            {"key": "A", "count": 2, "value": 20.0},
            {"key": "B", "count": 2, "value": 30.0},
        ]
        assert [row["change_pct"] for row in ranked] == [3, 2]
        assert window["current"]["value"] == 35.0
        assert window["previous"]["value"] == 15.0
        assert absences == [{"code": "000003", "trade_date": "2026-08-05", "reason": "suspended"}]
    finally:
        capsule.cleanup()
