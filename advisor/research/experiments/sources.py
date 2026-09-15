"""Source-policy retention keeps immutable metadata when original text expires."""
from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

from pydantic import model_validator

from .contracts import Contract, Hash, Name
from .registration import record_identity
from .repository import Conflict
from .resolution import digest


class SourceDocument(Contract):
    source_id: Name
    content_hash: Hash
    source_ref: Name
    title: str
    url: str | None
    fetched_at: datetime
    expires_at: datetime
    retention_policy_ref: Name

    @model_validator(mode="after")
    def dates_and_url(self):
        if any(v.tzinfo is None or v.utcoffset() is None for v in (self.fetched_at, self.expires_at)):
            raise ValueError("source retention timestamps must be timezone-aware")
        if self.expires_at <= self.fetched_at:
            raise ValueError("source expiry must follow fetch time")
        if self.url is not None:
            parsed = urlsplit(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("source URL must be HTTP(S) without credentials")
        return self


class SourceRetention:
    def __init__(self, records):
        self.records = records
        self.artifacts = records.artifacts

    def register(self, experiment_id, test_id, document, *, submission_identity):
        document = SourceDocument.model_validate(document)
        test = self.records.read(test_id)
        if test["kind"] != "test" or test["experiment_id"] != experiment_id:
            raise Conflict("source must belong to a test in the same experiment")
        source_record_id = record_identity(experiment_id, "source", document.source_id)
        previous = self.records.db.execute(
            "SELECT record_id FROM lagent_records WHERE experiment_id=? AND kind='source_document' AND submission_identity=?",
            (experiment_id, submission_identity)).fetchone()
        if previous is not None:
            original = self.records.read(previous[0])
            if (original["record_id"] != source_record_id
                    or digest(original["value"]) != digest({"registration_version": 1, "document": document.model_dump(mode="json")})
                    or self.records.related(original["record_id"]) != [{"relation": "test", "target_id": test_id}]):
                raise Conflict("source submission identity has different content")
            # Retry acknowledges the original registration, even after its bytes
            # legally expired; it never renews the source's retention deadline.
            return original
        if document.expires_at <= self.records._now():
            raise ValueError("cannot register already-expired source bytes")
        return self.records.put(experiment_id=experiment_id, kind="source_document",
                                record_id=source_record_id,
                                submission_identity=submission_identity,
                                value={"registration_version": 1, "document": document.model_dump(mode="json")},
                                links=(("test", test_id),), source_policy_hashes=(document.content_hash,))

    def _document(self, record_id):
        record = self.records.read(record_id)
        if record["kind"] != "source_document":
            raise Conflict("expected source document")
        return record, SourceDocument.model_validate(record["value"]["document"])

    def _expired(self, record, document):
        marked = self.records.db.execute(
            "SELECT 1 FROM lagent_record_links l JOIN lagent_records r ON r.record_id=l.record_id "
            "WHERE l.target_id=? AND l.relation='source' AND r.kind='source_expiration' LIMIT 1", (record["record_id"],)).fetchone()
        return marked is not None or document.expires_at <= self.records._now()

    def describe(self, record_id):
        record, document = self._document(record_id)
        expired = self._expired(record, document)
        available = not expired and self.artifacts.verify(document.content_hash)
        return {"record_id": record_id, "document": document.model_dump(mode="json"),
                "availability": "available" if available else "unavailable",
                "reason": None if available else "source_policy_expired" if expired else "artifact_missing_or_corrupt",
                "replay": "exact_bytes" if available else "metadata_and_hash_only"}

    def read_bytes(self, record_id):
        view = self.describe(record_id)
        if view["availability"] != "available":
            raise FileNotFoundError("source original is unavailable; metadata and hash remain")
        return self.artifacts.read_bytes(view["document"]["content_hash"])

    def expire(self, record_id, *, submission_identity):
        record, document = self._document(record_id)
        if document.expires_at > self.records._now():
            raise Conflict("source has not reached its retention deadline")
        # Commit unavailability before removing bytes. A crash after this fact is
        # harmless: downloads remain unavailable and the same expiry can resume.
        expiration = self.records.put(experiment_id=record["experiment_id"], kind="source_expiration",
                                      record_id=record_identity(record["experiment_id"], "expiry", submission_identity),
                                      submission_identity=submission_identity,
                                      value={"source_id": record_id, "content_hash": document.content_hash,
                                             "reason": "source_policy_expired", "expires_at": document.expires_at.isoformat()},
                                      links=(("source", record_id),))
        with self.records._transaction():
            refs = self.records.db.execute("SELECT l.record_id, l.retention FROM lagent_artifact_links l WHERE l.content_hash=?",
                                           (document.content_hash,)).fetchall()
            metadata = self.records.db.execute("SELECT retention_class FROM research_artifacts WHERE content_hash=?", (document.content_hash,)).fetchone()
            # Shared internal/business bytes must never be deleted by a source's expiry.
            deletable = metadata is not None and metadata[0] == "source_policy"
            for target_id, retention in refs:
                if retention != "source_policy":
                    deletable = False
                    break
                target = self.records.read(target_id)
                if target["kind"] != "source_document":
                    deletable = False
                    break
                other = SourceDocument.model_validate(target["value"]["document"])
                if not self._expired(target, other):
                    deletable = False
                    break
            if deletable:
                self.records.fault("before_source_unlink")
                # Do not resolve a tampered symlink into another protected object.
                target = self.artifacts.root / document.content_hash[:2] / document.content_hash[2:]
                if target.parent.is_symlink():
                    raise Conflict("source artifact directory is a symlink")
                target.unlink(missing_ok=True)
                self.artifacts._sync_directory(target.parent)
                self.records.fault("after_source_unlink")
                self.records.db.execute("UPDATE research_artifacts SET availability='expired' WHERE content_hash=?", (document.content_hash,))
        return {"expiration": expiration, "bytes_deleted": deletable, **self.describe(record_id)}
