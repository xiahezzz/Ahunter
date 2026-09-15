"""Loopback Web/CLI adapter for LAgent configuration, submission and traces."""
from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
import sqlite3
import re

from advisor.research import lagent


def register_lagent_routes(app, *, repository_factory, catalog_factory, artifact_store_factory, root):
    # Reuse the application's same-origin, content-type and bounded JSON checks.
    from advisor.web.api import (_bounded_json_object, _require_local_json_request,
                                 _ResearchRequestPayloadError, _request_subject, _research_request_item)

    async def payload(request):
        try:
            _require_local_json_request(request, _ResearchRequestPayloadError)
            return await _bounded_json_object(request, 100_000, _ResearchRequestPayloadError)
        except _ResearchRequestPayloadError:
            raise HTTPException(400, "LAgent 请求输入无效") from None

    def run(operation, *, writable=False):
        repository = None
        try:
            repository = repository_factory(writable=writable)
            return operation(repository)
        except lagent.SettingsConflict as error:
            raise HTTPException(409, str(error)) from None
        except (ValueError, TypeError, KeyError):
            raise HTTPException(400, "LAgent 输入、配置或请求编号无效") from None
        except (OSError, sqlite3.Error):
            raise HTTPException(503, "LAgent 控制面暂不可用") from None
        finally:
            if repository is not None:
                repository.close()

    @app.get("/api/research/lagent/settings")
    def lagent_settings():
        return run(lagent.settings_view, writable=True)

    @app.put("/api/research/lagent/settings")
    async def update_lagent_settings(request: Request):
        body = await payload(request)
        if set(body) != {"expected_version", "config"}:
            raise HTTPException(400, "需要 expected_version 和完整 config")
        return run(lambda repository: lagent.update_settings(repository, catalog=catalog_factory(), root=root, **body), writable=True)

    @app.post("/api/research/lagent/requests")
    async def submit_lagent_request(request: Request):
        body = await payload(request)
        if set(body) != {"scope", "code", "task", "submission_identity", "expected_version"}:
            raise HTTPException(400, "LAgent 研究请求字段无效")
        def submit(repository):
            subject = _request_subject(body["scope"], body["code"])
            if not isinstance(body["submission_identity"], str) or not body["submission_identity"].strip() or len(body["submission_identity"]) > 256:
                raise ValueError("invalid submission identity")
            result = lagent.submit(repository, catalog=catalog_factory(), subject=subject, task=body["task"],
                submission_identity=body["submission_identity"], expected_version=body["expected_version"], root=root)
            return {"request": _research_request_item(result)}
        return JSONResponse(run(submit, writable=True), status_code=202)

    @app.get("/api/research/lagent/requests/{request_id}/trace")
    def lagent_trace(request_id: str, after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500)):
        def read(repository):
            return {**lagent.trace(repository, request_id, after=after, limit=limit),
                    "request": _research_request_item(repository.get_request(request_id))}
        return run(read)

    @app.get("/api/research/lagent/requests/{request_id}/artifacts/{artifact_hash}")
    def lagent_artifact(request_id: str, artifact_hash: str):
        def read(repository):
            if not re.fullmatch(r"[0-9a-f]{64}", artifact_hash):
                raise ValueError("invalid artifact hash")
            permitted = repository.connection.execute(
                "SELECT 1 FROM research_lagent_events WHERE request_id=? AND "
                "(json_extract(payload_json,'$.artifact_hash')=? OR json_extract(payload_json,'$.capsule_hash')=?) LIMIT 1",
                (request_id, artifact_hash, artifact_hash),
            ).fetchone()
            if not permitted:
                raise HTTPException(404, "该研究没有此调用产物")
            store = artifact_store_factory()
            try:
                if not store.verify(artifact_hash):
                    raise HTTPException(409, "调用产物未通过完整性校验")
                return FileResponse(store._path_for(artifact_hash), media_type="application/json",
                    filename=f"lagent-{artifact_hash}.json")
            finally:
                store.close()
        return run(read)
