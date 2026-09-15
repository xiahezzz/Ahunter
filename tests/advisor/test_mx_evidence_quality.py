import hashlib
import json
import os
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from advisor.evidence import mx_adapter
from advisor.evidence.mx_adapter import MediaMetadata, MxEvidence, read_collector_snapshot
from advisor.evidence.service import persist_evidence


SHANGHAI = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 7, 12, 8, 30, tzinfo=SHANGHAI)
COLLECTOR_SCHEMA = Path(__file__).parents[2] / "src" / "events" / "schema.sql"


def _millis(value: datetime) -> int:
    return int(value.timestamp() * 1000)


@pytest.fixture
def write_allowed_rids(monkeypatch):
    def write(tmp_path: Path, values: list[object], *, content: str | None = None) -> Path:
        repo = tmp_path / "repo"
        path = repo / "config" / "allowed-rids.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(mx_adapter, "repo_root", lambda: repo)
        if content is None:
            content = yaml.safe_dump({"allowed_rids": values}, sort_keys=True)
        path.write_text(content, encoding="utf-8")
        return path

    return write


@pytest.fixture
def create_collector_db():
    def create(
        tmp_path: Path,
        *,
        future_event: bool = False,
        problems: bool = False,
    ) -> tuple[Path, str]:
        path = tmp_path / "events.sqlite"
        connection = sqlite3.connect(path)
        connection.executescript(COLLECTOR_SCHEMA.read_text(encoding="utf-8"))
        received = _millis(AS_OF - timedelta(minutes=20))
        future = _millis(AS_OF + timedelta(minutes=1))
        connection.execute("INSERT INTO ingest_runs VALUES (?, ?)", ("collector-run", received))
        hashes = {
            "evt-authorized-1": hashlib.sha256(b"accepted-1").hexdigest(),
            "evt-authorized-2": hashlib.sha256(b"accepted-2").hexdigest(),
            "evt-other": hashlib.sha256(b"not-authorized").hexdigest(),
        }
        rows = [
            ("evt-authorized-1", 123, received - 1_000, "关注 600519 " + "A" * 2_000),
            ("evt-authorized-2", 123, future if future_event else received, "观察 000001"),
            ("evt-other", 999, received, "private non-allowlisted content"),
        ]
        for event_id, rid, received_at, text in rows:
            connection.execute(
                """
                INSERT INTO events (
                  event_id, schema_version, rid, source_message_id, oid, received_at,
                  source_created_at, raw_payload_hash, raw_payload, raw_payload_expires_at,
                  decoded_text, parsed_content_json, content_hash, ingest_run_id
                ) VALUES (?, 1, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, '{}', ?, 'collector-run')
                """,
                (
                    event_id,
                    rid,
                    received_at,
                    received_at - 500,
                    hashlib.sha256((event_id + "-raw").encode()).hexdigest(),
                    "raw-secret-" + event_id,
                    received_at + 30 * 86_400_000,
                    text,
                    hashes[event_id],
                ),
            )
        connection.execute(
            """
            INSERT INTO media (
              event_id, rid, source_url, url_hash, content_hash, content_type, local_path, downloaded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "evt-authorized-2",
                123,
                "https://secret.invalid/image?token=hidden",
                "b" * 64,
                "c" * 64,
                "image/jpeg",
                "data/media/2026-07-12/accepted.jpg",
                received,
            ),
        )
        bucket = received - (received % 3_600_000)
        connection.executemany(
            "INSERT INTO ingest_counters (bucket_start, kind, count) VALUES (?, ?, ?)",
            [(bucket, "accepted", 3), (bucket, "ignored", 4)],
        )
        if problems:
            connection.execute(
                "INSERT INTO media_jobs VALUES (?, ?, ?, ?, 'pending', 0, ?, NULL)",
                ("evt-authorized-1", 123, "https://secret.invalid/pending", "d" * 64, received),
            )
            connection.execute(
                "INSERT INTO media_jobs VALUES (?, ?, ?, ?, 'failed', 1, ?, 'timeout')",
                ("evt-authorized-2", 123, "https://secret.invalid/failed", "e" * 64, received),
            )
            connection.execute(
                "INSERT INTO decode_failures VALUES (?, 'DecodeError', ?, 1)",
                ("f" * 64, bucket),
            )
        connection.commit()
        connection.close()
        return path, hashes["evt-authorized-2"]

    return create


def test_empty_allowed_rids_returns_inactive_blocking_snapshot(
    tmp_path, create_collector_db, write_allowed_rids
):
    db, _ = create_collector_db(tmp_path)
    allowed = write_allowed_rids(tmp_path, [])

    snapshot = read_collector_snapshot(db, allowed, as_of=AS_OF)

    assert snapshot.events == ()
    assert snapshot.quality.blocking_failure
    assert "inactive" in snapshot.quality.details


def test_only_allowlisted_accepted_rows_become_bounded_evidence(
    tmp_path, create_collector_db, write_allowed_rids
):
    db, expected_hash = create_collector_db(tmp_path)
    allowed = write_allowed_rids(tmp_path, [123])

    snapshot = read_collector_snapshot(db, allowed, as_of=AS_OF, limit=1)

    assert snapshot.allowed_rids == (123,)
    assert [event.rid for event in snapshot.events] == [123]
    assert snapshot.events[0].content_hash == expected_hash
    assert len(snapshot.events[0].summary) <= 800
    serialized = json.dumps(snapshot.events[0].to_dict())
    for forbidden in ("raw_payload", "source_url", "private non-allowlisted", "token=hidden"):
        assert forbidden not in serialized
    assert snapshot.events[0].evidence_id == hashlib.sha256(
        f"a-hunter:evidence:v1\0mx\0evt-authorized-2\0{expected_hash}".encode()
    ).hexdigest()
    assert snapshot.events[0].media[0].local_path == "data/media/2026-07-12/accepted.jpg"


@pytest.mark.parametrize(
    "values",
    [[0], [-1], [True], ["123"], [123, 123]],
)
def test_invalid_allowed_rids_fail_closed(tmp_path, create_collector_db, write_allowed_rids, values):
    db, _ = create_collector_db(tmp_path)
    allowed = write_allowed_rids(tmp_path, values)

    snapshot = read_collector_snapshot(db, allowed, as_of=AS_OF)

    assert snapshot.events == ()
    assert snapshot.quality.blocking_failure


def test_malformed_yaml_fails_closed(tmp_path, create_collector_db, write_allowed_rids):
    db, _ = create_collector_db(tmp_path)
    allowed = write_allowed_rids(tmp_path, [], content="allowed_rids: [123\n")

    snapshot = read_collector_snapshot(db, allowed, as_of=AS_OF)

    assert snapshot.events == ()
    assert snapshot.quality.blocking_failure


@pytest.mark.parametrize("target", ["database", "config"])
def test_symlinked_inputs_are_rejected(
    tmp_path, create_collector_db, write_allowed_rids, target
):
    db, _ = create_collector_db(tmp_path)
    allowed = write_allowed_rids(tmp_path, [123])
    original = db if target == "database" else allowed
    link = tmp_path / f"linked-{original.name}"
    link.symlink_to(original)

    with pytest.raises(ValueError, match="symlink"):
        read_collector_snapshot(link if target == "database" else db, link if target == "config" else allowed, as_of=AS_OF)


def test_missing_real_collector_schema_fails_closed(tmp_path, write_allowed_rids):
    db = tmp_path / "events.sqlite"
    sqlite3.connect(db).execute("CREATE TABLE events (event_id TEXT)").connection.close()
    allowed = write_allowed_rids(tmp_path, [123])

    snapshot = read_collector_snapshot(db, allowed, as_of=AS_OF)

    assert snapshot.events == ()
    assert snapshot.quality.blocking_failure
    assert "schema" in snapshot.quality.details


def test_future_collector_timestamp_is_excluded_and_blocks(
    tmp_path, create_collector_db, write_allowed_rids
):
    db, _ = create_collector_db(tmp_path, future_event=True)
    allowed = write_allowed_rids(tmp_path, [123])

    snapshot = read_collector_snapshot(db, allowed, as_of=AS_OF)

    assert [event.source_id for event in snapshot.events] == ["evt-authorized-1"]
    assert snapshot.quality.blocking_failure
    assert "future" in snapshot.quality.details


def test_pending_failed_media_and_decode_failures_block_without_exposing_payloads(
    tmp_path, create_collector_db, write_allowed_rids
):
    db, _ = create_collector_db(tmp_path, problems=True)
    allowed = write_allowed_rids(tmp_path, [123])

    snapshot = read_collector_snapshot(db, allowed, as_of=AS_OF)

    assert snapshot.quality.blocking_failure
    assert "pending" in snapshot.quality.details
    assert "secret.invalid" not in snapshot.quality.details


def test_ambiguous_counters_fail_closed(tmp_path, create_collector_db, write_allowed_rids):
    db, _ = create_collector_db(tmp_path)
    connection = sqlite3.connect(db)
    connection.execute("UPDATE ingest_counters SET count = 2 WHERE kind = 'accepted'")
    connection.commit()
    connection.close()
    allowed = write_allowed_rids(tmp_path, [123])

    snapshot = read_collector_snapshot(db, allowed, as_of=AS_OF)

    assert snapshot.quality.blocking_failure
    assert "counter" in snapshot.quality.details


def test_sensitive_values_in_decoded_summary_are_not_exposed(
    tmp_path, create_collector_db, write_allowed_rids
):
    db, _ = create_collector_db(tmp_path)
    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE events SET decoded_text = '关注 600519 token=super-secret-value' WHERE event_id = 'evt-authorized-2'"
    )
    connection.commit()
    connection.close()

    snapshot = read_collector_snapshot(db, write_allowed_rids(tmp_path, [123]), as_of=AS_OF, limit=1)

    assert "super-secret-value" not in json.dumps(snapshot.events[0].to_dict())
    assert "[redacted]" in snapshot.events[0].summary


def test_complete_auth_cookie_bearer_and_jwt_secrets_are_redacted(
    tmp_path, create_collector_db, write_allowed_rids
):
    secrets = (
        "super-secret-token",
        "session=secret-cookie",
        "standalone-bearer-secret",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzZWNyZXQifQ.signature-secret",
        "assigned-secret",
    )
    decoded = (
        "关注 600519 Authorization: \"Bearer super-secret-token\"\n"
        "Cookie: session=secret-cookie\n"
        "Basic dXNlcjpwYXNzd29yZA== "
        "Bearer standalone-bearer-secret "
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzZWNyZXQifQ.signature-secret "
        "token=assigned-secret"
    )
    db, _ = create_collector_db(tmp_path)
    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE events SET decoded_text = ? WHERE event_id = 'evt-authorized-2'",
        (decoded,),
    )
    connection.commit()
    connection.close()

    snapshot = read_collector_snapshot(
        db, write_allowed_rids(tmp_path, [123]), as_of=AS_OF, limit=1
    )
    serialized = json.dumps(snapshot.events[0].to_dict())

    assert all(secret not in serialized for secret in secrets)
    assert "Bearer" not in snapshot.events[0].summary


@pytest.mark.parametrize(
    "local_path",
    [
        "https://secret.invalid/media.jpg",
        "/data/events/media/accepted.jpg",
        "data/events/media/../secret.jpg",
    ],
)
def test_media_path_outside_collector_namespace_fails_closed(
    tmp_path, create_collector_db, write_allowed_rids, local_path
):
    db, _ = create_collector_db(tmp_path)
    connection = sqlite3.connect(db)
    connection.execute("UPDATE media SET local_path = ?", (local_path,))
    connection.commit()
    connection.close()

    snapshot = read_collector_snapshot(
        db, write_allowed_rids(tmp_path, [123]), as_of=AS_OF
    )

    assert snapshot.events == ()
    assert snapshot.quality.blocking_failure


def test_secret_or_unsafe_collector_source_id_fails_closed(
    tmp_path, create_collector_db, write_allowed_rids
):
    db, _ = create_collector_db(tmp_path)
    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE events SET event_id = ? WHERE event_id = 'evt-authorized-2'",
        ("token=source-secret\n../debug",),
    )
    connection.commit()
    connection.close()

    snapshot = read_collector_snapshot(
        db, write_allowed_rids(tmp_path, [123]), as_of=AS_OF, limit=1
    )

    assert snapshot.events == ()
    assert snapshot.quality.blocking_failure


@pytest.mark.parametrize(
    "source_id",
    [
        "session_abcdefgh",
        "event_session_abcdefgh",
        "SESSION-ID.abcdefgh",
        "sess-abcdefgh",
        "socket_id_abcdefgh",
        "event-socket-id-foo",
        "token.abcdefgh",
        "access_token_foo",
        "refresh-token.foo",
        "debug_identifier",
        "mx.debug_identifier",
        "debugger-id",
        "cdp.identifier",
        "event_api_key_foo",
        "event-secret-foo",
        "event.password.foo",
        "event_credential_foo",
        "event-id-token-foo",
        "event_cookie_foo",
        "event.authorization.foo",
        "event_sessionid_abcdefgh",
        "event_socketid_foo",
        "mx_debugidentifier",
        "event_cdptoken_foo",
        "event_apikey_foo",
        "event_idtoken_foo",
    ],
)
def test_sensitive_opaque_collector_source_id_fails_closed(
    tmp_path, create_collector_db, write_allowed_rids, source_id
):
    db, _ = create_collector_db(tmp_path)
    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE events SET event_id = ? WHERE event_id = 'evt-authorized-2'",
        (source_id,),
    )
    connection.commit()
    connection.close()

    snapshot = read_collector_snapshot(
        db, write_allowed_rids(tmp_path, [123]), as_of=AS_OF, limit=1
    )

    assert snapshot.events == ()
    assert snapshot.quality.blocking_failure


@pytest.mark.parametrize(
    "source_id",
    [
        "event_accesstoken_foo",
        "event_sessionaccesstoken_foo",
        "event_accessauthorizationtoken_foo",
        "event_debugsessionaccesstoken_foo",
        "event_refreshtoken_foo",
        "event_sessiontoken_foo",
        "event_authorizationtoken_foo",
        "event_cookietoken_foo",
        "event_debugid_foo",
        "event_authtoken_foo",
        "event_sockettoken_foo",
        "event_apikeytoken_foo",
        "event_passwordtoken_foo",
        "event_credentialtoken_foo",
        "event_secrettoken_foo",
    ],
)
def test_concatenated_sensitive_opaque_identifiers_are_rejected(source_id):
    assert not mx_adapter.valid_opaque_identifier(source_id)


@pytest.mark.parametrize(
    "source_id",
    [
        "evt-authorized-1",
        "event_author_foo",
        "event_sessionized_foo",
        "event_debuggable_foo",
    ],
)
def test_benign_opaque_identifiers_remain_valid(source_id):
    assert mx_adapter.valid_opaque_identifier(source_id)


def test_snapshot_dto_redacts_forged_sensitive_values():
    secrets = (
        "dto-auth-secret",
        "dto-basic-secret",
        "dto-bearer-secret",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJkdG8ifQ.dto-signature",
        "dto-session-secret",
        "dto-source-secret",
        "dto-media-secret",
    )
    evidence = MxEvidence(
        evidence_id="e" * 64,
        source_type="mx",
        source_id="token=dto-source-secret",
        rid=123,
        content_hash="c" * 64,
        summary=(
            'Authorization: "Bearer dto-auth-secret" Basic dto-basic-secret '
            "Bearer dto-bearer-secret " + secrets[3] + " session=dto-session-secret"
        ),
        received_at=AS_OF,
        source_created_at=None,
        media=(
            MediaMetadata(
                "d" * 64,
                "image/jpeg",
                "data/events/media/token=dto-media-secret",
                AS_OF,
            ),
        ),
    )

    serialized = json.dumps(evidence.to_dict())

    assert all(secret not in serialized for secret in secrets)
    assert evidence.to_dict()["source_id"] == "[redacted]"


def test_media_dto_serialization_never_returns_unsafe_local_path():
    media = MediaMetadata(
        "d" * 64,
        "image/jpeg",
        "data/events/media/../access_token=media-secret.jpg",
        AS_OF,
    )

    payload = media.to_dict()

    assert payload["local_path"] == "[redacted]"
    assert "media-secret" not in json.dumps(payload)


@pytest.mark.parametrize(
    "secret_text",
    [
        "access_token=access-secret",
        "refresh_token=refresh-secret",
        "session_id=session-secret",
        "socket_id=socket-secret",
    ],
)
def test_dto_redaction_covers_sensitive_compound_names(secret_text: str):
    media = MediaMetadata("d" * 64, "image/jpeg", f"data/events/media/{secret_text}", AS_OF)

    serialized = json.dumps(media.to_dict())

    assert secret_text.split("=", 1)[1] not in serialized


def test_collector_database_read_is_pinned_to_validated_descriptor(
    tmp_path, create_collector_db, write_allowed_rids, monkeypatch
):
    db, _ = create_collector_db(tmp_path)
    replacement_dir = tmp_path / "replacement"
    replacement_dir.mkdir()
    replacement, _ = create_collector_db(replacement_dir)
    replacement_connection = sqlite3.connect(replacement)
    replacement_connection.execute("DELETE FROM events")
    replacement_connection.execute("UPDATE ingest_counters SET count = 0 WHERE kind = 'accepted'")
    replacement_connection.commit()
    replacement_connection.close()
    allowed = write_allowed_rids(tmp_path, [123])
    real_connect = sqlite3.connect
    opened_uri = None

    def swap_path_then_connect(database, *args, **kwargs):
        nonlocal opened_uri
        opened_uri = database
        os.replace(replacement, db)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(mx_adapter.sqlite3, "connect", swap_path_then_connect)

    snapshot = read_collector_snapshot(db, allowed, as_of=AS_OF)

    assert opened_uri is not None and ".mx-read-" in opened_uri
    assert {event.source_id for event in snapshot.events} == {
        "evt-authorized-1",
        "evt-authorized-2",
    }


def test_snapshot_open_creates_no_files_in_read_only_collector_directory(
    tmp_path, create_collector_db, write_allowed_rids
):
    collector_dir = tmp_path / "collector"
    collector_dir.mkdir()
    db, _ = create_collector_db(collector_dir)
    allowed = write_allowed_rids(tmp_path, [123])
    before = sorted(path.name for path in collector_dir.iterdir())
    collector_dir.chmod(0o555)
    try:
        snapshot = read_collector_snapshot(db, allowed, as_of=AS_OF)
    finally:
        collector_dir.chmod(0o755)

    assert not snapshot.quality.blocking_failure
    assert sorted(path.name for path in collector_dir.iterdir()) == before


def _advisor_evidence_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE events_normalized (
          evidence_source_id TEXT PRIMARY KEY, source_type TEXT NOT NULL, source_id TEXT NOT NULL,
          code TEXT, as_of TEXT NOT NULL, summary TEXT NOT NULL, raw_ref_json TEXT NOT NULL,
          quality_status TEXT NOT NULL DEFAULT 'passed'
        );
        CREATE TABLE evidence (
          evidence_id TEXT PRIMARY KEY, run_id TEXT, code TEXT, as_of TEXT NOT NULL,
          source_type TEXT NOT NULL, source_id TEXT NOT NULL, summary TEXT NOT NULL,
          confidence REAL NOT NULL DEFAULT 0.5, facts_json TEXT NOT NULL DEFAULT '[]',
          inferences_json TEXT NOT NULL DEFAULT '[]', conflicts_json TEXT NOT NULL DEFAULT '[]',
          quality_flags_json TEXT NOT NULL DEFAULT '[]'
        );
        """
    )


def test_persist_evidence_is_idempotent_bounded_and_excludes_future(
    tmp_path, create_collector_db, write_allowed_rids
):
    db, _ = create_collector_db(tmp_path, future_event=True)
    snapshot = read_collector_snapshot(db, write_allowed_rids(tmp_path, [123]), as_of=AS_OF)
    connection = sqlite3.connect(":memory:")
    _advisor_evidence_tables(connection)

    first = persist_evidence(connection, "advisor-run", snapshot, as_of=AS_OF)
    second = persist_evidence(connection, "advisor-run", snapshot, as_of=AS_OF)

    assert first == second
    assert len(first) == 1
    assert connection.execute("SELECT count(*) FROM events_normalized").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM evidence").fetchone()[0] == 1
    normalized = connection.execute("SELECT summary, raw_ref_json FROM events_normalized").fetchone()
    assert len(normalized[0]) <= 800
    assert "raw-secret" not in normalized[1]
    assert "source_url" not in normalized[1]


@pytest.mark.parametrize("run_id", ("token=secret", "../escape", "run\nid"))
def test_persist_evidence_rejects_unsafe_run_id_before_writing(
    tmp_path, create_collector_db, write_allowed_rids, run_id: str
):
    db, _ = create_collector_db(tmp_path)
    snapshot = read_collector_snapshot(db, write_allowed_rids(tmp_path, [123]), as_of=AS_OF)
    connection = sqlite3.connect(":memory:")
    _advisor_evidence_tables(connection)

    with pytest.raises(ValueError, match="invalid run_id"):
        persist_evidence(connection, run_id, snapshot, as_of=AS_OF)

    assert connection.execute("SELECT count(*) FROM events_normalized").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM evidence").fetchone()[0] == 0


def test_persist_evidence_rejects_future_source_and_media_timestamps(
    tmp_path, create_collector_db, write_allowed_rids
):
    db, _ = create_collector_db(tmp_path)
    snapshot = read_collector_snapshot(db, write_allowed_rids(tmp_path, [123]), as_of=AS_OF)
    source_future = replace(
        snapshot.events[1], source_created_at=AS_OF + timedelta(seconds=1)
    )
    media_event = snapshot.events[0]
    assert media_event.media
    media_future = replace(
        media_event,
        media=(replace(media_event.media[0], downloaded_at=AS_OF + timedelta(seconds=1)),),
    )
    bounded_snapshot = replace(snapshot, events=(source_future, media_future))
    connection = sqlite3.connect(":memory:")
    _advisor_evidence_tables(connection)

    with pytest.raises(ValueError, match="source_created_at"):
        persist_evidence(connection, "advisor-run", bounded_snapshot, as_of=AS_OF)

    assert connection.execute("SELECT count(*) FROM events_normalized").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM evidence").fetchone()[0] == 0


def test_persist_evidence_rejects_snapshot_after_run_as_of(
    tmp_path, create_collector_db, write_allowed_rids
):
    db, _ = create_collector_db(tmp_path)
    snapshot = read_collector_snapshot(db, write_allowed_rids(tmp_path, [123]), as_of=AS_OF)
    future_snapshot = replace(snapshot, as_of=AS_OF + timedelta(seconds=1))
    connection = sqlite3.connect(":memory:")
    _advisor_evidence_tables(connection)

    with pytest.raises(ValueError, match="snapshot as_of"):
        persist_evidence(connection, "advisor-run", future_snapshot, as_of=AS_OF)

    assert connection.execute("SELECT count(*) FROM events_normalized").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM evidence").fetchone()[0] == 0


def test_persisted_summary_and_raw_refs_contain_no_complete_secrets(
    tmp_path, create_collector_db, write_allowed_rids
):
    secret = "persisted-super-secret-token"
    db, _ = create_collector_db(tmp_path)
    source = sqlite3.connect(db)
    source.execute(
        "UPDATE events SET decoded_text = ? WHERE event_id = 'evt-authorized-2'",
        (f"关注 600519 Authorization: Bearer {secret}",),
    )
    source.commit()
    source.close()
    snapshot = read_collector_snapshot(
        db, write_allowed_rids(tmp_path, [123]), as_of=AS_OF, limit=1
    )
    connection = sqlite3.connect(":memory:")
    _advisor_evidence_tables(connection)

    persist_evidence(connection, "advisor-run", snapshot, as_of=AS_OF)

    source_id, summary, raw_ref = connection.execute(
        "SELECT source_id, summary, raw_ref_json FROM events_normalized"
    ).fetchone()
    evidence_source_id, evidence_summary = connection.execute(
        "SELECT source_id, summary FROM evidence"
    ).fetchone()
    assert secret not in source_id
    assert secret not in evidence_source_id
    assert secret not in summary
    assert secret not in evidence_summary
    assert secret not in raw_ref


def test_persistence_rejects_forged_sensitive_source_id_without_writing():
    event = MxEvidence(
        evidence_id="e" * 64,
        source_type="mx",
        source_id="Bearer persistence-source-secret",
        rid=123,
        content_hash="c" * 64,
        summary="Authorization: Basic cGVyc2lzdGVuY2U6c2VjcmV0",
        received_at=AS_OF,
        source_created_at=None,
        media=(),
    )
    snapshot = mx_adapter.CollectorSnapshot(
        events=(event,), quality=object(), as_of=AS_OF, allowed_rids=(123,)
    )
    connection = sqlite3.connect(":memory:")
    _advisor_evidence_tables(connection)

    with pytest.raises(ValueError, match="source_id"):
        persist_evidence(connection, "advisor-run", snapshot, as_of=AS_OF)

    assert connection.execute("SELECT COUNT(*) FROM events_normalized").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


@pytest.mark.parametrize(
    "source_id",
    [
        "session_abcdefgh",
        "event_session_abcdefgh",
        "debug_identifier",
        "mx.debug_identifier",
        "access_token_foo",
        "socket-id.foo",
        "event-socket-id-foo",
        "CDP.identifier",
        "event_api_key_foo",
        "event-secret-foo",
        "event.password.foo",
        "event_credential_foo",
        "event-id-token-foo",
        "event_cookie_foo",
        "event.authorization.foo",
    ],
)
def test_persistence_rejects_sensitive_opaque_source_id_before_transaction(source_id: str):
    content_hash = "c" * 64
    event = MxEvidence(
        evidence_id=hashlib.sha256(
            f"a-hunter:evidence:v1\0mx\0{source_id}\0{content_hash}".encode()
        ).hexdigest(),
        source_type="mx",
        source_id=source_id,
        rid=123,
        content_hash=content_hash,
        summary="关注 600519",
        received_at=AS_OF,
        source_created_at=None,
        media=(),
    )
    snapshot = mx_adapter.CollectorSnapshot(
        events=(event,), quality=object(), as_of=AS_OF, allowed_rids=(123,)
    )
    connection = sqlite3.connect(":memory:")
    _advisor_evidence_tables(connection)
    statements: list[str] = []
    connection.set_trace_callback(statements.append)

    with pytest.raises(ValueError, match="source_id"):
        persist_evidence(connection, "advisor-run", snapshot, as_of=AS_OF)

    assert not any(statement.startswith(("BEGIN", "SAVEPOINT")) for statement in statements)
    assert connection.execute("SELECT COUNT(*) FROM events_normalized").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


def test_persist_evidence_rejects_non_allowlisted_rid_before_transaction(
    tmp_path, create_collector_db, write_allowed_rids
):
    db, _ = create_collector_db(tmp_path)
    snapshot = read_collector_snapshot(
        db, write_allowed_rids(tmp_path, [123]), as_of=AS_OF, limit=1
    )
    authorized = snapshot.events[0]
    forged = replace(authorized, rid=456)
    snapshot = replace(snapshot, events=(forged,))
    connection = sqlite3.connect(":memory:")
    _advisor_evidence_tables(connection)
    statements: list[str] = []
    connection.set_trace_callback(statements.append)

    with pytest.raises(ValueError, match="allowlisted rid"):
        persist_evidence(connection, "advisor-run", snapshot, as_of=AS_OF)

    assert not any(statement.startswith(("BEGIN", "SAVEPOINT")) for statement in statements)
    assert connection.execute("SELECT COUNT(*) FROM events_normalized").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


def test_persist_evidence_rejects_forged_allowlist_provenance_before_transaction(
    tmp_path, create_collector_db, write_allowed_rids
):
    db, _ = create_collector_db(tmp_path)
    snapshot = read_collector_snapshot(
        db, write_allowed_rids(tmp_path, [123]), as_of=AS_OF, limit=1
    )
    forged = replace(snapshot.events[0], rid=456)
    forged_snapshot = replace(snapshot, events=(forged,), allowed_rids=(456,))
    connection = sqlite3.connect(":memory:")
    _advisor_evidence_tables(connection)
    statements: list[str] = []
    connection.set_trace_callback(statements.append)

    with pytest.raises(ValueError, match="authorization provenance"):
        persist_evidence(connection, "advisor-run", forged_snapshot, as_of=AS_OF)

    assert not any(statement.startswith(("BEGIN", "SAVEPOINT")) for statement in statements)
    assert connection.execute("SELECT COUNT(*) FROM events_normalized").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


@pytest.mark.parametrize("replacement", ([], [456]))
def test_persist_evidence_revalidates_current_allowlist_before_transaction(
    tmp_path, create_collector_db, write_allowed_rids, replacement
):
    db, _ = create_collector_db(tmp_path)
    allowed = write_allowed_rids(tmp_path, [123])
    snapshot = read_collector_snapshot(db, allowed, as_of=AS_OF, limit=1)
    write_allowed_rids(tmp_path, replacement)
    connection = sqlite3.connect(":memory:")
    _advisor_evidence_tables(connection)
    statements: list[str] = []
    connection.set_trace_callback(statements.append)

    with pytest.raises(ValueError, match="authorization provenance"):
        persist_evidence(connection, "advisor-run", snapshot, as_of=AS_OF)

    assert not any(statement.startswith(("BEGIN", "SAVEPOINT")) for statement in statements)
    assert connection.execute("SELECT COUNT(*) FROM events_normalized").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


def test_alternate_allowlist_path_cannot_issue_authorization(
    tmp_path, create_collector_db, write_allowed_rids, monkeypatch
):
    db, _ = create_collector_db(tmp_path)
    write_allowed_rids(tmp_path, [123])
    repo = tmp_path / "repo"
    alternate = tmp_path / "alternate.yaml"
    alternate.write_text("allowed_rids: [123]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="authoritative"):
        read_collector_snapshot(db, alternate, as_of=AS_OF, limit=1)


def test_snapshot_authorization_revalidates_authoritative_repo_path_before_transaction(
    tmp_path, create_collector_db, write_allowed_rids, monkeypatch
):
    db, _ = create_collector_db(tmp_path)
    original_repo = tmp_path / "original-repo"
    (original_repo / "config").mkdir(parents=True)
    allowed = original_repo / "config" / "allowed-rids.yaml"
    allowed.write_text("allowed_rids: [123]\n", encoding="utf-8")
    monkeypatch.setattr(mx_adapter, "repo_root", lambda: original_repo, raising=False)
    snapshot = read_collector_snapshot(db, allowed, as_of=AS_OF, limit=1)

    replacement_repo = tmp_path / "replacement-repo"
    (replacement_repo / "config").mkdir(parents=True)
    (replacement_repo / "config" / "allowed-rids.yaml").write_text(
        "allowed_rids: [123]\n", encoding="utf-8"
    )
    monkeypatch.setattr(mx_adapter, "repo_root", lambda: replacement_repo, raising=False)
    connection = sqlite3.connect(":memory:")
    _advisor_evidence_tables(connection)
    statements: list[str] = []
    connection.set_trace_callback(statements.append)

    with pytest.raises(ValueError, match="authorization provenance"):
        persist_evidence(connection, "advisor-run", snapshot, as_of=AS_OF)

    assert not any(statement.startswith(("BEGIN", "SAVEPOINT")) for statement in statements)
    assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("field", "forged", "message"),
    [
        ("evidence_id", "f" * 64, "evidence_id"),
        ("source_type", "news", "source_type"),
        ("rid", 0, "rid"),
        ("content_hash", "C" * 64, "content_hash"),
        ("source_id", "../forged-source", "source_id"),
    ],
)
def test_persist_evidence_rejects_forged_identity_before_transaction(
    field: str, forged: object, message: str
):
    source_id = "event-safe"
    content_hash = "c" * 64
    evidence_id = hashlib.sha256(
        f"a-hunter:evidence:v1\0mx\0{source_id}\0{content_hash}".encode()
    ).hexdigest()
    event = MxEvidence(
        evidence_id=evidence_id,
        source_type="mx",
        source_id=source_id,
        rid=123,
        content_hash=content_hash,
        summary="关注 600519",
        received_at=AS_OF,
        source_created_at=None,
        media=(),
    )
    event = replace(event, **{field: forged})
    snapshot = mx_adapter.CollectorSnapshot(
        events=(event,), quality=object(), as_of=AS_OF, allowed_rids=(123,)
    )
    connection = sqlite3.connect(":memory:")
    _advisor_evidence_tables(connection)
    statements: list[str] = []
    connection.set_trace_callback(statements.append)

    with pytest.raises(ValueError, match=message):
        persist_evidence(connection, "advisor-run", snapshot, as_of=AS_OF)

    assert not any(statement.startswith(("BEGIN", "SAVEPOINT")) for statement in statements)
    assert connection.execute("SELECT COUNT(*) FROM events_normalized").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"summary": "token=forged-summary"}, "summary"),
        ({"summary": "x" * 801}, "summary"),
        ({"received_at": AS_OF + timedelta(seconds=1)}, "received_at"),
        ({"source_created_at": datetime(2026, 7, 12, 8, 0)}, "source_created_at"),
        ({"media": (MediaMetadata("D" * 64, "image/jpeg", "data/events/media/a.jpg", AS_OF),)}, "media"),
        ({"media": (MediaMetadata("d" * 64, "image/jpeg", "data/events/media/a.jpg", AS_OF + timedelta(seconds=1)),)}, "media"),
    ],
)
def test_persist_evidence_rejects_forged_payload_fields_without_writing(changes, message):
    source_id = "event-safe"
    content_hash = "c" * 64
    event = MxEvidence(
        evidence_id=hashlib.sha256(
            f"a-hunter:evidence:v1\0mx\0{source_id}\0{content_hash}".encode()
        ).hexdigest(),
        source_type="mx",
        source_id=source_id,
        rid=123,
        content_hash=content_hash,
        summary="关注 600519",
        received_at=AS_OF,
        source_created_at=None,
        media=(),
    )
    connection = sqlite3.connect(":memory:")
    _advisor_evidence_tables(connection)

    with pytest.raises(ValueError, match=message):
        persist_evidence(
            connection,
            "advisor-run",
            mx_adapter.CollectorSnapshot(
                events=(replace(event, **changes),),
                quality=object(),
                as_of=AS_OF,
                allowed_rids=(123,),
            ),
            as_of=AS_OF,
        )

    assert connection.execute("SELECT COUNT(*) FROM events_normalized").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


@pytest.mark.parametrize(
    "local_path",
    ["https://secret.invalid/a.jpg", "/data/events/media/a.jpg", "data/events/media/../a.jpg"],
)
def test_persistence_rejects_forged_unsafe_media_path(local_path: str):
    source_id = "event-safe"
    content_hash = "c" * 64
    event = MxEvidence(
        evidence_id=hashlib.sha256(
            f"a-hunter:evidence:v1\0mx\0{source_id}\0{content_hash}".encode()
        ).hexdigest(),
        source_type="mx",
        source_id=source_id,
        rid=123,
        content_hash=content_hash,
        summary="关注 600519",
        received_at=AS_OF,
        source_created_at=None,
        media=(MediaMetadata("d" * 64, "image/jpeg", local_path, AS_OF),),
    )
    snapshot = mx_adapter.CollectorSnapshot(
        events=(event,), quality=object(), as_of=AS_OF, allowed_rids=(123,)
    )
    connection = sqlite3.connect(":memory:")
    _advisor_evidence_tables(connection)

    with pytest.raises(ValueError, match="media metadata"):
        persist_evidence(connection, "advisor-run", snapshot, as_of=AS_OF)

    assert connection.execute("SELECT COUNT(*) FROM events_normalized").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


def test_persist_evidence_rolls_back_both_tables_on_failure(
    tmp_path, create_collector_db, write_allowed_rids
):
    db, _ = create_collector_db(tmp_path)
    snapshot = read_collector_snapshot(db, write_allowed_rids(tmp_path, [123]), as_of=AS_OF)
    connection = sqlite3.connect(":memory:")
    _advisor_evidence_tables(connection)
    connection.execute(
        "CREATE TRIGGER reject_evidence BEFORE INSERT ON evidence BEGIN SELECT RAISE(ABORT, 'reject'); END"
    )

    with pytest.raises(sqlite3.IntegrityError):
        persist_evidence(connection, "advisor-run", snapshot, as_of=AS_OF)

    assert connection.execute("SELECT count(*) FROM events_normalized").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM evidence").fetchone()[0] == 0


def test_persist_evidence_preserves_caller_owned_transaction(
    tmp_path, create_collector_db, write_allowed_rids
):
    db, _ = create_collector_db(tmp_path)
    snapshot = read_collector_snapshot(db, write_allowed_rids(tmp_path, [123]), as_of=AS_OF)
    connection = sqlite3.connect(":memory:")
    _advisor_evidence_tables(connection)
    connection.execute("CREATE TABLE caller_work (value TEXT)")
    connection.execute("BEGIN")
    connection.execute("INSERT INTO caller_work VALUES ('uncommitted')")

    persist_evidence(connection, "advisor-run", snapshot, as_of=AS_OF)
    connection.rollback()

    assert connection.execute("SELECT count(*) FROM caller_work").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM events_normalized").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM evidence").fetchone()[0] == 0


def test_persist_evidence_rejects_conflicting_existing_identity(
    tmp_path, create_collector_db, write_allowed_rids
):
    db, _ = create_collector_db(tmp_path)
    snapshot = read_collector_snapshot(db, write_allowed_rids(tmp_path, [123]), as_of=AS_OF, limit=1)
    event = snapshot.events[0]
    connection = sqlite3.connect(":memory:")
    _advisor_evidence_tables(connection)
    connection.execute(
        """
        INSERT INTO events_normalized (
          evidence_source_id, source_type, source_id, code, as_of, summary, raw_ref_json
        ) VALUES (?, 'mx', 'different-source', NULL, ?, 'tampered', '{}')
        """,
        (event.evidence_id, event.received_at.isoformat()),
    )
    connection.commit()

    with pytest.raises(ValueError, match="conflicting normalized evidence"):
        persist_evidence(connection, "advisor-run", snapshot, as_of=AS_OF)

    assert connection.execute("SELECT count(*) FROM evidence").fetchone()[0] == 0
