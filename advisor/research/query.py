from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import fcntl
import gzip
import heapq
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import statistics
import tempfile
import time
from typing import Any, Iterator

from advisor.research.contracts import VersionRef


class QueryDenied(RuntimeError):
    pass


@dataclass
class QueryAudit:
    query_count: int = 0
    returned_rows: int = 0
    returned_bytes: int = 0


class CapsuleQuery:
    def __init__(self, capsule_root: Path) -> None:
        self.root = capsule_root.resolve()
        self._prepared_paths: dict[bytes, Path] = {}
        manifest_path = self.root / "manifest.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise QueryDenied("capsule manifest is unavailable")
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        try:
            declared_products = tuple(
                str(VersionRef.parse(item)) for item in self.manifest.get("declared_products", ())
            )
        except (TypeError, ValueError) as error:
            raise QueryDenied("capsule declared products are invalid") from error
        self.products = set(declared_products)
        query_backed = self.manifest.get("query_backed_products", ())
        if not isinstance(query_backed, list) or any(item not in self.products for item in query_backed):
            raise QueryDenied("capsule query-backed product declaration is invalid")
        self.query_backed_products = set(query_backed)
        query_formats = self.manifest.get("query_backed_formats", {})
        if (
            not isinstance(query_formats, dict)
            or any(key not in self.query_backed_products for key in query_formats)
            or any(value not in {"json", "ndjson-v1"} for value in query_formats.values())
        ):
            raise QueryDenied("capsule query-backed format declaration is invalid")
        self.query_backed_formats = {
            product: query_formats.get(product, "json") for product in self.query_backed_products
        }
        self.budget = self.manifest.get("query_budget", 0)
        self.max_rows = self.manifest.get("max_result_rows", 1)
        self.max_bytes = self.manifest.get("max_result_bytes", 2_000_000)
        self.lock_path = self.root / "query-log.lock"
        if not self.lock_path.is_file() or self.lock_path.is_symlink() or not self.lock_path.resolve().is_relative_to(self.root):
            raise QueryDenied("capsule query lock is unavailable")
        with self.lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self.audit = self._load_audit()
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _load_audit(self) -> QueryAudit:
        path = self.root / "query-log.jsonl"
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise QueryDenied("capsule query log is unavailable")
        audit = QueryAudit()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            for line in lines:
                record = json.loads(line)
                audit.query_count += 1
                audit.returned_rows += int(record.get("returned_rows", 0))
                audit.returned_bytes += int(record.get("result_bytes", 0))
        except (OSError, UnicodeDecodeError, ValueError, TypeError) as error:
            raise QueryDenied("capsule query log is invalid") from error
        return audit

    def execute(self, product_ref: str, operation: str = "get", **params: Any) -> Any:
        with self.lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                self.audit = self._load_audit()
                return self._execute_locked(product_ref, operation, params)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def prepare(self, requests: list[dict[str, Any]], *, cancel_event: Any = None):
        """Materialize only the date union of an already validated host plan.

        The immutable backing is scanned once, using bounded memory. Private
        temporary views are used only for the exact prepared requests; all
        authorization, result limits and audit writes still go through execute.
        """
        grouped: dict[str, list[dict[str, Any]]] = {}
        for request in requests:
            grouped.setdefault(request["product"], []).append(request)
        with tempfile.TemporaryDirectory(prefix="query-plan-", dir=self.root) as directory:
            try:
                for product, group in grouped.items():
                    if len(group) < 2 or self.query_backed_formats.get(product) != "ndjson-v1":
                        continue
                    windows = [_request_window(request) for request in group]
                    if any(window is None for window in windows):
                        continue
                    start = min(window[0] for window in windows if window is not None)
                    end = max(window[1] for window in windows if window is not None)
                    source = self.root / "query-data" / f"{product.replace('@', '__')}.json"
                    if not source.is_file() or source.is_symlink() or not source.resolve().is_relative_to(self.root):
                        raise QueryDenied("product view is unavailable")
                    target = Path(directory) / f"{product.replace('@', '__')}.gz"
                    with gzip.open(target, "wb", compresslevel=1) as output:
                        for index, record in enumerate(_iter_ndjson_records(source)):
                            if index % 1024 == 0 and cancel_event is not None and cancel_event.is_set():
                                raise QueryDenied("query execution cancelled")
                            trade_date = record["row"].get("trade_date")
                            if isinstance(trade_date, str) and start <= trade_date <= end:
                                output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
                    for request in group:
                        self._prepared_paths[_request_key(request)] = target
                yield
            finally:
                self._prepared_paths.clear()

    def _execute_locked(self, product_ref: str, operation: str, params: dict[str, Any]) -> Any:
        try:
            canonical_ref = str(VersionRef.parse(product_ref))
        except (TypeError, ValueError) as error:
            raise QueryDenied("product reference is invalid") from error
        if canonical_ref != product_ref or canonical_ref not in self.products:
            raise QueryDenied("product is not declared by this Capsule")
        if self.budget is not None and self.audit.query_count >= self.budget:
            raise QueryDenied("query budget exceeded")
        directory = "query-data" if canonical_ref in self.query_backed_products else "products"
        path = self.root / directory / f"{canonical_ref.replace('@', '__')}.json"
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise QueryDenied("product view is unavailable")
        started = time.monotonic()
        if self.query_backed_formats.get(canonical_ref) == "ndjson-v1":
            path = self._prepared_paths.get(_request_key({
                "product": canonical_ref, "operation": operation, "params": params,
            }), path)
            result = _stream_operation(
                path,
                operation,
                params,
                remaining_rows=None if self.max_rows is None else self.max_rows - self.audit.returned_rows,
                remaining_bytes=None if self.max_bytes is None else self.max_bytes - self.audit.returned_bytes,
            )
        else:
            try:
                with path.open("rb") as handle:
                    compressed = handle.read(2) == b"\x1f\x8b"
                if compressed:
                    with gzip.open(path, "rt", encoding="utf-8") as handle:
                        payload = json.load(handle)
                else:
                    with path.open("r", encoding="utf-8") as handle:
                        payload = json.load(handle)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise QueryDenied("product view is invalid") from error
            result = self._operation(payload, operation, params)
        rows = _row_count(result)
        result_bytes = len(json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if self.max_rows is not None and self.audit.returned_rows + rows > self.max_rows:
            raise QueryDenied("query result exceeds row budget")
        if self.max_bytes is not None and self.audit.returned_bytes + result_bytes > self.max_bytes:
            raise QueryDenied("query result exceeds byte budget")
        request = {"product": canonical_ref, "operation": operation, "params": params}
        self.audit.query_count += 1
        self.audit.returned_rows += rows
        self.audit.returned_bytes += result_bytes
        audit = {
            "product": canonical_ref,
            "operation": operation,
            "params": params,
            "query_budget": self.budget,
            "max_result_rows": self.max_rows,
            "max_result_bytes": self.max_bytes,
            "result_rows": rows,
            "returned_rows": rows,
            "result_bytes": result_bytes,
            "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
            "request": request,
            "result_hash": hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
            "total_query_count": self.audit.query_count,
            "total_returned_rows": self.audit.returned_rows,
            "total_returned_bytes": self.audit.returned_bytes,
        }
        with (self.root / "query-log.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(audit, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return result

    @staticmethod
    def _operation(payload: Any, operation: str, params: dict[str, Any]) -> Any:
        payload = _payload_view(payload)
        if operation == "get":
            return payload
        if operation == "items":
            return payload.get("items", []) if isinstance(payload, dict) else []
        if operation == "rows":
            return payload.get("rows", []) if isinstance(payload, dict) else []
        if operation in {"absences", "securities", "sessions"}:
            values = payload.get(operation, []) if isinstance(payload, dict) else []
            return _filtered_rows({"rows": values}, params) if params else values
        if operation == "search":
            terms = str(params.get("term", "")).lower()
            values = payload.get("items", []) if isinstance(payload, dict) else payload
            if not isinstance(values, list):
                return []
            return [item for item in values if terms in json.dumps(item, ensure_ascii=False).lower()]
        if operation == "filter":
            values = _filtered_rows(payload, params)
            return values
        if operation == "slice":
            values = payload.get("rows", payload.get("items", [])) if isinstance(payload, dict) else payload
            if not isinstance(values, list):
                return []
            start = max(0, int(params.get("start", 0)))
            end = min(len(values), start + max(0, int(params.get("limit", 50))))
            return values[start:end]
        if operation in {"aggregate", "group_by"}:
            return _aggregate(payload, params, grouped=operation == "group_by" or params.get("group_by") is not None)
        if operation == "breadth":
            return _breadth(payload, params)
        if operation == "rank":
            return _rank(payload, params)
        if operation in {"window", "window_compare"}:
            return _window_compare(payload, params)
        raise QueryDenied(f"unsupported query operation: {operation}")


_MAX_NDJSON_LINE_BYTES = 2_000_000


def _request_key(request: dict[str, Any]) -> bytes:
    return json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def _request_window(request: dict[str, Any]) -> tuple[str, str] | None:
    params = request["params"]
    if request["operation"] in {"window", "window_compare"}:
        starts = [params.get("current_start"), params.get("previous_start")]
        ends = [params.get("current_end"), params.get("previous_end")]
    else:
        starts, ends = [params.get("start_date")], [params.get("end_date")]
    if not all(isinstance(value, str) and _DATE.fullmatch(value) for value in starts + ends):
        return None
    return min(starts), max(ends)


def _stream_operation(
    path: Path,
    operation: str,
    params: dict[str, Any],
    *,
    remaining_rows: int | None,
    remaining_bytes: int | None,
) -> Any:
    if operation == "get":
        raise QueryDenied("streaming query product requires a bounded operation")
    if operation in {"rows", "items", "filter", "search", "slice", "absences", "securities", "sessions"}:
        return _stream_rows(
            path,
            operation,
            params,
            remaining_rows=remaining_rows,
            remaining_bytes=remaining_bytes,
        )
    if operation in {"aggregate", "group_by"}:
        return _stream_aggregate(
            path,
            params,
            grouped=operation == "group_by" or params.get("group_by") is not None,
            max_groups=None if remaining_rows is None else max(0, remaining_rows),
            remaining_bytes=remaining_bytes,
        )
    if operation == "breadth":
        return _stream_breadth(path, params)
    if operation == "rank":
        return _stream_rank(
            path,
            params,
            remaining_rows=remaining_rows,
            remaining_bytes=remaining_bytes,
        )
    if operation in {"window", "window_compare"}:
        return _stream_window_compare(path, params)
    raise QueryDenied(f"unsupported query operation: {operation}")


def _iter_ndjson_rows(path: Path, *, record_kind: str = "bar") -> Iterator[dict[str, Any]]:
    for record in _iter_ndjson_records(path):
        if record["kind"] == record_kind:
            yield record["row"]


def _iter_ndjson_records(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with gzip.open(path, "rb") as handle:
            while True:
                line = handle.readline(_MAX_NDJSON_LINE_BYTES + 1)
                if not line:
                    return
                if len(line) > _MAX_NDJSON_LINE_BYTES:
                    raise QueryDenied("query product row exceeds size limit")
                try:
                    record = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise QueryDenied("query product row is invalid") from error
                if not isinstance(record, dict) or record.get("kind") not in {
                    "bar", "absence", "security", "session",
                }:
                    raise QueryDenied("query product record is invalid")
                row = record.get("row")
                if not isinstance(row, dict):
                    raise QueryDenied("query product row is invalid")
                yield record
    except QueryDenied:
        raise
    except (OSError, EOFError) as error:
        raise QueryDenied("product view is invalid") from error


def _filter_spec(params: dict[str, Any]) -> tuple[str | None, Any, set[str] | None, str | None, str | None]:
    field = params.get("field")
    expected = params.get("equals")
    codes = params.get("codes")
    start = params.get("start_date")
    end = params.get("end_date")
    if field is not None:
        field = _field(field)
    if codes is not None:
        if (
            not isinstance(codes, list)
            or any(not isinstance(code, str) or not re.fullmatch(r"\d{6}", code) for code in codes)
        ):
            raise QueryDenied("query codes are invalid")
        code_set: set[str] | None = set(codes)
    else:
        code_set = None
    if start is not None:
        start = _date(start, name="start_date")
    if end is not None:
        end = _date(end, name="end_date")
    if isinstance(start, str) and isinstance(end, str) and start > end:
        raise QueryDenied("query date window is invalid")
    return field, expected, code_set, start, end


def _matches_filter(
    item: dict[str, Any],
    spec: tuple[str | None, Any, set[str] | None, str | None, str | None],
) -> bool:
    field, expected, code_set, start, end = spec
    if field is not None and item.get(field) != expected:
        return False
    if code_set is not None and item.get("code") not in code_set:
        return False
    if start is not None or end is not None:
        trade_date = item.get("trade_date")
        if not isinstance(trade_date, str) or not _DATE.fullmatch(trade_date):
            return False
        if start is not None and trade_date < start:
            return False
        if end is not None and trade_date > end:
            return False
    return True


def _stream_rows(
    path: Path,
    operation: str,
    params: dict[str, Any],
    *,
    remaining_rows: int | None,
    remaining_bytes: int | None,
) -> list[dict[str, Any]]:
    spec = _filter_spec(params if operation in {"filter", "absences", "securities", "sessions"} else {})
    record_kind = {
        "absences": "absence",
        "securities": "security",
        "sessions": "session",
    }.get(operation, "bar")
    term = str(params.get("term", "")).lower() if operation == "search" else None
    start = 0
    limit: int | None = None
    if operation == "slice":
        raw_start = params.get("start", 0)
        raw_limit = params.get("limit", 50)
        if type(raw_start) is not int or type(raw_limit) is not int:
            raise QueryDenied("query slice is invalid")
        start = max(0, raw_start)
        limit = max(0, raw_limit)
    result: list[dict[str, Any]] = []
    encoded_size = 2
    seen = 0
    for item in _iter_ndjson_rows(path, record_kind=record_kind):
        if not _matches_filter(item, spec):
            continue
        if term is not None and term not in json.dumps(item, ensure_ascii=False).lower():
            continue
        if seen < start:
            seen += 1
            continue
        if limit is not None and len(result) >= limit:
            break
        if remaining_rows is not None and len(result) >= remaining_rows:
            raise QueryDenied("query result exceeds row budget")
        item_bytes = len(json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        encoded_size += item_bytes + (1 if result else 0)
        if remaining_bytes is not None and encoded_size > remaining_bytes:
            raise QueryDenied("query result exceeds byte budget")
        result.append(item)
    return result


@dataclass
class _MetricState:
    count: int = 0
    numeric_count: int = 0
    total: float = 0.0
    minimum: float | None = None
    maximum: float | None = None

    def add(self, value: Any, *, numeric: bool) -> float | None:
        self.count += 1
        if not numeric:
            return None
        number = _numeric(value)
        if number is None:
            return None
        self.numeric_count += 1
        self.total += number
        self.minimum = number if self.minimum is None else min(self.minimum, number)
        self.maximum = number if self.maximum is None else max(self.maximum, number)
        return number

    def value(self, metric: str, median: float | None = None) -> float | int | None:
        if metric == "count":
            return self.count
        if not self.numeric_count or self.numeric_count != self.count:
            return None
        if metric == "sum":
            return self.total if self.numeric_count else 0.0
        if metric == "mean":
            return self.total / self.numeric_count if self.numeric_count else None
        if metric == "median":
            return median
        if metric == "min":
            return self.minimum
        if metric == "max":
            return self.maximum
        raise QueryDenied("query aggregate metric is invalid")

    def coverage(self, metric: str) -> dict[str, int]:
        if metric == "count" or self.count == self.numeric_count:
            return {}
        return {"numeric_count": self.numeric_count, "missing_count": self.count - self.numeric_count}


class _MedianSpool:
    def __init__(self, directory: Path) -> None:
        handle = tempfile.NamedTemporaryFile(prefix="a-hunter-median-", suffix=".sqlite", dir=directory, delete=False)
        handle.close()
        self.path = Path(handle.name)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=OFF")
        self.connection.execute("PRAGMA synchronous=OFF")
        self.connection.execute("CREATE TABLE values_spool(group_key TEXT NOT NULL, value REAL NOT NULL)")
        self.connection.execute("CREATE INDEX values_spool_order ON values_spool(group_key, value)")

    def add(self, group_key: str, value: float) -> None:
        self.connection.execute("INSERT INTO values_spool(group_key, value) VALUES (?, ?)", (group_key, value))

    def median(self, group_key: str, count: int) -> float | None:
        if count <= 0:
            return None
        offset = (count - 1) // 2
        limit = 2 if count % 2 == 0 else 1
        rows = self.connection.execute(
            "SELECT value FROM values_spool WHERE group_key = ? ORDER BY value LIMIT ? OFFSET ?",
            (group_key, limit, offset),
        ).fetchall()
        return sum(float(row[0]) for row in rows) / len(rows) if rows else None

    def close(self) -> None:
        self.connection.close()
        self.path.unlink(missing_ok=True)


def _aggregate_parameters(params: dict[str, Any], *, grouped: bool) -> tuple[str, str | None, str | None]:
    metric = params.get("metric", "count")
    if metric not in {"count", "sum", "mean", "median", "min", "max"}:
        raise QueryDenied("query aggregate metric is invalid")
    value_field = _field(params["value_field"], name="value_field") if "value_field" in params else None
    if metric != "count" and value_field is None:
        raise QueryDenied("query aggregate requires value_field")
    group_field = _field(params.get("group_by"), name="group_by") if grouped else None
    return metric, value_field, group_field


def _stream_aggregate(
    path: Path,
    params: dict[str, Any],
    *,
    grouped: bool,
    max_groups: int | None,
    remaining_bytes: int | None,
) -> dict[str, Any]:
    metric, value_field, group_field = _aggregate_parameters(params, grouped=grouped)
    spec = _filter_spec(params)
    groups: dict[str, _MetricState] = {}
    group_state_bytes = 0
    spool = _MedianSpool(path.parent) if metric == "median" else None
    try:
        for item in _iter_ndjson_rows(path):
            if not _matches_filter(item, spec):
                continue
            if group_field is None:
                key = ""
            else:
                raw_key = item.get(group_field)
                if raw_key is None or isinstance(raw_key, (dict, list)):
                    continue
                key = str(raw_key)
                if len(key.encode("utf-8")) > 256:
                    raise QueryDenied("query group key exceeds size limit")
            state = groups.get(key)
            if state is None:
                if grouped and max_groups is not None and len(groups) >= max_groups:
                    raise QueryDenied("query result exceeds row budget")
                projected_group_bytes = group_state_bytes + len(key.encode("utf-8")) + 512
                if grouped and remaining_bytes is not None and projected_group_bytes > remaining_bytes:
                    raise QueryDenied("query result exceeds byte budget")
                group_state_bytes = projected_group_bytes
                state = groups[key] = _MetricState()
            number = state.add(item.get(value_field) if value_field else None, numeric=metric != "count")
            if spool is not None and number is not None:
                spool.add(key, number)
        if grouped:
            result: dict[str, Any] = {
                "group_by": group_field,
                "metric": metric,
                "value_field": value_field,
                "groups": [],
            }
            encoded_size = len(
                json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
            output_groups: list[dict[str, Any]] = result["groups"]
            for key in sorted(groups):
                output = {
                    "key": key,
                    "count": groups[key].count,
                    **groups[key].coverage(metric),
                    "value": groups[key].value(
                        metric,
                        spool.median(key, groups[key].numeric_count) if spool is not None else None,
                    ),
                }
                output_size = len(
                    json.dumps(output, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                )
                encoded_size += output_size + (1 if output_groups else 0)
                if remaining_bytes is not None and encoded_size > remaining_bytes:
                    raise QueryDenied("query result exceeds byte budget")
                output_groups.append(output)
            return result
        state = groups.get("", _MetricState())
        return {
            "metric": metric,
            "value_field": value_field,
            "count": state.count,
            **state.coverage(metric),
            "value": state.value(
                metric,
                spool.median("", state.numeric_count) if spool is not None else None,
            ),
        }
    finally:
        if spool is not None:
            spool.close()


def _stream_breadth(path: Path, params: dict[str, Any]) -> dict[str, Any]:
    spec = _filter_spec({key: value for key, value in params.items() if key != "field"})
    field = _breadth_field(params)
    positive = negative = flat = missing = count = 0
    for item in _iter_ndjson_rows(path):
        if not _matches_filter(item, spec):
            continue
        count += 1
        number = _numeric(item.get(field))
        if number is None:
            missing += 1
        elif number > 0:
            positive += 1
        elif number < 0:
            negative += 1
        else:
            flat += 1
    usable = positive + negative + flat
    return {
        "field": field,
        "count": count,
        "positive": positive,
        "negative": negative,
        "flat": flat,
        "missing": missing,
        "advance_ratio": positive / usable if usable else None,
        "decline_ratio": negative / usable if usable else None,
    }


class _ReverseKey:
    def __init__(self, value: tuple[float, str, str]) -> None:
        self.value = value

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, _ReverseKey):
            return NotImplemented
        return self.value > other.value


def _stream_rank(
    path: Path,
    params: dict[str, Any],
    *,
    remaining_rows: int | None,
    remaining_bytes: int | None,
) -> list[dict[str, Any]]:
    spec = _filter_spec(
        {key: value for key, value in params.items() if key not in {"field", "direction", "limit"}}
    )
    field = _field(params.get("field", "change_pct"))
    direction = params.get("direction", "desc")
    if direction not in {"asc", "desc"}:
        raise QueryDenied("query rank direction is invalid")
    limit = params.get("limit", 50)
    if type(limit) is not int or limit < 1:
        raise QueryDenied("query rank limit is invalid")
    if remaining_rows is not None and limit > remaining_rows:
        raise QueryDenied("query result exceeds row budget")

    def sort_key(item: dict[str, Any]) -> tuple[float, str, str]:
        number = float(_numeric(item.get(field)))
        return (
            number if direction == "asc" else -number,
            str(item.get("code", "")),
            str(item.get("trade_date", "")),
        )

    selected_heap: list[tuple[_ReverseKey, int, dict[str, Any], int]] = []
    selected_bytes = 2
    sequence = 0
    for item in _iter_ndjson_rows(path):
        if not _matches_filter(item, spec) or _numeric(item.get(field)) is None:
            continue
        key = sort_key(item)
        item_bytes = len(
            json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ) + 32
        entry = (_ReverseKey(key), sequence, item, item_bytes)
        sequence += 1
        if len(selected_heap) < limit:
            projected = selected_bytes + item_bytes + (1 if selected_heap else 0)
            if remaining_bytes is not None and projected > remaining_bytes:
                raise QueryDenied("query result exceeds byte budget")
            heapq.heappush(selected_heap, entry)
            selected_bytes = projected
        elif key < selected_heap[0][0].value:
            removed = heapq.heapreplace(selected_heap, entry)
            projected = selected_bytes - removed[3] + item_bytes
            if remaining_bytes is not None and projected > remaining_bytes:
                raise QueryDenied("query result exceeds byte budget")
            selected_bytes = projected
    selected_entries = sorted(selected_heap, key=lambda entry: (sort_key(entry[2]), entry[1]))
    selected = [entry[2] for entry in selected_entries]
    return [{"rank": index + 1, **item} for index, item in enumerate(selected)]


def _stream_window_compare(path: Path, params: dict[str, Any]) -> dict[str, Any]:
    field = _field(params.get("field", "close"))
    metric = params.get("metric", "mean")
    if metric not in {"sum", "mean", "median", "min", "max", "count"}:
        raise QueryDenied("query window metric is invalid")
    current_start = _date(params.get("current_start"), name="current_start")
    current_end = _date(params.get("current_end"), name="current_end")
    previous_start = _date(params.get("previous_start"), name="previous_start")
    previous_end = _date(params.get("previous_end"), name="previous_end")
    if current_start > current_end or previous_start > previous_end:
        raise QueryDenied("query window is invalid")
    base_params = {
        key: value
        for key, value in params.items()
        if key not in {"current_start", "current_end", "previous_start", "previous_end", "metric", "field"}
    }
    spec = _filter_spec(base_params)
    states = {"current": _MetricState(), "previous": _MetricState()}
    observed_dates: dict[str, set[str]] = {"current": set(), "previous": set()}
    spool = _MedianSpool(path.parent) if metric == "median" else None
    try:
        for item in _iter_ndjson_rows(path):
            if not _matches_filter(item, spec):
                continue
            trade_date = item.get("trade_date")
            if not isinstance(trade_date, str) or not _DATE.fullmatch(trade_date):
                continue
            windows = []
            if current_start <= trade_date <= current_end:
                windows.append("current")
            if previous_start <= trade_date <= previous_end:
                windows.append("previous")
            for window in windows:
                observed_dates[window].add(trade_date)
                state = states[window]
                number = state.add(item.get(field), numeric=metric != "count")
                if spool is not None and number is not None:
                    spool.add(window, number)
        values = {
            key: state.value(
                metric,
                spool.median(key, state.numeric_count) if spool is not None else None,
            )
            for key, state in states.items()
        }
        delta = None
        if isinstance(values["current"], (int, float)) and isinstance(values["previous"], (int, float)):
            delta = values["current"] - values["previous"]
        return _window_comparison({
            "field": field,
            "metric": metric,
            "current": {
                "start_date": current_start,
                "end_date": current_end,
                "count": states["current"].count,
                **states["current"].coverage(metric),
                "value": values["current"],
            },
            "previous": {
                "start_date": previous_start,
                "end_date": previous_end,
                "count": states["previous"].count,
                **states["previous"].coverage(metric),
                "value": values["previous"],
            },
            "delta": delta,
        }, observed_dates)
    finally:
        if spool is not None:
            spool.close()


def _row_count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        if isinstance(value.get("groups"), list):
            return len(value["groups"])
        return 1
    return 0


_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _payload_view(value: Any) -> Any:
    """Unwrap the standard Agent input envelope without widening access."""
    if isinstance(value, dict) and isinstance(value.get("payload"), dict) and "product" in value:
        return value["payload"]
    return value


def _rows(payload: Any) -> list[dict[str, Any]]:
    value = payload.get("rows", payload.get("items", [])) if isinstance(payload, dict) else payload
    if not isinstance(value, list):
        raise QueryDenied("query product has no tabular rows")
    return [item for item in value if isinstance(item, dict)]


def _field(value: Any, *, name: str = "field") -> str:
    if not isinstance(value, str) or not _FIELD.fullmatch(value):
        raise QueryDenied(f"query {name} is invalid")
    return value


def _date(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise QueryDenied(f"query {name} is invalid")
    return value


def _filtered_rows(payload: Any, params: dict[str, Any]) -> list[dict[str, Any]]:
    values = _rows(payload)
    field = params.get("field")
    expected = params.get("equals")
    codes = params.get("codes")
    start = params.get("start_date")
    end = params.get("end_date")
    if field is not None:
        field = _field(field)
    if codes is not None:
        if not isinstance(codes, list) or any(not isinstance(code, str) or not re.fullmatch(r"\d{6}", code) for code in codes):
            raise QueryDenied("query codes are invalid")
        code_set = set(codes)
    else:
        code_set = None
    if start is not None:
        start = _date(start, name="start_date")
    if end is not None:
        end = _date(end, name="end_date")
    if isinstance(start, str) and isinstance(end, str) and start > end:
        raise QueryDenied("query date window is invalid")
    result = []
    for item in values:
        if field is not None and item.get(field) != expected:
            continue
        if code_set is not None and item.get("code") not in code_set:
            continue
        trade_date = item.get("trade_date")
        if start is not None or end is not None:
            if not isinstance(trade_date, str) or not _DATE.fullmatch(trade_date):
                continue
            if start is not None and trade_date < start:
                continue
            if end is not None and trade_date > end:
                continue
        result.append(item)
    return result


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _metric(rows: list[dict[str, Any]], name: str, value_field: str | None) -> float | int | None:
    if name == "count":
        return len(rows)
    if value_field is None:
        raise QueryDenied("query aggregate requires value_field")
    values = [_numeric(item.get(value_field)) for item in rows]
    numbers = [item for item in values if item is not None]
    if not numbers or len(numbers) != len(rows):
        return None
    if name == "sum":
        return sum(numbers) if numbers else 0.0
    if name == "mean":
        return statistics.fmean(numbers) if numbers else None
    if name == "median":
        return statistics.median(numbers) if numbers else None
    if name == "min":
        return min(numbers) if numbers else None
    if name == "max":
        return max(numbers) if numbers else None
    raise QueryDenied("query aggregate metric is invalid")


def _aggregate(payload: Any, params: dict[str, Any], *, grouped: bool) -> dict[str, Any]:
    values = _filtered_rows(payload, params)
    metric = params.get("metric", "count")
    if metric not in {"count", "sum", "mean", "median", "min", "max"}:
        raise QueryDenied("query aggregate metric is invalid")
    value_field = _field(params["value_field"], name="value_field") if "value_field" in params else None
    group_field = params.get("group_by")
    if grouped:
        group_field = _field(group_field, name="group_by")
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in values:
            raw = item.get(group_field)
            if raw is None or isinstance(raw, (dict, list)):
                continue
            groups.setdefault(str(raw), []).append(item)
        return {
            "group_by": group_field,
            "metric": metric,
            "value_field": value_field,
            "groups": [
                {"key": key, "count": len(groups[key]), "value": _metric(groups[key], metric, value_field),
                 **_coverage(groups[key], metric, value_field)}
                for key in sorted(groups)
            ],
        }
    return {"metric": metric, "value_field": value_field, "count": len(values), "value": _metric(values, metric, value_field),
            **_coverage(values, metric, value_field)}


def _coverage(rows: list[dict[str, Any]], metric: str, field: str | None) -> dict[str, int]:
    if metric == "count":
        return {}
    numeric_count = sum(_numeric(row.get(field)) is not None for row in rows)
    if numeric_count == len(rows):
        return {}
    return {"numeric_count": numeric_count, "missing_count": len(rows) - numeric_count}


def _breadth_field(params: dict[str, Any]) -> str:
    if params.get("field", "change_pct") != "change_pct":
        raise QueryDenied("breadth requires change_pct (percentage price change), not a price or volume level")
    return "change_pct"


def _breadth(payload: Any, params: dict[str, Any]) -> dict[str, Any]:
    field = _breadth_field(params)
    values = _filtered_rows(payload, {key: value for key, value in params.items() if key != "field"})
    positive = negative = flat = missing = 0
    for item in values:
        number = _numeric(item.get(field))
        if number is None:
            missing += 1
        elif number > 0:
            positive += 1
        elif number < 0:
            negative += 1
        else:
            flat += 1
    usable = positive + negative + flat
    return {
        "field": field,
        "count": len(values),
        "positive": positive,
        "negative": negative,
        "flat": flat,
        "missing": missing,
        "advance_ratio": positive / usable if usable else None,
        "decline_ratio": negative / usable if usable else None,
    }


def _rank(payload: Any, params: dict[str, Any]) -> list[dict[str, Any]]:
    values = _filtered_rows(payload, {key: value for key, value in params.items() if key != "field"})
    field = _field(params.get("field", "change_pct"))
    direction = params.get("direction", "desc")
    if direction not in {"asc", "desc"}:
        raise QueryDenied("query rank direction is invalid")
    limit = params.get("limit", 50)
    if type(limit) is not int or limit < 1:
        raise QueryDenied("query rank limit is invalid")
    valid = [item for item in values if _numeric(item.get(field)) is not None]
    valid.sort(
        key=lambda item: (
            _numeric(item.get(field)) if direction == "asc" else -float(_numeric(item.get(field))),
            str(item.get("code", "")),
            str(item.get("trade_date", "")),
        )
    )
    return [
        {"rank": index + 1, **item}
        for index, item in enumerate(valid[:limit])
    ]


def _window_compare(payload: Any, params: dict[str, Any]) -> dict[str, Any]:
    field = _field(params.get("field", "close"))
    metric = params.get("metric", "mean")
    if metric not in {"sum", "mean", "median", "min", "max", "count"}:
        raise QueryDenied("query window metric is invalid")
    current_start = _date(params.get("current_start"), name="current_start")
    current_end = _date(params.get("current_end"), name="current_end")
    previous_start = _date(params.get("previous_start"), name="previous_start")
    previous_end = _date(params.get("previous_end"), name="previous_end")
    if current_start > current_end or previous_start > previous_end:
        raise QueryDenied("query window is invalid")
    base_params = {key: value for key, value in params.items() if key not in {"current_start", "current_end", "previous_start", "previous_end", "metric", "field"}}
    current = _filtered_rows(payload, {**base_params, "start_date": current_start, "end_date": current_end})
    previous = _filtered_rows(payload, {**base_params, "start_date": previous_start, "end_date": previous_end})
    current_value = _metric(current, metric, None if metric == "count" else field)
    previous_value = _metric(previous, metric, None if metric == "count" else field)
    delta = None
    if isinstance(current_value, (int, float)) and isinstance(previous_value, (int, float)):
        delta = current_value - previous_value
    return _window_comparison({
        "field": field,
        "metric": metric,
        "current": {"start_date": current_start, "end_date": current_end, "count": len(current), "value": current_value,
                    **_coverage(current, metric, field)},
        "previous": {"start_date": previous_start, "end_date": previous_end, "count": len(previous), "value": previous_value,
                     **_coverage(previous, metric, field)},
        "delta": delta,
    }, {"current": {row['trade_date'] for row in current},
        "previous": {row['trade_date'] for row in previous}})


def _window_comparison(result: dict[str, Any], dates: dict[str, set[str]]) -> dict[str, Any]:
    """Expose observed coverage; it is not a claim of calendar completeness."""
    for window, observed in dates.items():
        result[window].update({
            "observed_session_count": len(observed),
            "first_observed_session": min(observed) if observed else None,
            "last_observed_session": max(observed) if observed else None,
        })
    if result["current"]["value"] is None or result["previous"]["value"] is None:
        result["comparison_issue"] = "missing_numeric_values"
        result["delta"] = None
    elif result["metric"] == "sum" and len(dates["current"]) != len(dates["previous"]):
        result["comparison_issue"] = "unequal_session_coverage"
        result["delta"] = None
    return result
