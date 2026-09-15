"""Local owner entry points for drafts, readiness and audited experiment evidence.

No executor, candidate process or provider is started by this adapter. Detailed
reads use POST because hidden-scope exposure must commit before data is returned.
"""
import json
from pathlib import Path
import re
import sqlite3

from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .candidates import decode_candidate_bytes, seal_candidate_upload
from .budget import CostUnavailable
from .calibration import Calibrations
from .calibration_inputs import seal_calibration_upload
from .contracts import CandidateProposal, original_case
from .control import ExperimentControl
from .comparison_inputs import seal_condition_upload
from .data.bundles import BundleInvalid, HistoricalBundles
from .definitions import DefinitionResolutions
from .evaluate import ComparisonAssessments
from .evolution import EvolutionTree
from .preflight import inspect
from .queries import QueryDenied
from .repository import Conflict, Missing, encode
from .selection import ExperimentSelection, selection_overview


def register_experiment_routes(app, *, repository_factory, catalog_factory, artifact_store_factory):
    from advisor.web.api import _bounded_json_object, _require_local_json_request

    async def payload(request, keys):
        try:
            _require_local_json_request(request, ValueError)
            body = await _bounded_json_object(request, 1_000_000, ValueError)
            if set(body) != set(keys):
                raise ValueError("unexpected fields")
            # Reject JSON NaN/Infinity before any record or exposure write.
            json.dumps(body, allow_nan=False)
            for key in ("submission_identity", "audit_identity"):
                if key in body and (not isinstance(body[key], str) or not body[key].strip() or len(body[key]) > 256):
                    raise ValueError("invalid identity")
            for key in ("calendar_bundle_id", "definition_id", "purpose", "baseline_id", "comparison_id", "expected_selection_id", "plan_id", "baseline", "candidate"):
                if key in body and (not isinstance(body[key], str) or not body[key].strip() or len(body[key]) > 256):
                    raise ValueError("invalid reference")
            if "selection_id" in body and body["selection_id"] is not None:
                if not isinstance(body["selection_id"], str) or not body["selection_id"].strip() or len(body["selection_id"]) > 256:
                    raise ValueError("invalid selection reference")
            if "previous_comparison" in body and body["previous_comparison"] is not None:
                if not isinstance(body["previous_comparison"], str) or not body["previous_comparison"].strip() or len(body["previous_comparison"]) > 256:
                    raise ValueError("invalid previous comparison")
            if "reason" in body and (not isinstance(body["reason"], str) or not body["reason"].strip() or len(body["reason"]) > 4096):
                raise ValueError("invalid reason")
            return body
        except (ValueError, TypeError, RecursionError):
            raise HTTPException(400, "实验请求需要本地同源 JSON、完整字段及非空稳定标识") from None

    def run(operation, *, writable=False, with_artifacts=False, create_artifacts=False):
        repository = artifacts = None
        try:
            repository = repository_factory(writable=writable)
            if with_artifacts:
                artifacts = artifact_store_factory(writable=True) if create_artifacts else artifact_store_factory()
            return operation(repository, repository.experiment_store(artifacts=artifacts))
        except QueryDenied as error:
            raise HTTPException(403, str(error)) from None
        except Missing as error:
            raise HTTPException(404, str(error)) from None
        except Conflict as error:
            raise HTTPException(409, str(error)) from None
        except CostUnavailable as error:
            raise HTTPException(409, {"status": "blocked", "reason": str(error), "execution_available": False}) from None
        except BundleInvalid as error:
            raise HTTPException(400, {"status": "blocked", "issues": error.issues,
                                      "failure_record_id": error.failure_record_id,
                                      "execution_available": False}) from None
        except FileNotFoundError:
            raise HTTPException(410, "原产物不可用；请查看导出清单中的元数据、哈希和保留状态") from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise HTTPException(400, "实验输入、记录编号或分页条件无效") from None
        except (OSError, sqlite3.Error):
            raise HTTPException(503, "实验存储暂不可用") from None
        finally:
            if artifacts is not None:
                artifacts.close()
            if repository is not None:
                repository.close()

    def scoped(store, experiment_id, record_id, *, kind=None):
        store.get(experiment_id)
        record = store.records().read(record_id)
        if record["experiment_id"] != experiment_id or (kind is not None and record["kind"] != kind):
            raise Missing("该实验没有此类型记录")
        return record

    @app.get("/api/research/experiments/presets/original-case")
    def experiment_original_case():
        return {"config": original_case(), "execution_available": False}

    @app.post("/api/research/experiments")
    async def create_experiment(request: Request):
        body = await payload(request, ("config", "submission_identity"))
        if not isinstance(body["config"], dict):
            raise HTTPException(400, "config 必须是实验草案对象")
        return JSONResponse(run(lambda _, store: store.create(body["config"], body["submission_identity"]), writable=True),
                            status_code=201)

    @app.get("/api/research/experiments")
    def list_experiments(limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0)):
        return run(lambda _, store: store.list(limit=limit, offset=offset))

    @app.get("/api/research/experiments/{experiment_id}")
    def show_experiment(experiment_id: str):
        return run(lambda _, store: store.get(experiment_id))

    def evolution_page(experiment_id, view, limit, cursor, proposal_id=None):
        def read(_, store):
            store.get(experiment_id)
            return EvolutionTree(store.records()).page(experiment_id=experiment_id, view=view, limit=limit,
                                                        cursor=cursor, proposal_id=proposal_id)
        return run(read)

    @app.get("/api/research/experiments/{experiment_id}/tree")
    def experiment_tree(experiment_id: str, limit: int = Query(default=50, ge=1, le=200), cursor: str | None = None):
        return evolution_page(experiment_id, "tree", limit, cursor)

    @app.get("/api/research/experiments/{experiment_id}/candidates/{proposal_id}/tests")
    def experiment_candidate_tests(experiment_id: str, proposal_id: str, limit: int = Query(default=50, ge=1, le=200), cursor: str | None = None):
        return evolution_page(experiment_id, "tests", limit, cursor, proposal_id)

    @app.get("/api/research/experiments/{experiment_id}/candidates/{proposal_id}/comparisons")
    def experiment_candidate_comparisons(experiment_id: str, proposal_id: str, limit: int = Query(default=50, ge=1, le=200), cursor: str | None = None):
        return evolution_page(experiment_id, "comparisons", limit, cursor, proposal_id)

    @app.get("/api/research/experiments/{experiment_id}/selection/history")
    def experiment_selection_history(experiment_id: str, limit: int = Query(default=50, ge=1, le=200), cursor: str | None = None):
        return evolution_page(experiment_id, "selection", limit, cursor)

    @app.post("/api/research/experiments/{experiment_id}/preflight")
    async def preflight_experiment(experiment_id: str, request: Request):
        body = await payload(request, ("submission_identity",))
        def perform(repository, store):
            experiment = store.get(experiment_id)
            existing = store.existing_preflight(experiment_id, body["submission_identity"])
            if existing is not None:
                return existing
            report = inspect(repository, experiment, None if experiment["validation_errors"] else catalog_factory())
            return store.record_preflight(experiment_id, body["submission_identity"], report)
        return run(perform, writable=True)

    @app.get("/api/research/experiments/{experiment_id}/preflights")
    def list_experiment_preflights(experiment_id: str, limit: int = Query(default=50, ge=1, le=200),
                                  offset: int = Query(default=0, ge=0)):
        return run(lambda _, store: store.preflights(experiment_id, limit=limit, offset=offset))

    @app.get("/api/research/experiments/{experiment_id}/records")
    def list_experiment_records(experiment_id: str, kind: str | None = None,
                                limit: int = Query(default=50, ge=1, le=200), cursor: str | None = None):
        def read(_, store):
            store.get(experiment_id)
            return store.queries(viewer="owner").page(experiment_id=experiment_id, kind=kind, limit=limit, cursor=cursor)
        return run(read)

    @app.post("/api/research/experiments/{experiment_id}/bundles/import")
    async def import_experiment_bundle(experiment_id: str, request: Request):
        body = await payload(request, ("root", "submission_identity"))
        def register(_, store):
            store.get(experiment_id)
            if (not isinstance(body["root"], str) or not Path(body["root"]).is_absolute()
                    or not Path(body["root"]).is_dir()):
                raise ValueError("historical bundle root must be an explicit absolute local directory")
            bundle = HistoricalBundles(store.records()).import_bundle(experiment_id, body["root"],
                submission_identity=body["submission_identity"])
            return {"bundle": bundle, "execution_available": False}
        result = await run_in_threadpool(run, register, writable=True, with_artifacts=True, create_artifacts=True)
        return JSONResponse(result, status_code=201)

    @app.post("/api/research/experiments/{experiment_id}/definitions/resolve")
    async def resolve_experiment_definition(experiment_id: str, request: Request):
        body = await payload(request, ("config", "calendar_bundle_id", "submission_identity"))
        def register(_, store):
            scoped(store, experiment_id, body["calendar_bundle_id"], kind="data_bundle")
            return DefinitionResolutions(store.records()).register(experiment_id, **body)
        return run(register, writable=True, with_artifacts=True)

    @app.post("/api/research/experiments/{experiment_id}/plans")
    async def register_experiment_plan(experiment_id: str, request: Request):
        body = await payload(request, ("definition_id", "plan", "purpose", "selection_id", "submission_identity"))
        def register(_, store):
            scoped(store, experiment_id, body["definition_id"], kind="definition")
            return store.registry().test_plan(experiment_id, **body)
        return JSONResponse(run(register, writable=True, with_artifacts=True), status_code=201)

    @app.get("/api/research/experiments/{experiment_id}/selection")
    def show_experiment_selection(experiment_id: str):
        def read(_, store):
            store.get(experiment_id)
            return {"selection": selection_overview(store.records(), experiment_id), "execution_available": False}
        return run(read)

    @app.post("/api/research/experiments/{experiment_id}/selection/initialize")
    async def initialize_experiment_selection(experiment_id: str, request: Request):
        body = await payload(request, ("definition_id", "baseline_id"))
        def initialize(_, store):
            scoped(store, experiment_id, body["definition_id"], kind="definition")
            scoped(store, experiment_id, body["baseline_id"], kind="candidate_proposal")
            record = ExperimentSelection(store.records()).initialize(experiment_id, **body)
            return {"selection": record, "execution_available": False}
        result = await run_in_threadpool(run, initialize, writable=True, with_artifacts=True)
        return JSONResponse(result, status_code=201)

    @app.post("/api/research/experiments/{experiment_id}/selection/apply")
    async def apply_experiment_selection(experiment_id: str, request: Request):
        body = await payload(request, ("comparison_id", "expected_selection_id", "submission_identity"))
        def apply(_, store):
            scoped(store, experiment_id, body["comparison_id"], kind="comparison")
            scoped(store, experiment_id, body["expected_selection_id"], kind="selection")
            # A mutation must not become another way to obtain final-holdout or
            # unknown-scope feedback. The original controller rechecks evidence.
            store.queries(viewer="owner").comparison_feedback(body["comparison_id"])
            decision = ExperimentSelection(store.records()).apply_comparison(**body)
            return {"decision": decision, "execution_available": False}
        return await run_in_threadpool(run, apply, writable=True, with_artifacts=True)

    @app.post("/api/research/experiments/{experiment_id}/selection/finalize-holdout")
    async def finalize_experiment_selection(experiment_id: str, request: Request):
        body = await payload(request, ("expected_selection_id", "submission_identity"))
        def finalize(_, store):
            scoped(store, experiment_id, body["expected_selection_id"], kind="selection")
            record = ExperimentSelection(store.records()).finalize(experiment_id, **body)
            return {"selection": record, "execution_available": False}
        return await run_in_threadpool(run, finalize, writable=True, with_artifacts=True)

    @app.post("/api/research/experiments/{experiment_id}/candidates")
    async def register_experiment_candidate(experiment_id: str, request: Request):
        body = await payload(request, ("manifest", "files_base64", "proposal", "diff_base64", "submission_identity"))
        def register(_, store):
            store.get(experiment_id)
            metadata = body["proposal"]
            if not isinstance(metadata, dict) or set(metadata) != {"proposal_id", "parent_proposal_id", "hypothesis", "source"}:
                raise ValueError("candidate proposal metadata fields are invalid")
            # Validate lineage metadata and diff bytes before any package writes.
            proposal = CandidateProposal.model_validate({**metadata, "package_hash": "0" * 64, "diff_artifact_hash": None})
            diff = decode_candidate_bytes(body["diff_base64"]) if body["diff_base64"] is not None else None
            package = seal_candidate_upload(body["manifest"], body["files_base64"], artifacts=store.artifacts)
            sealed = {**proposal.model_dump(mode="json"), "package_hash": package.package_hash,
                      "diff_artifact_hash": store.artifacts.put_bytes(diff).content_hash if diff is not None else None}
            return {**store.registry().candidate(experiment_id, sealed, submission_identity=body["submission_identity"]),
                    "execution_available": False}
        return JSONResponse(run(register, writable=True, with_artifacts=True, create_artifacts=True), status_code=201)

    @app.get("/api/research/experiments/{experiment_id}/candidates")
    def list_experiment_candidates(experiment_id: str, limit: int = Query(default=50, ge=1, le=200), cursor: str | None = None):
        def read(_, store):
            store.get(experiment_id)
            return store.queries(viewer="owner").candidates(experiment_id=experiment_id, limit=limit, cursor=cursor)
        return run(read)

    @app.get("/api/research/experiments/{experiment_id}/candidates/{proposal_id}")
    def show_experiment_candidate(experiment_id: str, proposal_id: str):
        def read(_, store):
            store.get(experiment_id)
            return store.queries(viewer="owner").candidate(experiment_id=experiment_id, proposal_id=proposal_id)
        return run(read, with_artifacts=True)

    @app.post("/api/research/experiments/{experiment_id}/records/{record_id}/detail")
    async def show_experiment_record(experiment_id: str, record_id: str, request: Request):
        body = await payload(request, ("audit_identity",))
        def read(_, store):
            scoped(store, experiment_id, record_id)
            return store.queries(viewer="owner").detail(record_id, **body)
        return run(read, writable=True, with_artifacts=True)

    @app.post("/api/research/experiments/{experiment_id}/tests/{test_id}/events")
    async def experiment_test_events(experiment_id: str, test_id: str, request: Request,
                                     after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500)):
        body = await payload(request, ("audit_identity",))
        def read(_, store):
            scoped(store, experiment_id, test_id, kind="test")
            return store.queries(viewer="owner").events(test_id, after=after, limit=limit, **body)
        return run(read, writable=True)

    @app.post("/api/research/experiments/{experiment_id}/tests/{test_id}/cancel")
    async def cancel_experiment_test(experiment_id: str, test_id: str, request: Request):
        body = await payload(request, ("submission_identity", "reason"))
        def cancel(_, store):
            scoped(store, experiment_id, test_id, kind="test")
            return ExperimentControl(store.records()).cancel(experiment_id, test_id, **body)
        result = run(cancel, writable=True)
        return JSONResponse(result, status_code=200 if result["terminal"] else 202)

    @app.post("/api/research/experiments/{experiment_id}/tests/{test_id}/rerun")
    async def rerun_experiment_test(experiment_id: str, test_id: str, request: Request):
        body = await payload(request, ("submission_identity", "reason"))
        def register(_, store):
            scoped(store, experiment_id, test_id, kind="test")
            test = store.registry().rerun(experiment_id, test_id, rerun_identity=body["submission_identity"], reason=body["reason"])
            return {"test": test, "status": store.records().status(test["record_id"]), "execution_available": False}
        return JSONResponse(run(register, writable=True), status_code=201)

    @app.post("/api/research/experiments/{experiment_id}/records/{record_id}/export")
    async def export_experiment_record(experiment_id: str, record_id: str, request: Request):
        body = await payload(request, ("audit_identity",))
        def read(_, store):
            scoped(store, experiment_id, record_id)
            manifest = store.queries(viewer="owner").export(record_id, **body)
            return Response(encode(manifest).encode(), media_type="application/json",
                            headers={"Content-Disposition": 'attachment; filename="experiment-record.json"'})
        return run(read, writable=True, with_artifacts=True)

    @app.post("/api/research/experiments/{experiment_id}/records/{record_id}/artifacts/{artifact_hash}")
    async def download_experiment_artifact(experiment_id: str, record_id: str, artifact_hash: str, request: Request):
        body = await payload(request, ("audit_identity",))
        if not re.fullmatch(r"[0-9a-f]{64}", artifact_hash):
            raise HTTPException(400, "产物哈希无效")
        def read(_, store):
            scoped(store, experiment_id, record_id)
            try:
                data = store.queries(viewer="owner").artifact(record_id, artifact_hash, **body)
            except ValueError as error:
                if isinstance(error, Conflict):
                    raise
                raise Conflict("原产物未通过完整性校验") from error
            return Response(data, media_type="application/octet-stream", headers={
                "Content-Disposition": f'attachment; filename="{artifact_hash}.bin"',
                "X-Content-SHA256": artifact_hash})
        return run(read, writable=True, with_artifacts=True)

    @app.get("/api/research/experiments/{experiment_id}/comparisons/{record_id}/feedback")
    def experiment_comparison_feedback(experiment_id: str, record_id: str):
        def read(_, store):
            scoped(store, experiment_id, record_id, kind="comparison")
            return store.queries(viewer="owner").comparison_feedback(record_id)
        return run(read)

    def calibration_metadata(record):
        # Resource evidence spans every declared task, including holdout. Neither
        # mutation returns measurements or task budgets without an audited read.
        return {**{key: record[key] for key in ("record_id", "content_hash", "sequence", "created_at")},
                "cost_record_type": record["value"]["cost_record_type"], "formal_ready": record["value"]["formal_ready"]}

    @app.post("/api/research/experiments/{experiment_id}/calibrations")
    async def freeze_experiment_calibration(experiment_id: str, request: Request):
        body = await payload(request, ("plan_id", "expected_selection_id", "table", "envelopes", "artifacts_base64"))
        def freeze(_, store):
            scoped(store, experiment_id, body["plan_id"], kind="test_plan")
            scoped(store, experiment_id, body["expected_selection_id"], kind="selection")
            table, envelopes = seal_calibration_upload(body["table"], body["envelopes"], body["artifacts_base64"], artifacts=store.artifacts)
            record = Calibrations(store.records()).freeze(body["plan_id"], table, envelopes,
                                                        expected_selection_id=body["expected_selection_id"])
            return {"calibration": calibration_metadata(record), "execution_available": False}
        return JSONResponse(await run_in_threadpool(run, freeze, writable=True, with_artifacts=True), status_code=201)

    @app.post("/api/research/experiments/{experiment_id}/calibrations/{record_id}/complete")
    async def complete_experiment_calibration(experiment_id: str, record_id: str, request: Request):
        await payload(request, ())
        def complete(_, store):
            scoped(store, experiment_id, record_id, kind="cost")
            record = Calibrations(store.records()).complete(record_id)
            return {"calibration": calibration_metadata(record), "execution_available": False}
        return await run_in_threadpool(run, complete, writable=True, with_artifacts=True)

    @app.post("/api/research/experiments/{experiment_id}/comparisons")
    async def freeze_experiment_comparison(experiment_id: str, request: Request):
        body = await payload(request, ("plan_id", "baseline", "candidate", "submission_identity", "previous_comparison",
                                       "expected_selection_id", "runtime_conditions", "runtime_artifacts_base64"))
        def freeze(_, store):
            scoped(store, experiment_id, body["plan_id"], kind="test_plan")
            scoped(store, experiment_id, body["expected_selection_id"], kind="selection")
            if body["previous_comparison"] is not None:
                scoped(store, experiment_id, body["previous_comparison"], kind="comparison")
                store.queries(viewer="owner").require_comparison_scope(body["previous_comparison"])
            supplied = seal_condition_upload(body["runtime_conditions"], body["runtime_artifacts_base64"], artifacts=store.artifacts)
            conditions = {key: value for key, value in body.items() if key not in {"runtime_conditions", "runtime_artifacts_base64"}}
            record = ComparisonAssessments(store.records()).freeze(**conditions, runtime_conditions=supplied)
            return {"comparison": record, "execution_available": False}
        result = await run_in_threadpool(run, freeze, writable=True, with_artifacts=True)
        return JSONResponse(result, status_code=201)

    @app.post("/api/research/experiments/{experiment_id}/comparisons/{record_id}/complete")
    async def complete_experiment_comparison(experiment_id: str, record_id: str, request: Request):
        await payload(request, ())  # Result identity is canonical for the frozen comparison.
        def complete(_, store):
            scoped(store, experiment_id, record_id, kind="comparison")
            queries = store.queries(viewer="owner")
            queries.require_comparison_scope(record_id)
            result = ComparisonAssessments(store.records()).complete(record_id, require_terminal=True)
            # Sample details remain behind the audited record endpoint. Only the
            # existing aggregate allowlist and result identity leave this write.
            metadata = {key: result[key] for key in ("record_id", "content_hash", "sequence", "created_at")}
            metadata.update({key: result["value"][key] for key in ("status", "formal_ready", "promotion_authorized")})
            return {"comparison": metadata, "feedback": queries.comparison_feedback(result["record_id"]),
                    "execution_available": False}
        return await run_in_threadpool(run, complete, writable=True, with_artifacts=True)
