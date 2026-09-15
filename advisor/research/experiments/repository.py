"""Immutable drafts and append-only preflight records in the existing control DB."""
from datetime import datetime, timezone
import hashlib
import json
import uuid

from .contracts import ExperimentDraft
from .resolution import validation_errors


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


class Conflict(ValueError):
    pass


class Missing(LookupError):
    pass


class ExperimentStore:
    def __init__(self, connection, *, artifacts=None):
        self.db = connection
        self.artifacts = artifacts

    def records(self, **kwargs):
        from .records import ExperimentRecords
        return ExperimentRecords(self.db, artifacts=self.artifacts, **kwargs)

    def registry(self, **kwargs):
        from .registration import ExperimentRegistry
        return ExperimentRegistry(self.records(**kwargs))

    def queries(self, *, viewer, **kwargs):
        from .queries import ExperimentQueries
        return ExperimentQueries(self.records(**kwargs), viewer=viewer)

    def create(self, raw, submission_identity):
        errors = validation_errors(raw)
        parsed = raw if errors else ExperimentDraft.model_validate(raw).model_dump(mode="json")
        fingerprint = digest(raw)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT experiment_id, input_hash FROM lagent_experiments WHERE submission_identity=?",
                                  (submission_identity,)).fetchone()
            if row:
                if row[1] != fingerprint:
                    raise Conflict("提交标识已用于另一份配置；修改配置请使用新标识")
                result = self.get(row[0])
            else:
                experiment_id = "lexp-" + uuid.uuid4().hex
                self.db.execute("INSERT INTO lagent_experiments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                (experiment_id, submission_identity, str(raw.get("name") or "未命名实验草案"), encode(raw), fingerprint,
                                 encode(parsed), digest(parsed), now()))
                result = self.get(experiment_id)
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def get(self, experiment_id):
        row = self.db.execute("SELECT experiment_id, name, raw_json, config_json, config_hash, created_at "
                              "FROM lagent_experiments WHERE experiment_id=?", (experiment_id,)).fetchone()
        if row is None:
            raise Missing("实验不存在")
        if digest(json.loads(row[3])) != row[4]:
            raise Conflict("实验配置完整性校验失败")
        return {"experiment_id": row[0], "name": row[1], "status": "draft", "raw_config": json.loads(row[2]),
                "config": json.loads(row[3]), "config_hash": row[4], "created_at": row[5],
                "execution_available": False,
                "validation_errors": [error.model_dump(mode="json") for error in validation_errors(json.loads(row[3]))]}

    def list(self, *, limit, offset):
        rows = self.db.execute("SELECT experiment_id, name, config_hash, created_at FROM lagent_experiments "
                               "ORDER BY created_at, experiment_id LIMIT ? OFFSET ?", (limit + 1, offset)).fetchall()
        return {"items": [{"experiment_id": r[0], "name": r[1], "config_hash": r[2], "created_at": r[3],
                           "status": "draft"} for r in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None}

    def existing_preflight(self, experiment_id, submission_identity):
        row = self.db.execute("SELECT result_json, result_hash FROM lagent_experiment_preflights "
                              "WHERE experiment_id=? AND submission_identity=?",
                              (experiment_id, submission_identity)).fetchone()
        if row is None:
            return None
        value = json.loads(row[0])
        if digest(value) != row[1]:
            raise Conflict("预检记录完整性校验失败")
        return value

    def record_preflight(self, experiment_id, submission_identity, report):
        if {"record_id", "experiment_id", "record_type", "created_at"} & report.keys():
            raise ValueError("preflight report cannot override record metadata")
        # No providers or models are called while this transaction is held.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.existing_preflight(experiment_id, submission_identity)
            if existing is not None:
                original = {key: value for key, value in existing.items()
                            if key not in {"record_id", "experiment_id", "record_type", "created_at"}}
                if digest(original) != digest(report):
                    raise Conflict("预检提交标识已用于不同内容")
                self.db.commit()
                return existing
            value = {**report, "record_id": "lpre-" + uuid.uuid4().hex, "experiment_id": experiment_id,
                     "record_type": "preflight", "created_at": now()}
            self.db.execute("INSERT INTO lagent_experiment_preflights VALUES (?, ?, ?, ?, ?, ?)",
                            (value["record_id"], experiment_id, submission_identity, encode(value), digest(value), value["created_at"]))
            self.db.commit()
            return value
        except BaseException:
            self.db.rollback()
            raise

    def preflights(self, experiment_id, *, limit, offset):
        self.get(experiment_id)
        rows = self.db.execute("SELECT submission_identity FROM lagent_experiment_preflights WHERE experiment_id=? "
                               "ORDER BY created_at, record_id LIMIT ? OFFSET ?",
                               (experiment_id, limit + 1, offset)).fetchall()
        return {"items": [self.existing_preflight(experiment_id, r[0]) for r in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None}
