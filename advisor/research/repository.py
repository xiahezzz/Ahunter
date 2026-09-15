from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterator, Sequence
import hashlib

from advisor.db.migrate import migrate_database
from advisor.db.repository import connect
from advisor.research.artifacts import ArtifactRef
from advisor.research.contracts import ResearchBoundary, ResearchScope, ResearchSubject, VersionRef
from advisor.research.market_time import a_share_date


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_value(value: dict | str | None) -> str:
    if value is None:
        return "{}"
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as error:
            raise ValueError("audit metadata must be valid JSON") from error
        if not isinstance(parsed, dict):
            raise ValueError("audit metadata must be a JSON object")
        return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if not isinstance(value, dict):
        raise TypeError("audit metadata must be a dictionary or JSON object")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_STATUS_TRANSITIONS = {
    "pending": frozenset({"running", "blocked", "failed", "cancelled"}),
    "running": frozenset({"passed", "blocked", "failed", "cancelled"}),
    "passed": frozenset(),
    "blocked": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


REQUEST_STATUSES = frozenset({"queued", "running", "passed", "partial", "blocked", "failed", "cancelled"})
REQUEST_TERMINAL_STATUSES = frozenset({"passed", "partial", "blocked", "failed", "cancelled"})
_REQUEST_TRANSITIONS = {
    "queued": frozenset({"running", "cancelled", "blocked", "failed"}),
    "running": REQUEST_TERMINAL_STATUSES,
    "passed": frozenset(),
    "partial": frozenset(),
    "blocked": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


@dataclass(frozen=True)
class ResearchRequest:
    request_id: str
    team: VersionRef
    scope: ResearchScope
    subject: ResearchSubject
    origin: str
    requested_at: datetime
    accepted_at: datetime
    boundary: ResearchBoundary | None
    status: str
    cancel_requested: bool
    phase: str
    agents_completed: int
    agents_total: int
    decision_stage: str | None
    reason_code: str | None
    cycle_id: str | None
    report_json_hash: str | None
    report_markdown_hash: str | None
    published_at: datetime | None
    rerun_of: str | None
    claimed_by: str | None
    claimed_at: datetime | None
    last_updated_at: datetime
    finished_at: datetime | None
    mode: str = "team"


class ResearchRequestOwnershipLost(RuntimeError):
    """A claimed Request was recovered and claimed by another executor."""


@dataclass(frozen=True)
class ResearchRecord:
    record_id: str
    request_id: str
    team: VersionRef
    scope: ResearchScope
    subject: ResearchSubject
    origin: str
    requested_at: datetime
    accepted_at: datetime
    boundary: ResearchBoundary | None
    status: str
    phase: str
    reason_code: str | None
    cycle_id: str | None
    report_json_hash: str | None
    report_markdown_hash: str | None
    published_at: datetime | None
    rerun_of: str | None
    mode: str = "team"


class ResearchRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    @classmethod
    def open(cls, db_path: Path) -> ResearchRepository:
        migrate_database(db_path)
        return cls(connect(db_path))

    def experiment_store(self, *, artifacts=None):
        """Historical experiment storage shares this control-plane connection."""
        from advisor.research.experiments.repository import ExperimentStore
        return ExperimentStore(self.connection, artifacts=artifacts)

    def open_peer(self) -> ResearchRepository | None:
        """Open an independent connection to this file-backed control plane.

        Service heartbeats must not share the executor connection: a long
        SQLite transaction in the Cycle must never make a background renewal
        commit somebody else's transaction.  In-memory fixtures have no
        process-safe peer path and intentionally return ``None``.
        """
        rows = self.connection.execute("PRAGMA database_list").fetchall()
        main_path = next((row[2] for row in rows if row[1] == "main" and isinstance(row[2], str) and row[2]), None)
        if not isinstance(main_path, str) or main_path == ":memory:":
            return None
        return ResearchRepository(connect(Path(main_path)))

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def record_artifact(self, ref: ArtifactRef, *, relative_path: str, retention_class: str = "standard") -> None:
        self.connection.execute(
            """
            INSERT INTO research_artifacts(
              content_hash, media_type, byte_size, relative_path,
              retention_class, availability, created_at
            ) VALUES (?, ?, ?, ?, ?, 'available', ?)
            ON CONFLICT(content_hash) DO UPDATE SET
              media_type = excluded.media_type,
              byte_size = excluded.byte_size,
              relative_path = excluded.relative_path,
              retention_class = excluded.retention_class,
              availability = 'available'
            """,
            (ref.content_hash, ref.media_type, ref.byte_size, relative_path, retention_class, _now()),
        )

    def query_backing_migration(self, source_artifact_hash: str) -> dict | None:
        row = self.connection.execute(
            """
            SELECT backing_artifact_hash, migrated_envelope_json
            FROM research_query_backing_migrations
            WHERE source_artifact_hash = ?
            """,
            (source_artifact_hash,),
        ).fetchone()
        if row is None:
            return None
        envelope = json.loads(row[1])
        if not isinstance(envelope, dict):
            raise ValueError("query backing migration envelope is invalid")
        return {"backing_artifact_hash": str(row[0]), "envelope": envelope}

    def record_query_backing_migration(
        self,
        *,
        source_artifact_hash: str,
        backing_artifact_hash: str,
        migrated_envelope: dict,
    ) -> None:
        envelope_json = json.dumps(
            migrated_envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        existing = self.connection.execute(
            """
            SELECT backing_artifact_hash, migrated_envelope_json
            FROM research_query_backing_migrations
            WHERE source_artifact_hash = ?
            """,
            (source_artifact_hash,),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != (backing_artifact_hash, envelope_json):
                raise ValueError("query backing migration already differs")
            return
        self.connection.execute(
            """
            INSERT INTO research_query_backing_migrations(
              source_artifact_hash, backing_artifact_hash, migrated_envelope_json, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (source_artifact_hash, backing_artifact_hash, envelope_json, _now()),
        )

    def create_batch(
        self,
        *,
        batch_id: str,
        as_of: str,
        team_refs: list[str],
        subject_refs: list[str],
        execution_policy_ref: str,
    ) -> None:
        existing = self.connection.execute(
            "SELECT as_of, team_refs_json, subject_refs_json, execution_policy_ref FROM research_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        team_refs_json = json.dumps(sorted(team_refs), separators=(",", ":"))
        subject_refs_json = json.dumps(sorted(subject_refs), separators=(",", ":"))
        expected = (as_of, team_refs_json, subject_refs_json, execution_policy_ref)
        if existing is not None:
            if tuple(existing) != expected:
                raise ValueError(f"batch_id already exists with different immutable inputs: {batch_id}")
            return
        self.connection.execute(
            """
            INSERT INTO research_batches(
              batch_id, as_of, status, team_refs_json, subject_refs_json,
              execution_policy_ref, created_at
            ) VALUES (?, ?, 'pending', ?, ?, ?, ?)
            """,
            (batch_id, as_of, team_refs_json, subject_refs_json, execution_policy_ref, _now()),
        )

    def create_cycle(
        self,
        *,
        cycle_id: str,
        subject_code: str,
        subject_name: str | None,
        as_of: str,
        fingerprint: str,
        batch_id: str | None = None,
    ) -> None:
        existing = self.connection.execute(
            "SELECT batch_id, subject_code, subject_name, as_of, cycle_fingerprint FROM research_cycles WHERE cycle_id = ?",
            (cycle_id,),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != (batch_id, subject_code, subject_name, as_of, fingerprint):
                raise ValueError(f"cycle_id already exists with different immutable inputs: {cycle_id}")
            return
        self.connection.execute(
            """
            INSERT INTO research_cycles(
              cycle_id, batch_id, subject_code, subject_name, as_of,
              status, cycle_fingerprint, created_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (cycle_id, batch_id, subject_code, subject_name, as_of, fingerprint, _now()),
        )

    def create_run(self, *, run_id: str, cycle_id: str, team_ref: str) -> None:
        existing = self.connection.execute(
            "SELECT cycle_id, team_ref FROM research_runs WHERE research_run_id = ?", (run_id,)
        ).fetchone()
        if existing is not None:
            if tuple(existing) != (cycle_id, team_ref):
                raise ValueError(f"research_run_id already exists with different scope: {run_id}")
            return
        self.connection.execute(
            "INSERT INTO research_runs(research_run_id, cycle_id, team_ref, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
            (run_id, cycle_id, team_ref, _now()),
        )

    def set_status(
        self,
        table: str,
        identifier_column: str,
        identifier: str,
        status: str,
        *,
        message: str | None = None,
        finished: bool = False,
    ) -> None:
        allowed_tables = {
            "research_batches": "batch_id",
            "research_cycles": "cycle_id",
            "research_runs": "research_run_id",
            "research_invocations": "invocation_key",
            "research_stage_runs": "stage_run_id",
        }
        if allowed_tables.get(table) != identifier_column:
            raise ValueError("unsupported research status table")
        if status not in {"pending", "running", "passed", "blocked", "failed", "cancelled"}:
            raise ValueError(f"invalid research status: {status}")
        existing = self.connection.execute(
            f"SELECT status FROM {table} WHERE {identifier_column} = ?", (identifier,)
        ).fetchone()
        if existing is None:
            raise ValueError(f"unknown research status record: {identifier}")
        terminal = {"passed", "blocked", "failed", "cancelled"}
        current = existing[0]
        if current in terminal and status != current:
            raise ValueError(f"terminal research status is immutable: {identifier}")
        if current in terminal:
            return
        if status != current and status not in _STATUS_TRANSITIONS[current]:
            raise ValueError(f"invalid status transition: {current} -> {status}")
        if finished and status not in terminal:
            raise ValueError("only terminal research statuses may be finished")
        field = ", finished_at = ?" if finished else ""
        values: list[object] = [status]
        if finished:
            values.append(_now())
        values.extend([message, identifier])
        self.connection.execute(
            f"UPDATE {table} SET status = ?{field}, message = ? WHERE {identifier_column} = ?",
            values,
        )

    def record_snapshot(
        self,
        *,
        snapshot_id: str,
        cycle_id: str,
        subject_code: str,
        as_of: str,
        product_refs: list[str],
        product_hashes: dict[str, str],
        unavailable: dict[str, str] | None = None,
        snapshot_hash: str,
        sealed: bool,
    ) -> None:
        existing = self.connection.execute(
            "SELECT cycle_id, subject_code, as_of, status, product_refs_json, product_hashes_json, unavailable_json, snapshot_hash FROM research_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        product_refs_json = json.dumps(sorted(product_refs), separators=(",", ":"))
        product_hashes_json = json.dumps(product_hashes, sort_keys=True, separators=(",", ":"))
        unavailable_json = json.dumps(unavailable or {}, sort_keys=True, separators=(",", ":"))
        if existing is not None:
            expected = (cycle_id, subject_code, as_of, "sealed" if sealed else "building", product_refs_json, product_hashes_json, unavailable_json, snapshot_hash)
            actual = tuple(existing)
            if actual != expected:
                raise ValueError(f"snapshot_id already exists with different immutable content: {snapshot_id}")
            self.connection.execute(
                "UPDATE research_cycles SET snapshot_id = ? WHERE cycle_id = ? AND (snapshot_id IS NULL OR snapshot_id = ?)",
                (snapshot_id, cycle_id, snapshot_id),
            )
            return
        self.connection.execute(
            """
            INSERT INTO research_snapshots(
              snapshot_id, cycle_id, subject_code, as_of, status,
              product_refs_json, product_hashes_json, unavailable_json, snapshot_hash,
              created_at, sealed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                cycle_id,
                subject_code,
                as_of,
                "sealed" if sealed else "building",
                product_refs_json,
                product_hashes_json,
                unavailable_json,
                snapshot_hash,
                _now(),
                _now() if sealed else None,
            ),
        )
        self.connection.execute(
            "UPDATE research_cycles SET snapshot_id = ? WHERE cycle_id = ? AND snapshot_id IS NULL",
            (snapshot_id, cycle_id),
        )

    def record_snapshot_product(
        self,
        *,
        snapshot_id: str,
        product_ref: str,
        artifact_hash: str,
        quality_status: str,
        provider_attempts: list[dict[str, object]],
    ) -> None:
        existing = self.connection.execute(
            "SELECT artifact_hash, quality_status, provider_attempts_json FROM research_snapshot_products WHERE snapshot_id = ? AND product_ref = ?",
            (snapshot_id, product_ref),
        ).fetchone()
        attempts_json = json.dumps(provider_attempts, sort_keys=True, separators=(",", ":"))
        if existing is not None:
            if tuple(existing) != (artifact_hash, quality_status, attempts_json):
                raise ValueError("snapshot product cannot be changed after sealing")
            return
        self.connection.execute(
            """
            INSERT INTO research_snapshot_products(
              snapshot_id, product_ref, artifact_hash, quality_status, provider_attempts_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                product_ref,
                artifact_hash,
                quality_status,
                attempts_json,
            ),
        )

    def record_scope_snapshot(
        self,
        *,
        snapshot_id: str,
        cycle_id: str,
        subject: ResearchSubject,
        as_of: str,
        product_refs: list[str],
        product_hashes: dict[str, str],
        unavailable: dict[str, str] | None = None,
        snapshot_hash: str,
        sealed: bool,
    ) -> None:
        """Persist an explicit-scope Snapshot without a synthetic code.

        Security Cycles continue to use their historical tables for backwards
        compatibility. This companion index is used by Market requests and
        lets a recovered service reopen exactly the sealed artifacts.
        """
        if not subject.is_market:
            raise ValueError("scope snapshot persistence currently requires a Market Subject")
        existing = self.connection.execute(
            """
            SELECT cycle_id, scope, subject_code, subject_name, as_of, status,
                   product_refs_json, product_hashes_json, unavailable_json, snapshot_hash
            FROM research_scope_snapshots WHERE snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchone()
        product_refs_json = json.dumps(sorted(product_refs), separators=(",", ":"))
        product_hashes_json = json.dumps(product_hashes, sort_keys=True, separators=(",", ":"))
        unavailable_json = json.dumps(unavailable or {}, sort_keys=True, separators=(",", ":"))
        expected = (
            cycle_id,
            subject.scope.value,
            subject.code,
            subject.name,
            as_of,
            "sealed" if sealed else "building",
            product_refs_json,
            product_hashes_json,
            unavailable_json,
            snapshot_hash,
        )
        if existing is not None:
            if tuple(existing) != expected:
                raise ValueError(f"scope snapshot_id already exists with different immutable content: {snapshot_id}")
            return
        self.connection.execute(
            """
            INSERT INTO research_scope_snapshots(
              snapshot_id, cycle_id, scope, subject_code, subject_name, as_of, status,
              product_refs_json, product_hashes_json, unavailable_json, snapshot_hash,
              created_at, sealed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                cycle_id,
                subject.scope.value,
                subject.code,
                subject.name,
                as_of,
                "sealed" if sealed else "building",
                product_refs_json,
                product_hashes_json,
                unavailable_json,
                snapshot_hash,
                _now(),
                _now() if sealed else None,
            ),
        )

    def record_scope_snapshot_product(
        self,
        *,
        snapshot_id: str,
        product_ref: str,
        artifact_hash: str,
        quality_status: str,
        provider_attempts: list[dict[str, object]],
    ) -> None:
        existing = self.connection.execute(
            """
            SELECT artifact_hash, quality_status, provider_attempts_json
            FROM research_scope_snapshot_products
            WHERE snapshot_id = ? AND product_ref = ?
            """,
            (snapshot_id, product_ref),
        ).fetchone()
        attempts_json = json.dumps(provider_attempts, sort_keys=True, separators=(",", ":"))
        if existing is not None:
            if tuple(existing) != (artifact_hash, quality_status, attempts_json):
                raise ValueError("scope snapshot product cannot be changed after sealing")
            return
        self.connection.execute(
            """
            INSERT INTO research_scope_snapshot_products(
              snapshot_id, product_ref, artifact_hash, quality_status, provider_attempts_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (snapshot_id, product_ref, artifact_hash, quality_status, attempts_json),
        )

    def scope_snapshot_for_cycle(
        self,
        cycle_id: str,
        *,
        scope: ResearchScope = ResearchScope.market,
    ) -> dict[str, object] | None:
        """Return one sealed explicit-scope Snapshot and its immutable products."""
        row = self.connection.execute(
            """
            SELECT snapshot_id, scope, subject_code, subject_name, as_of, status,
                   unavailable_json, snapshot_hash
            FROM research_scope_snapshots
            WHERE cycle_id = ? AND scope = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (cycle_id, scope.value),
        ).fetchone()
        if row is None:
            return None
        try:
            subject = ResearchSubject(scope=ResearchScope(str(row[1])), code=row[2], name=row[3])
            boundary = ResearchBoundary(as_of=_parse_time(row[4]))
            unavailable = json.loads(row[6])
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("scope snapshot is invalid") from error
        if row[5] != "sealed" or not isinstance(unavailable, dict) or not isinstance(row[7], str):
            raise ValueError("scope snapshot is not sealed")
        products = self.connection.execute(
            """
            SELECT product_ref, artifact_hash, quality_status, provider_attempts_json
            FROM research_scope_snapshot_products
            WHERE snapshot_id = ? ORDER BY product_ref
            """,
            (row[0],),
        ).fetchall()
        return {
            "snapshot_id": str(row[0]),
            "subject": subject,
            "boundary": boundary,
            "unavailable": unavailable,
            "snapshot_hash": row[7],
            "products": tuple(tuple(item) for item in products),
        }

    def create_scope_invocation(
        self,
        *,
        invocation_key: str,
        cycle_id: str,
        subject: ResearchSubject,
        agent_ref: str,
        as_of: str,
        input_hashes: list[str],
        policy_ref: str,
    ) -> None:
        if not subject.is_market:
            raise ValueError("scope invocation persistence currently requires a Market Subject")
        input_hashes_json = json.dumps(sorted(input_hashes), separators=(",", ":"))
        existing = self.connection.execute(
            """
            SELECT cycle_id, scope, agent_ref, as_of, input_hashes_json, policy_ref
            FROM research_scope_invocations WHERE invocation_key = ?
            """,
            (invocation_key,),
        ).fetchone()
        expected = (cycle_id, subject.scope.value, agent_ref, as_of, input_hashes_json, policy_ref)
        if existing is not None:
            if tuple(existing) != expected:
                raise ValueError("scope invocation key already exists with different inputs")
            return
        self.connection.execute(
            """
            INSERT INTO research_scope_invocations(
              invocation_key, cycle_id, scope, agent_ref, as_of, input_hashes_json,
              policy_ref, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (invocation_key, cycle_id, subject.scope.value, agent_ref, as_of, input_hashes_json, policy_ref, _now()),
        )

    def start_scope_invocation(self, invocation_key: str) -> None:
        row = self.connection.execute(
            "SELECT status FROM research_scope_invocations WHERE invocation_key = ?", (invocation_key,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown scope invocation: {invocation_key}")
        if row[0] in {"running", "passed"}:
            return
        if row[0] in {"blocked", "failed", "cancelled"}:
            raise ValueError(f"terminal scope invocation is immutable: {invocation_key}")
        self.connection.execute(
            "UPDATE research_scope_invocations SET status = 'running', message = NULL WHERE invocation_key = ?",
            (invocation_key,),
        )

    def scope_invocation_cache(self, invocation_key: str) -> tuple[str, str | None] | None:
        row = self.connection.execute(
            "SELECT status, finding_hash FROM research_scope_invocations WHERE invocation_key = ?",
            (invocation_key,),
        ).fetchone()
        return (str(row[0]), str(row[1]) if isinstance(row[1], str) else None) if row is not None else None

    def scope_invocation_diagnostic(self, invocation_key: str) -> tuple[str, str | None] | None:
        """Return the immutable terminal status and its bounded diagnostic."""

        row = self.connection.execute(
            "SELECT status, message FROM research_scope_invocations WHERE invocation_key = ?",
            (invocation_key,),
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), str(row[1]) if isinstance(row[1], str) else None

    def record_scope_finding(
        self,
        invocation_key: str,
        *,
        output_hash: str,
        message: str | None = None,
        query_log_hash: str | None = None,
    ) -> None:
        existing = self.connection.execute(
            "SELECT status, finding_hash, query_log_hash FROM research_scope_invocations WHERE invocation_key = ?",
            (invocation_key,),
        ).fetchone()
        if existing is None:
            raise ValueError(f"unknown scope invocation: {invocation_key}")
        if existing[0] == "passed":
            if existing[1] != output_hash or existing[2] != query_log_hash:
                raise ValueError("scope Finding is immutable")
            return
        if existing[0] != "running":
            raise ValueError(f"invalid scope invocation status: {existing[0]}")
        self.connection.execute(
            """
            UPDATE research_scope_invocations
            SET status = 'passed', finding_hash = ?, query_log_hash = ?, finished_at = ?, message = ?
            WHERE invocation_key = ?
            """,
            (output_hash, query_log_hash, _now(), message, invocation_key),
        )

    def record_scope_invocation_failure(
        self,
        invocation_key: str,
        *,
        status: str,
        message: str,
        query_log_hash: str | None = None,
    ) -> None:
        if status not in {"blocked", "failed", "cancelled"}:
            raise ValueError("scope invocation failure must be terminal")
        existing = self.connection.execute(
            "SELECT status, message, query_log_hash FROM research_scope_invocations WHERE invocation_key = ?",
            (invocation_key,),
        ).fetchone()
        if existing is None:
            raise ValueError(f"unknown scope invocation: {invocation_key}")
        if existing[0] in {"passed", "blocked", "failed", "cancelled"}:
            if (
                existing[0] != status
                or (existing[1] or "") != message[:500]
                or existing[2] != query_log_hash
            ):
                raise ValueError(f"terminal scope invocation is immutable: {invocation_key}")
            return
        if existing[0] != "running":
            raise ValueError(f"invalid scope invocation status: {existing[0]}")
        self.connection.execute(
            """
            UPDATE research_scope_invocations
            SET status = ?, query_log_hash = ?, finished_at = ?, message = ?
            WHERE invocation_key = ?
            """,
            (status, query_log_hash, _now(), message[:500], invocation_key),
        )

    def create_scope_attempt(
        self,
        *,
        invocation_key: str,
        attempt_number: int,
        capsule_hash: str,
        policy_json: dict | str | None = None,
        cli_version: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        usage_json: dict | str | None = None,
    ) -> str:
        """Create one immutable retry audit row for a Market Invocation."""
        attempt_id = "scope-attempt-" + hashlib.sha256(
            f"{invocation_key}|{attempt_number}".encode("utf-8")
        ).hexdigest()[:32]
        policy_value = _json_value(policy_json)
        usage_value = _json_value(usage_json)
        existing = self.connection.execute(
            """
            SELECT capsule_hash, policy_json, cli_version, model, reasoning_effort, usage_json
            FROM research_scope_invocation_attempts WHERE attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        expected = (capsule_hash, policy_value, cli_version, model, reasoning_effort, usage_value)
        if existing is not None:
            if tuple(existing) != expected:
                raise ValueError("scope attempt number already exists with a different capsule")
            return attempt_id
        self.connection.execute(
            """
            INSERT INTO research_scope_invocation_attempts(
              attempt_id, invocation_key, attempt_number, status, capsule_hash, started_at,
              policy_json, cli_version, model, reasoning_effort, usage_json
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                invocation_key,
                attempt_number,
                capsule_hash,
                _now(),
                policy_value,
                cli_version,
                model,
                reasoning_effort,
                usage_value,
            ),
        )
        return attempt_id

    def finish_scope_attempt(
        self,
        *,
        attempt_id: str,
        status: str,
        output_hash: str | None = None,
        error_class: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        if status not in {"passed", "failed", "blocked", "cancelled"}:
            raise ValueError("invalid scope attempt status")
        existing = self.connection.execute(
            "SELECT status, output_hash FROM research_scope_invocation_attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if existing is None:
            raise ValueError(f"unknown scope attempt: {attempt_id}")
        if existing[0] != "running":
            if existing[0] != status or existing[1] != output_hash:
                raise ValueError("accepted scope Attempt is immutable")
            return
        self.connection.execute(
            """
            UPDATE research_scope_invocation_attempts
            SET status = ?, output_hash = ?, error_class = ?, finished_at = ?, duration_ms = ?
            WHERE attempt_id = ?
            """,
            (status, output_hash, error_class, _now(), duration_ms, attempt_id),
        )

    def create_invocation(
        self,
        *,
        invocation_key: str,
        cycle_id: str,
        agent_ref: str,
        subject_code: str,
        as_of: str,
        input_hashes: list[str],
        policy_ref: str,
    ) -> None:
        existing = self.connection.execute(
            "SELECT cycle_id, agent_ref, subject_code, as_of, input_hashes_json, policy_ref FROM research_invocations WHERE invocation_key = ?",
            (invocation_key,),
        ).fetchone()
        input_hashes_json = json.dumps(sorted(input_hashes), separators=(",", ":"))
        if existing is not None:
            expected = (cycle_id, agent_ref, subject_code, as_of, input_hashes_json, policy_ref)
            if tuple(existing) != expected:
                raise ValueError("invocation key already exists with different inputs")
            return
        self.connection.execute(
            """
            INSERT INTO research_invocations(
              invocation_key, cycle_id, agent_ref, subject_code, as_of,
              input_hashes_json, policy_ref, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                invocation_key,
                cycle_id,
                agent_ref,
                subject_code,
                as_of,
                input_hashes_json,
                policy_ref,
                _now(),
            ),
        )

    def create_attempt(
        self,
        *,
        invocation_key: str,
        attempt_number: int,
        capsule_hash: str,
        policy_json: dict | str | None = None,
        cli_version: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        usage_json: dict | str | None = None,
    ) -> str:
        attempt_id = "attempt-" + hashlib.sha256(
            f"{invocation_key}|{attempt_number}".encode("utf-8")
        ).hexdigest()[:32]
        policy_value = _json_value(policy_json)
        usage_value = _json_value(usage_json)
        existing = self.connection.execute(
            "SELECT capsule_hash, policy_json, cli_version, model, reasoning_effort, usage_json FROM research_invocation_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if existing is not None:
            if tuple(existing) != (capsule_hash, policy_value, cli_version, model, reasoning_effort, usage_value):
                raise ValueError("attempt number already exists with a different capsule")
            return attempt_id
        self.connection.execute(
            """
            INSERT INTO research_invocation_attempts(
              attempt_id, invocation_key, attempt_number, status, capsule_hash, started_at,
              policy_json, cli_version, model, reasoning_effort, usage_json
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)
            """,
            (attempt_id, invocation_key, attempt_number, capsule_hash, _now(), policy_value, cli_version, model, reasoning_effort, usage_value),
        )
        return attempt_id

    def finish_attempt(
        self,
        *,
        attempt_id: str,
        status: str,
        output_hash: str | None = None,
        error_class: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        existing = self.connection.execute(
            "SELECT status, output_hash FROM research_invocation_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if existing is None:
            raise ValueError(f"unknown attempt: {attempt_id}")
        if existing[0] != "running":
            if existing[0] != status or existing[1] != output_hash:
                raise ValueError("accepted Attempt is immutable")
            return
        self.connection.execute(
            """
            UPDATE research_invocation_attempts
            SET status = ?, output_hash = ?, error_class = ?, finished_at = ?, duration_ms = ?
            WHERE attempt_id = ?
            """,
            (status, output_hash, error_class, _now(), duration_ms, attempt_id),
        )

    def record_finding(
        self,
        invocation_key: str,
        *,
        output_hash: str,
        message: str | None = None,
        query_log_hash: str | None = None,
    ) -> None:
        existing = self.connection.execute(
            "SELECT status, finding_hash, query_log_hash FROM research_invocations WHERE invocation_key = ?",
            (invocation_key,),
        ).fetchone()
        if existing is None:
            raise ValueError(f"unknown research invocation: {invocation_key}")
        if existing[0] == "passed":
            if existing[1] != output_hash:
                raise ValueError("Finding is immutable")
            if existing[2] != query_log_hash:
                raise ValueError("Finding is immutable")
            return
        if existing[0] in {"blocked", "failed", "cancelled"}:
            raise ValueError(f"terminal research status is immutable: {invocation_key}")
        if existing[0] != "running":
            raise ValueError(f"invalid status transition: {existing[0]} -> passed")
        self.connection.execute(
            "UPDATE research_invocations SET status = 'passed', finding_hash = ?, query_log_hash = ?, finished_at = ?, message = ? WHERE invocation_key = ?",
            (output_hash, query_log_hash, _now(), message, invocation_key),
        )

    def record_invocation_failure(
        self,
        invocation_key: str,
        *,
        status: str,
        message: str,
        query_log_hash: str | None = None,
    ) -> None:
        if status not in {"blocked", "failed", "cancelled"}:
            raise ValueError("invocation failure must be terminal")
        existing = self.connection.execute(
            "SELECT status, message, query_log_hash FROM research_invocations WHERE invocation_key = ?",
            (invocation_key,),
        ).fetchone()
        if existing is None:
            raise ValueError(f"unknown research invocation: {invocation_key}")
        if existing[0] in {"passed", "blocked", "failed", "cancelled"}:
            if (
                existing[0] != status
                or (existing[1] or "") != message[:500]
                or existing[2] != query_log_hash
            ):
                raise ValueError(f"terminal research status is immutable: {invocation_key}")
            return
        if existing[0] != "running":
            raise ValueError(f"invalid status transition: {existing[0]} -> {status}")
        self.connection.execute(
            "UPDATE research_invocations SET status = ?, query_log_hash = ?, finished_at = ?, message = ? WHERE invocation_key = ?",
            (status, query_log_hash, _now(), message[:500], invocation_key),
        )

    def record_stage_run(
        self, *, stage_run_id: str, research_run_id: str, stage_name: str, stage_order: int,
        input_hashes: list[str], output_hash: str | None = None,
        status: str = "passed", message: str | None = None,
    ) -> None:
        inputs_json = json.dumps(sorted(input_hashes), separators=(",", ":"))
        existing = self.connection.execute(
            "SELECT research_run_id, stage_name, stage_order, status, input_hashes_json, output_hash, message FROM research_stage_runs WHERE stage_run_id = ?",
            (stage_run_id,),
        ).fetchone()
        expected = (research_run_id, stage_name, stage_order, status, inputs_json, output_hash, message)
        if existing is not None:
            if tuple(existing) != expected:
                raise ValueError("stage run is immutable")
            return
        self.connection.execute(
            """
            INSERT INTO research_stage_runs(
              stage_run_id, research_run_id, stage_name, stage_order, status,
              input_hashes_json, output_hash, created_at, message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (stage_run_id, research_run_id, stage_name, stage_order, status, inputs_json, output_hash, _now(), message),
        )

    def record_stage_attempt(
        self,
        *,
        stage_attempt_id: str,
        stage_run_id: str,
        attempt_number: int,
        status: str,
        capsule_hash: str,
        output_hash: str | None = None,
        error_class: str | None = None,
        duration_ms: int | None = None,
        policy_json: dict | str | None = None,
        cli_version: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        usage_json: dict | str | None = None,
    ) -> None:
        policy_value = _json_value(policy_json)
        usage_value = _json_value(usage_json)
        existing = self.connection.execute(
            "SELECT stage_run_id, attempt_number, status, capsule_hash, output_hash, error_class, duration_ms, policy_json, cli_version, model, reasoning_effort, usage_json FROM research_stage_attempts WHERE stage_attempt_id = ?",
            (stage_attempt_id,),
        ).fetchone()
        expected = (stage_run_id, attempt_number, status, capsule_hash, output_hash, error_class, duration_ms, policy_value, cli_version, model, reasoning_effort, usage_value)
        if existing is not None:
            if tuple(existing) != expected:
                raise ValueError("Decision Stage Attempt is immutable")
            return
        self.connection.execute(
            """
            INSERT INTO research_stage_attempts(
              stage_attempt_id, stage_run_id, attempt_number, status, capsule_hash,
              output_hash, error_class, started_at, finished_at, duration_ms,
              policy_json, cli_version, model, reasoning_effort, usage_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stage_attempt_id,
                stage_run_id,
                attempt_number,
                status,
                capsule_hash,
                output_hash,
                error_class,
                _now(),
                _now(),
                duration_ms,
                policy_value,
                cli_version,
                model,
                reasoning_effort,
                usage_value,
            ),
        )

    def record_team_conclusion(
        self, *, conclusion_hash: str, research_run_id: str, team_ref: str, subject_code: str,
        as_of: str, stance: str, conviction: str, quality_status: str,
    ) -> None:
        existing = self.connection.execute(
            "SELECT research_run_id, team_ref, subject_code, as_of, stance, conviction, quality_status FROM research_team_conclusions WHERE conclusion_hash = ?",
            (conclusion_hash,),
        ).fetchone()
        expected = (research_run_id, team_ref, subject_code, as_of, stance, conviction, quality_status)
        if existing is not None:
            if tuple(existing) != expected:
                raise ValueError("Team Conclusion hash is already bound to another scope")
            return
        self.connection.execute(
            """
            INSERT INTO research_team_conclusions(
              conclusion_hash, research_run_id, team_ref, subject_code, as_of,
              stance, conviction, quality_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (conclusion_hash, research_run_id, team_ref, subject_code, as_of, stance, conviction, quality_status, _now()),
        )

    def record_report(
        self, *, report_id: str, cycle_id: str, team_ref: str, report_kind: str,
        json_hash: str | None, markdown_hash: str | None, status: str,
    ) -> None:
        existing = self.connection.execute(
            "SELECT cycle_id, team_ref, report_kind, json_hash, markdown_hash, status FROM research_reports WHERE report_id = ?",
            (report_id,),
        ).fetchone()
        expected = (cycle_id, team_ref, report_kind, json_hash, markdown_hash, status)
        if existing is not None:
            if tuple(existing) != expected:
                raise ValueError("Research Report is immutable")
            return
        self.connection.execute(
            "INSERT INTO research_reports(report_id, cycle_id, team_ref, report_kind, json_hash, markdown_hash, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (report_id, cycle_id, team_ref, report_kind, json_hash, markdown_hash, status, _now()),
        )

    def set_run_conclusion(self, run_id: str, conclusion_hash: str) -> None:
        existing = self.connection.execute(
            "SELECT conclusion_hash FROM research_runs WHERE research_run_id = ?",
            (run_id,),
        ).fetchone()
        if existing is None:
            raise ValueError(f"unknown research run: {run_id}")
        if existing[0] is not None and existing[0] != conclusion_hash:
            raise ValueError("Team Conclusion is immutable")
        self.connection.execute(
            "UPDATE research_runs SET conclusion_hash = ? WHERE research_run_id = ?",
            (conclusion_hash, run_id),
        )

    def record_review(
        self, *, review_id: str, conclusion_hash: str, team_ref: str, subject_code: str,
        as_of: str, status: str, outcome: str | None, review_hash: str | None,
    ) -> None:
        existing = self.connection.execute(
            "SELECT conclusion_hash, team_ref, subject_code, as_of, status, outcome, review_hash FROM research_reviews WHERE review_id = ?",
            (review_id,),
        ).fetchone()
        expected = (conclusion_hash, team_ref, subject_code, as_of, status, outcome, review_hash)
        if existing is not None:
            if tuple(existing) != expected:
                raise ValueError("Review is immutable")
            return
        self.connection.execute(
            "INSERT INTO research_reviews(review_id, conclusion_hash, team_ref, subject_code, as_of, status, outcome, review_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (review_id, conclusion_hash, team_ref, subject_code, as_of, status, outcome, review_hash, _now()),
        )

    # Request/Record control-plane methods.  Callers deliberately do not
    # compose SQL: this is the only mutable truth for new research work.
    def submit_request(
        self,
        *,
        team_ref: str | VersionRef,
        subject: ResearchSubject,
        origin: str,
        submission_identity: str,
        requested_at: datetime | None = None,
        accepted_at: datetime | None = None,
        boundary: ResearchBoundary | None = None,
        rerun_of: str | None = None,
    ) -> ResearchRequest:
        team = VersionRef.parse(team_ref)
        _validate_request_origin(origin)
        _validate_submission_identity(submission_identity)
        requested = _request_time(requested_at)
        accepted = _request_time(accepted_at) if accepted_at is not None else requested
        if accepted < requested:
            raise ValueError("accepted time cannot be earlier than requested time")
        if boundary is not None and boundary.as_of < requested and origin != "legacy":
            raise ValueError("Research Boundary cannot be earlier than requested time")
        # The installed database may still carry the original ISO-text CHECK
        # constraint.  Equal instants with different UTC offsets do not sort
        # chronologically as text (03:00+00:00 sorts before 08:00+08:00), so
        # persist all per-request monotonic timestamps in the request's
        # original offset.  Datetime equality and public semantics remain
        # instant-based.
        accepted_for_storage = accepted.astimezone(requested.tzinfo)
        boundary_for_storage = (
            boundary.as_of.astimezone(requested.tzinfo) if boundary is not None else None
        )
        if rerun_of is not None:
            parent = self.get_request(rerun_of)
            if parent.status not in REQUEST_TERMINAL_STATUSES:
                raise ValueError("only terminal Research Request may be rerun")
        submission_key = hashlib.sha256(submission_identity.encode("utf-8")).hexdigest()
        request_id = f"request-{submission_key[:32]}"
        now = _now()
        fixed_material = (
            str(team), team.id, team.version, subject.scope.value, subject.code, subject.name, origin, rerun_of,
        )
        material = (
            str(team), team.id, team.version, subject.scope.value, subject.code, subject.name,
            origin, requested.isoformat(), accepted_for_storage.isoformat(),
            boundary_for_storage.isoformat() if boundary_for_storage is not None else None, rerun_of,
        )
        existing = self.connection.execute(
            """
            SELECT team_ref, team_id, team_version, scope, subject_code, subject_name,
                   origin, rerun_of
            FROM research_requests WHERE submission_key = ?
            """,
            (submission_key,),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != fixed_material:
                raise ValueError("submission identity was already used for different Request inputs")
            return self.get_request(request_id)
        self.connection.execute(
            """
            INSERT INTO research_requests(
              request_id, submission_key, team_ref, team_id, team_version, scope,
              subject_code, subject_name, origin, requested_at, accepted_at, boundary_at,
              status, phase, rerun_of, last_updated_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'queued', ?, ?, ?)
            """,
            (request_id, submission_key, *material, now, now),
        )
        self._record_request_event(request_id, "queued", "queued", 0, 0, None, None, now)
        return self.get_request(request_id)

    def get_request(self, request_id: str) -> ResearchRequest:
        row = self.connection.execute(
            """
            SELECT request_id, team_ref, scope, subject_code, subject_name, origin,
                   requested_at, accepted_at, boundary_at, status, cancel_requested,
                   phase, agents_completed, agents_total, decision_stage, reason_code,
                   cycle_id, report_json_hash, report_markdown_hash, published_at, rerun_of,
                   claimed_by, claimed_at, last_updated_at, finished_at
            FROM research_requests WHERE request_id = ?
            """,
            (request_id,),
        ).fetchone()
        if row is None:
            raise ValueError("unknown Research Request")
        is_lagent = self.connection.execute("SELECT 1 FROM research_lagent_requests WHERE request_id=?", (request_id,)).fetchone()
        return replace(_request_from_row(row), mode="lagent" if is_lagent else "team")

    def claim_next_request(self, owner_id: str, *, claimed_at: datetime | None = None) -> ResearchRequest | None:
        if not isinstance(owner_id, str) or not owner_id.strip() or len(owner_id) > 160:
            raise ValueError("invalid Research Service owner")
        now = _request_time(claimed_at).isoformat() if claimed_at is not None else _now()
        from .work_queue import next_work
        head = next_work(self.connection)
        if head is None or head.kind != "request" or head.state != "queued":
            return None
        # One conditional UPDATE picks the next row and changes its status in
        # the same SQLite statement.  Independent connections cannot claim it
        # twice; scheduled work yields to every queued manual origin.
        cursor = self.connection.execute(
            """
            UPDATE research_requests
            SET status = 'running', phase = 'preflight', claimed_by = ?, claimed_at = ?,
                last_updated_at = ?
            WHERE request_id = (
              SELECT request_id FROM research_requests
              WHERE status = 'queued' AND cancel_requested = 0 AND request_id = ?
              ORDER BY CASE WHEN origin = 'scheduled' THEN 1 ELSE 0 END,
                       julianday(accepted_at) ASC, request_id ASC
              LIMIT 1
            ) AND status = 'queued' AND cancel_requested = 0
            RETURNING request_id
            """,
            (owner_id, now, now, head.work_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        request = self.get_request(row[0])
        self._record_request_event(
            request.request_id, request.status, request.phase, request.agents_completed,
            request.agents_total, request.decision_stage, request.reason_code, now,
        )
        return request

    def require_request_claim(self, request_id: str, claimed_by: str) -> ResearchRequest:
        """Fence a service write to its exact opaque claim identity.

        Service lease ownership and an individual Request claim are separate:
        after a lease handoff the new owner may recover and re-claim the same
        row before the former executor's next heartbeat.  The durable claim
        identity is therefore the authority for Request mutations.
        """
        if not isinstance(claimed_by, str) or not claimed_by.strip():
            raise ValueError("invalid Research Request claim owner")
        current = self.get_request(request_id)
        if current.status != "running" or current.claimed_by != claimed_by:
            raise ResearchRequestOwnershipLost("Research Request claim ownership changed")
        work = self.connection.execute("SELECT state,claimed_by,service_owner_id FROM research_work_queue WHERE work_id=? AND kind='request'", (request_id,)).fetchone()
        if work is None or work[0] != "running" or work[1] != claimed_by:
            raise ResearchRequestOwnershipLost("shared Research queue claim changed")
        if work[2] is not None:
            service = self.connection.execute("SELECT owner_id FROM research_service_leases WHERE lease_name='research-service'").fetchone()
            if service is None or service[0] != work[2]:
                raise ResearchRequestOwnershipLost("shared Research Service ownership changed")
        return current

    def set_request_boundary(
        self,
        request_id: str,
        boundary: ResearchBoundary,
        *,
        expected_claimed_by: str | None = None,
        updated_at: datetime | None = None,
    ) -> ResearchRequest:
        current = self.get_request(request_id)
        if expected_claimed_by is not None:
            self.require_request_claim(request_id, expected_claimed_by)
        if current.status not in {"queued", "running"}:
            raise ValueError("terminal Research Request boundary is immutable")
        if boundary.as_of < current.requested_at:
            raise ValueError("Research Boundary cannot be earlier than requested time")
        if current.boundary is not None:
            if current.boundary != boundary:
                raise ValueError("Research Boundary is immutable")
            return current
        now = _request_time(updated_at).isoformat() if updated_at is not None else _now()
        boundary_for_storage = boundary.as_of.astimezone(current.requested_at.tzinfo).isoformat()
        updated = self.connection.execute(
            """
            UPDATE research_requests SET boundary_at = ?, last_updated_at = ?
            WHERE request_id = ? AND boundary_at IS NULL
              AND (? IS NULL OR (status = 'running' AND claimed_by = ?))
            """,
            (boundary_for_storage, now, request_id, expected_claimed_by, expected_claimed_by),
        )
        if updated.rowcount != 1:
            if expected_claimed_by is not None:
                self.require_request_claim(request_id, expected_claimed_by)
            current = self.get_request(request_id)
            if current.boundary is not None:
                if current.boundary != boundary:
                    raise ValueError("Research Boundary is immutable")
                return current
            raise ValueError("Research Request boundary state changed")
        request = self.get_request(request_id)
        self._record_request_event(
            request.request_id, request.status, request.phase, request.agents_completed,
            request.agents_total, request.decision_stage, request.reason_code, now,
        )
        return request

    def update_request_progress(
        self,
        request_id: str,
        *,
        phase: str,
        agents_completed: int = 0,
        agents_total: int = 0,
        decision_stage: str | None = None,
        reason_code: str | None = None,
        expected_claimed_by: str | None = None,
        updated_at: datetime | None = None,
    ) -> ResearchRequest:
        current = self.get_request(request_id)
        if expected_claimed_by is not None:
            self.require_request_claim(request_id, expected_claimed_by)
        if current.status not in {"queued", "running"}:
            raise ValueError("terminal Research Request progress is immutable")
        _validate_progress(phase, agents_completed, agents_total, decision_stage, reason_code)
        now = _request_time(updated_at).isoformat() if updated_at is not None else _now()
        updated = self.connection.execute(
            """
            UPDATE research_requests
            SET phase = ?, agents_completed = ?, agents_total = ?, decision_stage = ?,
                reason_code = ?, last_updated_at = ?
            WHERE request_id = ?
              AND (? IS NULL OR (status = 'running' AND claimed_by = ?))
            """,
            (
                phase, agents_completed, agents_total, decision_stage, reason_code, now,
                request_id, expected_claimed_by, expected_claimed_by,
            ),
        )
        if updated.rowcount != 1:
            if expected_claimed_by is not None:
                self.require_request_claim(request_id, expected_claimed_by)
            raise ValueError("Research Request progress state changed")
        request = self.get_request(request_id)
        self._record_request_event(
            request.request_id, request.status, request.phase, request.agents_completed,
            request.agents_total, request.decision_stage, request.reason_code, now,
        )
        return request

    def request_cancel(self, request_id: str, *, updated_at: datetime | None = None) -> ResearchRequest:
        now = _request_time(updated_at).isoformat() if updated_at is not None else _now()
        # Do not branch on a stale read.  A queue claimer may promote this
        # row between a caller's initial read and its queued cancellation
        # UPDATE; in that case we must set the running cancellation signal
        # rather than accidentally return a still-running Request.
        for _attempt in range(2):
            queued = self.connection.execute(
                """
                UPDATE research_requests
                SET status = 'cancelled', phase = 'cancelled', reason_code = 'cancelled',
                    cancel_requested = 1, last_updated_at = ?, finished_at = ?
                WHERE request_id = ? AND status = 'queued'
                """,
                (now, now, request_id),
            )
            if queued.rowcount == 1:
                request = self.get_request(request_id)
                self._record_request_event(
                    request_id, request.status, request.phase, 0, 0, None, request.reason_code, now
                )
                self._materialize_record(request_id, now)
                return request
            running = self.connection.execute(
                """
                UPDATE research_requests
                SET cancel_requested = 1, last_updated_at = ?
                WHERE request_id = ? AND status = 'running'
                """,
                (now, request_id),
            )
            if running.rowcount == 1:
                request = self.get_request(request_id)
                self._record_request_event(
                    request_id, request.status, request.phase, request.agents_completed,
                    request.agents_total, request.decision_stage, "cancel_requested", now,
                )
                return request
            current = self.get_request(request_id)
            if current.status in REQUEST_TERMINAL_STATUSES:
                raise ValueError("terminal Research Request cannot be cancelled")
            # A concurrent writer can only leave this row queued here. Retry
            # once to make the queued→running handoff cancellation-safe.
        raise ValueError("Research Request cancellation state changed")

    def complete_request(
        self,
        request_id: str,
        *,
        status: str,
        phase: str = "complete",
        reason_code: str | None = None,
        cycle_id: str | None = None,
        report_json_hash: str | None = None,
        report_markdown_hash: str | None = None,
        published_at: datetime | None = None,
        expected_claimed_by: str | None = None,
        updated_at: datetime | None = None,
    ) -> ResearchRequest:
        if status not in REQUEST_TERMINAL_STATUSES:
            raise ValueError("Research Request completion must be terminal")
        current = self.get_request(request_id)
        if current.status in REQUEST_TERMINAL_STATUSES:
            if expected_claimed_by is not None:
                return current
            if current.status != status:
                raise ValueError("terminal Research Request is immutable")
            return current
        if expected_claimed_by is not None:
            self.require_request_claim(request_id, expected_claimed_by)
        if current.status != "running":
            raise ValueError("only running Research Request may complete")
        if status in {"passed", "partial"} and (not _valid_hash(report_json_hash) or not _valid_hash(report_markdown_hash)):
            raise ValueError("published Research Request requires verified report hashes")
        if status not in {"passed", "partial"} and (report_json_hash is not None or report_markdown_hash is not None):
            raise ValueError("non-published Research Request cannot carry report hashes")
        _validate_progress(phase, current.agents_completed, current.agents_total, current.decision_stage, reason_code)
        now = _request_time(updated_at).isoformat() if updated_at is not None else _now()
        publication = _request_time(published_at).isoformat() if published_at is not None else (now if status in {"passed", "partial"} else None)
        updated = self.connection.execute(
            """
            UPDATE research_requests
            SET status = ?, phase = ?, reason_code = ?, cycle_id = ?, report_json_hash = ?,
                report_markdown_hash = ?, published_at = ?, last_updated_at = ?, finished_at = ?
            WHERE request_id = ? AND status = 'running'
              AND (? = 'cancelled' OR cancel_requested = 0)
              AND (? IS NULL OR claimed_by = ?)
            """,
            (
                status, phase, reason_code, cycle_id, report_json_hash, report_markdown_hash,
                publication, now, now, request_id, status, expected_claimed_by, expected_claimed_by,
            ),
        )
        if updated.rowcount != 1:
            # Cancellation wins even if it arrived after the caller's first
            # state read and immediately before this terminal update.  The
            # conditional UPDATE keeps that decision in the database rather
            # than depending on service-process timing.
            cancelled = self.connection.execute(
                """
                UPDATE research_requests
                SET status = 'cancelled', phase = 'cancelled', reason_code = 'cancelled',
                    cycle_id = COALESCE(cycle_id, ?), report_json_hash = NULL,
                    report_markdown_hash = NULL, published_at = NULL,
                    last_updated_at = ?, finished_at = ?
                WHERE request_id = ? AND status = 'running' AND cancel_requested = 1
                  AND (? IS NULL OR claimed_by = ?)
                """,
                (cycle_id, now, now, request_id, expected_claimed_by, expected_claimed_by),
            )
            if cancelled.rowcount != 1:
                current = self.get_request(request_id)
                if expected_claimed_by is not None:
                    if current.status in REQUEST_TERMINAL_STATUSES:
                        return current
                    self.require_request_claim(request_id, expected_claimed_by)
                if current.status == "cancelled" or (current.status in REQUEST_TERMINAL_STATUSES and current.status == status):
                    return current
                raise ValueError("Research Request completion state changed")
        request = self.get_request(request_id)
        self._record_request_event(
            request_id, request.status, request.phase, request.agents_completed,
            request.agents_total, request.decision_stage, request.reason_code, now,
        )
        self._materialize_record(request_id, now)
        return request

    def rerun_request(
        self,
        request_id: str,
        *,
        submission_identity: str,
        requested_at: datetime | None = None,
        accepted_at: datetime | None = None,
    ) -> ResearchRequest:
        source = self.get_request(request_id)
        if source.status not in REQUEST_TERMINAL_STATUSES:
            raise ValueError("only terminal Research Request may be rerun")
        rerun = self.submit_request(
            team_ref=source.team,
            subject=source.subject,
            origin=source.origin,
            submission_identity=submission_identity,
            requested_at=requested_at,
            accepted_at=accepted_at,
            boundary=None,
            rerun_of=source.request_id,
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO research_lagent_requests "
            "SELECT ?, config_version, task, config_json, products_json FROM research_lagent_requests WHERE request_id=?",
            (rerun.request_id, source.request_id),
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO research_request_policies SELECT ?, policy_json FROM research_request_policies "
            "WHERE request_id=? AND EXISTS (SELECT 1 FROM research_lagent_requests WHERE request_id=?)",
            (rerun.request_id, source.request_id, source.request_id),
        )
        return self.get_request(rerun.request_id)

    def current_requests(self) -> tuple[ResearchRequest, ...]:
        rows = self.connection.execute(
            """
            SELECT request_id FROM research_requests
            WHERE status IN ('running', 'queued')
            ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END,
                     CASE WHEN origin = 'scheduled' THEN 1 ELSE 0 END,
                     julianday(accepted_at) ASC, request_id ASC
            """
        ).fetchall()
        return tuple(self.get_request(row[0]) for row in rows)

    def recover_interrupted_requests(self, owner_id: str, *, updated_at: datetime | None = None) -> tuple[ResearchRequest, ...]:
        """Return abandoned running work to the durable queue without erasing progress.

        This method is called only after the caller owns the singleton Service
        lease.  A resumed executor receives the original Request ID, Boundary,
        and phase, so idempotent Cycle/Snapshot checkpoints can be reused.
        """
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("invalid Research Service owner")
        now = _request_time(updated_at).isoformat() if updated_at is not None else _now()
        rows = self.connection.execute(
            """
            SELECT request_id FROM research_requests
            WHERE status = 'running' AND (claimed_by IS NULL OR claimed_by <> ?)
            ORDER BY julianday(accepted_at), request_id
            """,
            (owner_id,),
        ).fetchall()
        request_ids = tuple(str(row[0]) for row in rows)
        if request_ids:
            self.connection.execute(
                """
                UPDATE research_requests
                SET status = 'queued', claimed_by = NULL, claimed_at = NULL,
                    reason_code = 'service_recovery', last_updated_at = ?
                WHERE status = 'running' AND (claimed_by IS NULL OR claimed_by <> ?)
                """,
                (now, owner_id),
            )
            for request_id in request_ids:
                restored = self.get_request(request_id)
                self._record_request_event(
                    request_id, restored.status, restored.phase, restored.agents_completed,
                    restored.agents_total, restored.decision_stage, restored.reason_code, now,
                )
        return tuple(self.get_request(request_id) for request_id in request_ids)

    def get_record(self, record_id: str) -> ResearchRecord:
        row = self.connection.execute(
            """
            SELECT record_id, request_id, team_ref, scope, subject_code, subject_name,
                   origin, requested_at, accepted_at, boundary_at, status, phase,
                   reason_code, cycle_id, report_json_hash, report_markdown_hash,
                   published_at, rerun_of
            FROM research_records WHERE record_id = ?
            """,
            (record_id,),
        ).fetchone()
        if row is None:
            raise ValueError("unknown Research Record")
        is_lagent = self.connection.execute("SELECT 1 FROM research_lagent_requests WHERE request_id=?", (row[1],)).fetchone()
        return replace(_record_from_row(row), mode="lagent" if is_lagent else "team")

    def record_for_request(self, request_id: str) -> ResearchRecord | None:
        row = self.connection.execute("SELECT record_id FROM research_records WHERE request_id = ?", (request_id,)).fetchone()
        return self.get_record(row[0]) if row is not None else None

    def list_records(
        self,
        *,
        team_id: str | None = None,
        team_ref: str | VersionRef | None = None,
        statuses: Sequence[str] | None = ("passed", "partial"),
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[ResearchRecord, ...]:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("invalid Research Record page")
        clauses: list[str] = []
        values: list[object] = []
        if team_id is not None:
            VersionRef(id=team_id, version=1)
            clauses.append("team_id = ?")
            values.append(team_id)
        if team_ref is not None:
            clauses.append("team_ref = ?")
            values.append(str(VersionRef.parse(team_ref)))
        if statuses is not None:
            normalized = tuple(statuses)
            if not normalized or any(status not in REQUEST_TERMINAL_STATUSES for status in normalized):
                raise ValueError("invalid Research Record status filter")
            clauses.append("status IN (" + ",".join("?" for _ in normalized) + ")")
            values.extend(normalized)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT record_id FROM research_records" + where +
            " ORDER BY published_at IS NULL ASC, published_at DESC, record_id DESC LIMIT ? OFFSET ?",
            (*values, limit, offset),
        ).fetchall()
        return tuple(self.get_record(row[0]) for row in rows)

    def acquire_service_lease(
        self,
        owner_id: str,
        *,
        lease_name: str = "research-service",
        now: datetime | None = None,
        ttl_seconds: int = 30,
    ) -> bool:
        if not isinstance(owner_id, str) or not owner_id.strip() or ttl_seconds < 1 or ttl_seconds > 300:
            raise ValueError("invalid Research Service lease input")
        instant = _request_time(now)
        expires = datetime.fromtimestamp(instant.timestamp() + ttl_seconds, tz=instant.tzinfo)
        # The ownership predicate belongs in the same statement as the write.
        # A SELECT followed by an UPSERT permits two fresh connections to both
        # observe an absent/expired row and report success (split brain).
        # ``julianday`` compares explicit-offset timestamps rather than their
        # lexical representation, so callers may supply equivalent UTC offsets.
        row = self.connection.execute(
            """
            INSERT INTO research_service_leases(lease_name, owner_id, acquired_at, heartbeat_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(lease_name) DO UPDATE SET owner_id = excluded.owner_id,
              acquired_at = excluded.acquired_at, heartbeat_at = excluded.heartbeat_at, expires_at = excluded.expires_at
            WHERE research_service_leases.owner_id = excluded.owner_id
               OR julianday(research_service_leases.expires_at) <= julianday(?)
            RETURNING owner_id
            """,
            (lease_name, owner_id, instant.isoformat(), instant.isoformat(), expires.isoformat(), instant.isoformat()),
        ).fetchone()
        return row is not None and row[0] == owner_id

    def heartbeat_service_lease(
        self,
        owner_id: str,
        *,
        lease_name: str = "research-service",
        now: datetime | None = None,
        ttl_seconds: int = 30,
    ) -> bool:
        instant = _request_time(now)
        expires = datetime.fromtimestamp(instant.timestamp() + ttl_seconds, tz=instant.tzinfo)
        updated = self.connection.execute(
            "UPDATE research_service_leases SET heartbeat_at = ?, expires_at = ? WHERE lease_name = ? AND owner_id = ?",
            (instant.isoformat(), expires.isoformat(), lease_name, owner_id),
        )
        return updated.rowcount == 1

    def service_lease_status(self, *, lease_name: str = "research-service", now: datetime | None = None) -> dict[str, object]:
        row = self.connection.execute(
            "SELECT owner_id, acquired_at, heartbeat_at, expires_at FROM research_service_leases WHERE lease_name = ?", (lease_name,)
        ).fetchone()
        instant = _request_time(now)
        if row is None or _parse_time(row[3]) <= instant:
            return {"online": False, "owner_id": None, "heartbeat_at": None}
        return {"online": True, "owner_id": row[0], "heartbeat_at": row[2], "acquired_at": row[1]}

    def index_legacy_cycles(self) -> int:
        """Idempotently expose immutable published Cycles as read-only Records.

        No Cycle, Snapshot, report directory, or historical timestamp is
        rewritten.  A legacy cycle without a verified published report is
        indexed as its terminal failure state rather than being given a fake
        report reference.
        """
        rows = self.connection.execute(
            """
            SELECT c.cycle_id, c.subject_code, c.subject_name, c.as_of, c.status,
                   c.created_at, r.team_ref, r.status,
                   rp.json_hash, rp.markdown_hash
            FROM research_cycles AS c
            JOIN research_runs AS r ON r.cycle_id = c.cycle_id
            LEFT JOIN research_reports AS rp
              ON rp.cycle_id = c.cycle_id AND rp.team_ref = r.team_ref
             AND rp.report_kind = 'conclusion' AND rp.status = 'complete'
            WHERE r.status IN ('passed', 'blocked', 'failed', 'cancelled')
            ORDER BY c.created_at, c.cycle_id, r.team_ref
            """
        ).fetchall()
        indexed = 0
        for row in rows:
            cycle_id, code, name, as_of, _cycle_status, created_at, team_ref, run_status, json_hash, markdown_hash = tuple(row)
            if not isinstance(code, str) or not isinstance(team_ref, str):
                continue
            requested = _parse_time(created_at) if isinstance(created_at, str) else _parse_time(as_of)
            boundary = ResearchBoundary(as_of=_parse_time(as_of))
            submission = f"legacy:{cycle_id}:{team_ref}"
            request = self.submit_request(
                team_ref=team_ref,
                subject=ResearchSubject(code=code, name=name if isinstance(name, str) else None),
                origin="legacy",
                submission_identity=submission,
                requested_at=requested,
                accepted_at=requested,
                boundary=boundary,
            )
            if request.status in REQUEST_TERMINAL_STATUSES:
                continue
            status = str(run_status)
            valid_report = _valid_hash(json_hash) and _valid_hash(markdown_hash)
            if status == "passed" and not valid_report:
                status, reason = "blocked", "publication_failed"
            elif status == "blocked":
                reason = "quality_blocked"
            elif status == "failed":
                reason = "persistence_failed"
            elif status == "cancelled":
                reason = "cancelled"
            else:
                status, reason = "blocked", "quality_blocked"
            now = _now()
            self.connection.execute(
                """
                UPDATE research_requests
                SET status = ?, phase = 'complete', reason_code = ?, cycle_id = ?,
                    report_json_hash = ?, report_markdown_hash = ?,
                    published_at = ?, last_updated_at = ?, finished_at = ?
                WHERE request_id = ? AND status = 'queued'
                """,
                (
                    status, reason, cycle_id,
                    json_hash if status == "passed" else None,
                    markdown_hash if status == "passed" else None,
                    as_of if status == "passed" else None,
                    now, now, request.request_id,
                ),
            )
            self._record_request_event(request.request_id, status, "complete", 0, 0, None, reason, now)
            self._materialize_record(request.request_id, now)
            indexed += 1
        return indexed

    # Immutable industry taxonomy storage.  A taxonomy has its own lifetime
    # because quote-source fallback must never rewrite membership truth.
    def record_industry_taxonomy(
        self,
        *,
        taxonomy_hash: str,
        as_of_date: date,
        observed_at: datetime,
        fetched_at: datetime,
        source: str,
        coverage: float,
        payload: dict[str, object],
        members: Sequence[tuple[str, str, str]],
    ) -> None:
        if not _valid_hash(taxonomy_hash):
            raise ValueError("invalid industry taxonomy hash")
        if not isinstance(as_of_date, date) or isinstance(as_of_date, datetime):
            raise ValueError("invalid industry taxonomy date")
        if observed_at.tzinfo is None or observed_at.utcoffset() is None or fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            raise ValueError("industry taxonomy times must be timezone-aware")
        if not isinstance(source, str) or not source.strip() or len(source) > 160:
            raise ValueError("invalid industry taxonomy source")
        if isinstance(coverage, bool) or not isinstance(coverage, (int, float)) or not 0 <= float(coverage) <= 1:
            raise ValueError("invalid industry taxonomy coverage")
        normalized_members = tuple(sorted((str(industry_id), str(industry_name), str(code)) for industry_id, industry_name, code in members))
        if not normalized_members or len({item[2] for item in normalized_members}) != len(normalized_members):
            raise ValueError("industry taxonomy members are invalid")
        canonical_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        existing = self.connection.execute(
            "SELECT as_of_date, payload_json FROM research_industry_taxonomy_versions WHERE taxonomy_hash = ?",
            (taxonomy_hash,),
        ).fetchone()
        if existing is not None:
            if existing[0] != as_of_date.isoformat() or existing[1] != canonical_payload:
                raise ValueError("industry taxonomy hash conflicts with immutable contents")
            return
        self.connection.execute(
            """
            INSERT INTO research_industry_taxonomy_versions(
              taxonomy_hash, as_of_date, observed_at, fetched_at, source, coverage, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                taxonomy_hash, as_of_date.isoformat(), observed_at.isoformat(), fetched_at.isoformat(),
                source, float(coverage), canonical_payload, _now(),
            ),
        )
        self.connection.executemany(
            """
            INSERT INTO research_industry_taxonomy_members(taxonomy_hash, industry_id, industry_name, code)
            VALUES (?, ?, ?, ?)
            """,
            [(taxonomy_hash, industry_id, industry_name, code) for industry_id, industry_name, code in normalized_members],
        )

    def industry_taxonomy_for(
        self,
        as_of: datetime,
        *,
        max_age_trading_days: int = 5,
    ) -> dict[str, object] | None:
        if as_of.tzinfo is None or as_of.utcoffset() is None or not 0 <= max_age_trading_days <= 30:
            raise ValueError("invalid industry taxonomy lookup")
        row = self.connection.execute(
            """
            SELECT taxonomy_hash, as_of_date, observed_at, fetched_at, source, coverage, payload_json
            FROM research_industry_taxonomy_versions
            WHERE as_of_date <= ?
            ORDER BY as_of_date DESC, created_at DESC LIMIT 1
            """,
            (a_share_date(as_of).isoformat(),),
        ).fetchone()
        if row is None:
            return None
        version_date = date.fromisoformat(str(row[1]))
        age = self._trading_day_age(version_date, a_share_date(as_of))
        if age > max_age_trading_days:
            return None
        try:
            payload = json.loads(row[6])
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("stored industry taxonomy is invalid") from error
        if not isinstance(payload, dict):
            raise ValueError("stored industry taxonomy is invalid")
        members = self.connection.execute(
            """
            SELECT industry_id, industry_name, code
            FROM research_industry_taxonomy_members WHERE taxonomy_hash = ?
            ORDER BY industry_id, code
            """,
            (row[0],),
        ).fetchall()
        return {
            "taxonomy_hash": str(row[0]),
            "as_of_date": version_date.isoformat(),
            "observed_at": str(row[2]),
            "fetched_at": str(row[3]),
            "source": str(row[4]),
            "coverage": float(row[5]),
            "payload": payload,
            "members": tuple((str(item[0]), str(item[1]), str(item[2])) for item in members),
            "age_trading_days": age,
        }

    def _trading_day_age(self, start: date, end: date) -> int:
        if end <= start:
            return 0
        try:
            rows = self.connection.execute(
                "SELECT trade_date FROM trading_sessions WHERE trade_date > ? AND trade_date <= ? ORDER BY trade_date",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
            if rows:
                return len(rows)
        except sqlite3.Error:
            pass
        # Fixture/minimal databases may not yet have the Market Daily calendar.
        # Weekdays are a conservative bounded fallback for staleness disclosure.
        current = start + timedelta(days=1)
        count = 0
        while current <= end:
            if current.weekday() < 5:
                count += 1
            current += timedelta(days=1)
        return count

    def _record_request_event(
        self,
        request_id: str,
        status: str,
        phase: str,
        agents_completed: int,
        agents_total: int,
        decision_stage: str | None,
        reason_code: str | None,
        occurred_at: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO research_request_events(
              request_id, occurred_at, status, phase, agents_completed, agents_total,
              decision_stage, reason_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (request_id, occurred_at, status, phase, agents_completed, agents_total, decision_stage, reason_code),
        )

    def _materialize_record(self, request_id: str, created_at: str) -> None:
        row = self.connection.execute(
            """
            SELECT request_id, team_ref, team_id, team_version, scope, subject_code,
                   subject_name, origin, requested_at, accepted_at, boundary_at, status,
                   phase, reason_code, cycle_id, report_json_hash, report_markdown_hash,
                   published_at, rerun_of
            FROM research_requests WHERE request_id = ?
            """,
            (request_id,),
        ).fetchone()
        if row is None or row[11] not in REQUEST_TERMINAL_STATUSES:
            raise ValueError("only terminal Research Request can become a Record")
        record_id = f"record-{request_id.removeprefix('request-')}"
        self.connection.execute(
            """
            INSERT INTO research_records(
              record_id, request_id, team_ref, team_id, team_version, scope,
              subject_code, subject_name, origin, requested_at, accepted_at, boundary_at,
              status, phase, reason_code, cycle_id, report_json_hash, report_markdown_hash,
              published_at, rerun_of, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(request_id) DO NOTHING
            """,
            (record_id, *tuple(row), created_at),
        )


_REQUEST_PHASES = frozenset({"queued", "preflight", "snapshot", "agents", "decision", "publishing", "complete", "cancelled"})
_REQUEST_REASON_CODES = frozenset({
    "cancelled", "cancel_requested", "preflight_failed", "snapshot_unavailable",
    "provider_unavailable", "taxonomy_stale", "information_unavailable", "agent_timeout",
    "agent_invalid", "quality_blocked", "pipeline_blocked", "pipeline_failed", "persistence_failed",
    "publication_failed", "service_recovery", "service_unavailable",
})


def _request_from_row(row: sqlite3.Row | tuple[object, ...]) -> ResearchRequest:
    scope = ResearchScope(row[2])
    return ResearchRequest(
        request_id=str(row[0]),
        team=VersionRef.parse(str(row[1])),
        scope=scope,
        subject=ResearchSubject(scope=scope, code=row[3], name=row[4]),
        origin=str(row[5]),
        requested_at=_parse_time(row[6]),
        accepted_at=_parse_time(row[7]),
        boundary=ResearchBoundary(as_of=_parse_time(row[8])) if row[8] is not None else None,
        status=str(row[9]),
        cancel_requested=bool(row[10]),
        phase=str(row[11]),
        agents_completed=int(row[12]),
        agents_total=int(row[13]),
        decision_stage=str(row[14]) if row[14] is not None else None,
        reason_code=str(row[15]) if row[15] is not None else None,
        cycle_id=str(row[16]) if row[16] is not None else None,
        report_json_hash=str(row[17]) if row[17] is not None else None,
        report_markdown_hash=str(row[18]) if row[18] is not None else None,
        published_at=_parse_time(row[19]) if row[19] is not None else None,
        rerun_of=str(row[20]) if row[20] is not None else None,
        claimed_by=str(row[21]) if row[21] is not None else None,
        claimed_at=_parse_time(row[22]) if row[22] is not None else None,
        last_updated_at=_parse_time(row[23]),
        finished_at=_parse_time(row[24]) if row[24] is not None else None,
    )


def _record_from_row(row: sqlite3.Row | tuple[object, ...]) -> ResearchRecord:
    scope = ResearchScope(row[3])
    return ResearchRecord(
        record_id=str(row[0]),
        request_id=str(row[1]),
        team=VersionRef.parse(str(row[2])),
        scope=scope,
        subject=ResearchSubject(scope=scope, code=row[4], name=row[5]),
        origin=str(row[6]),
        requested_at=_parse_time(row[7]),
        accepted_at=_parse_time(row[8]),
        boundary=ResearchBoundary(as_of=_parse_time(row[9])) if row[9] is not None else None,
        status=str(row[10]),
        phase=str(row[11]),
        reason_code=str(row[12]) if row[12] is not None else None,
        cycle_id=str(row[13]) if row[13] is not None else None,
        report_json_hash=str(row[14]) if row[14] is not None else None,
        report_markdown_hash=str(row[15]) if row[15] is not None else None,
        published_at=_parse_time(row[16]) if row[16] is not None else None,
        rerun_of=str(row[17]) if row[17] is not None else None,
    )


def _request_time(value: datetime | None) -> datetime:
    instant = value if value is not None else datetime.now(timezone.utc)
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Research Request time must be timezone-aware")
    return instant


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("stored Research Request time is invalid")
    try:
        instant = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("stored Research Request time is invalid") from error
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("stored Research Request time is invalid")
    return instant


def _validate_submission_identity(value: object) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("invalid Research Request submission identity")


def _validate_request_origin(value: object) -> None:
    if not isinstance(value, str) or value not in {"web", "cli", "scheduled", "legacy"}:
        raise ValueError("invalid Research Request origin")


def _validate_progress(
    phase: object,
    agents_completed: object,
    agents_total: object,
    decision_stage: object,
    reason_code: object,
) -> None:
    if not isinstance(phase, str) or phase not in _REQUEST_PHASES:
        raise ValueError("invalid Research Request phase")
    if type(agents_completed) is not int or type(agents_total) is not int or agents_completed < 0 or agents_total < 0 or agents_completed > agents_total:
        raise ValueError("invalid Research Request Agent progress")
    if decision_stage is not None and (not isinstance(decision_stage, str) or not decision_stage or len(decision_stage) > 80):
        raise ValueError("invalid Research Request Decision Stage")
    if reason_code is not None and (not isinstance(reason_code, str) or reason_code not in _REQUEST_REASON_CODES):
        raise ValueError("invalid Research Request reason code")


def _valid_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)
