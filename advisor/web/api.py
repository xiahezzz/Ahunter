"""Local dashboard endpoints with bounded, durable operator actions."""

import base64
import hashlib
import hmac
from ipaddress import ip_address
import json
import math
import os
import re
import secrets
import sqlite3
import stat
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from advisor import paths as advisor_paths
from advisor.config import load_advisor_config, resolve_research_artifact_dir, resolve_research_catalog
from advisor.db.repository import connect as connect_database
from advisor.db.migrate import migrate_database
from advisor.ledger.importer import (
    LedgerCapacityError,
    LedgerConflictError,
)
from advisor.ledger.model import (
    LedgerTransaction,
    apply_transactions,
    ledger_transaction_sort_key,
    validate_ledger_transaction,
)
from advisor.ledger.store import LedgerStore
from advisor.market_daily.control import MarketDailyControlPlane, MarketRunError
from advisor.mx.chrome_launcher import (
    ChromeLaunchError,
    ChromeLauncher,
    DedicatedChromeLauncher,
)
from advisor.mx.information import (
    MxInformationCursorConflict,
    MxInformationNotFound,
    MxInformationStore,
    MxInformationUnavailable,
    MxInformationValidationError,
)
from advisor.mx.rid_authorization import (
    RidAuthorizationConflict,
    RidAuthorizationStore,
    RidAuthorizationUnavailable,
    RidAuthorizationValidationError,
)
from advisor.research.catalog import ManifestCatalog, load_catalog_from_directory
from advisor.research.execution_settings import ExecutionSettingsService, ExecutionSettingsConflict
from advisor.research.artifacts import ArtifactStore
from advisor.research.contracts import ResearchScope, ResearchSubject, VersionRef
from advisor.research.repository import ResearchRecord, ResearchRepository, ResearchRequest
from advisor.research.agent_access_publication import (
    AgentAccessPublicationConflict,
    AgentAccessPublicationNotFound,
    AgentAccessPublicationService,
    AgentAccessPublicationStorageError,
    AgentAccessPublicationValidationError,
)
from advisor.research.agent_instructions_publication import (
    AgentInstructionsPublicationConflict,
    AgentInstructionsPublicationNotFound,
    AgentInstructionsPublicationService,
    AgentInstructionsPublicationStorageError,
    AgentInstructionsPublicationValidationError,
)
from advisor.research.daily_teams import (
    DailyTeamSetNotFound,
    DailyTeamSetService,
    DailyTeamSetStorageError,
    DailyTeamSetValidationError,
)
from advisor.research.team_publication import (
    TeamPublicationConflict,
    TeamPublicationNotFound,
    TeamPublicationService,
    TeamPublicationStorageError,
    TeamPublicationValidationError,
)
from advisor.reporting.contracts import (
    StaleArchiveCursorError,
    read_active_verified_archive,
    read_verified_archive,
)
from advisor.services.manager import ServiceSetManager, statuses_as_dict


_COMPONENTS = ("collector", "market_updater", "advisor_scheduler", "frontend", "api")
_HEALTH_STATUSES = frozenset({"ok", "healthy", "running", "degraded", "failed", "stopped", "unknown"})
_MAX_HEALTH_BYTES = 64 * 1024
_CODE_RE = re.compile(r"(?:[0368]\d{5}|(?:SH|SZ|BJ)\d{6})\Z")
_DASHBOARD_CODE_RE = re.compile(r"[0368]\d{5}\Z")
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_ASSET_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_ACCOUNT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_CHART_TYPE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_LEDGER_ORDER_BY = "account_id, trade_date, transaction_id"
_MAX_LEDGER_REPLAY_ROWS = 10_000
_MAX_IMPORT_TRANSACTIONS = _MAX_LEDGER_REPLAY_ROWS
_MAX_LEDGER_REQUEST_BYTES = 512 * 1024
_MAX_LEDGER_FIELD_LENGTH = 4096
_LEDGER_PAYLOAD_KEYS = frozenset({
    "transaction_id", "account_id", "trade_date", "transaction_type", "code",
    "quantity", "price", "amount", "fees",
})
_REQUIRED_LEDGER_PAYLOAD_KEYS = _LEDGER_PAYLOAD_KEYS - {"account_id", "code"}
_MAX_LEDGER_PAYLOAD_KEYS = len(_LEDGER_PAYLOAD_KEYS)
_MAX_CHART_BYTES = 5 * 1024 * 1024
_MAX_CURRENT_RUN_CANDIDATES = 100
_MAX_CURRENT_QUALITY_CHECKS = 100
_MAX_QUALITY_DETAILS_BYTES = 64 * 1024
_MAX_CURRENT_REPORT_LINKS = 20
_MAX_REPORT_ARCHIVE_ROWS = 500
_MAX_CURRENT_PROFILE_LINKS = 100
_MAX_CURRENT_CHART_LINKS = 100
_MAX_MARKET_DAILY_RUNS = 100
_MAX_MARKET_DAILY_FAILURES = 100
_CURRENT_REPORT_LOOKBACK_DAYS = 30
_MAX_PROFILE_JSON_BYTES = 64 * 1024
_MAX_PROFILE_JSON_DEPTH = 8
_MAX_PROFILE_JSON_ITEMS = 500
_MAX_PROFILE_STRING_LENGTH = 4096
_MAX_DB_NAME_LENGTH = 256
_MAX_DB_INDUSTRY_LENGTH = 256
_MAX_DB_TIMESTAMP_LENGTH = 64
_MAX_SQLITE_INTEGER = 2**63 - 1
_MAX_RESEARCH_TEAM_REQUEST_BYTES = 64 * 1024
_RESEARCH_TEAM_PAYLOAD_KEYS = frozenset({"team_id", "scope", "title", "agent_ids"})
_MAX_RESEARCH_REQUEST_BYTES = 64 * 1024
_RESEARCH_REQUEST_PAYLOAD_KEYS = frozenset({"team_ref", "scope", "code", "submission_identity"})
_RESEARCH_RERUN_PAYLOAD_KEYS = frozenset({"submission_identity"})
_MAX_AGENT_ACCESS_REQUEST_BYTES = 64 * 1024
_AGENT_ACCESS_PAYLOAD_KEYS = frozenset({"data_access", "rid_version"})
_MAX_AGENT_INSTRUCTIONS_REQUEST_BYTES = 256 * 1024
_AGENT_INSTRUCTIONS_PAYLOAD_KEYS = frozenset({"instructions"})
_MAX_MX_RID_REQUEST_BYTES = 64 * 1024
_MX_RID_PAYLOAD_KEYS = frozenset({"rids", "version"})
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def create_app(
    state_dir: Path | None = None,
    db_path: Path | None = None,
    *,
    research_root: Path | None = None,
    research_config_path: Path | None = None,
    allowed_rids_path: Path | None = None,
    mx_events_db_path: Path | None = None,
    mx_media_root: Path | None = None,
    service_manager_factory: Callable[..., ServiceSetManager] | None = None,
    chrome_launcher_factory: Callable[[], ChromeLauncher] | None = None,
) -> FastAPI:
    """Create the local-only advisor API without creating state on read paths."""
    resolved_state_dir = Path(state_dir) if state_dir is not None else advisor_paths.advisor_data_dir()
    resolved_db_path = Path(db_path) if db_path is not None else resolved_state_dir / "advisor.sqlite"
    resolved_research_root = (research_root or advisor_paths.repo_root()).resolve()
    resolved_research_config_path = (
        Path(research_config_path) if research_config_path is not None else resolved_research_root / "config" / "advisor.yaml"
    ).resolve()
    # Do not resolve the final authoritative config path here: the store must
    # be able to reject a symlink rather than silently following it.
    resolved_allowed_rids_path = (
        Path(allowed_rids_path).expanduser()
        if allowed_rids_path is not None
        else resolved_research_root / "config" / "allowed-rids.yaml"
    )
    resolved_mx_events_db_path = (
        Path(mx_events_db_path).expanduser()
        if mx_events_db_path is not None
        else resolved_research_root / "data" / "state" / "events.sqlite"
    )
    resolved_mx_media_root = (
        Path(mx_media_root).expanduser().resolve()
        if mx_media_root is not None
        else resolved_research_root
    )
    report_cursor_secret = secrets.token_bytes(32)
    mx_information = MxInformationStore(
        resolved_mx_events_db_path,
        resolved_allowed_rids_path,
        repository_root=resolved_mx_media_root,
        token_secret=secrets.token_bytes(32),
    )
    chrome_launcher = (
        chrome_launcher_factory()
        if chrome_launcher_factory is not None
        else DedicatedChromeLauncher()
    )
    app = FastAPI(title="A Hunter Advisor")

    def research_services() -> tuple[ManifestCatalog, TeamPublicationService, DailyTeamSetService]:
        config = load_advisor_config(resolved_research_config_path)
        catalog_dir = resolve_research_catalog(config, resolved_research_root)
        return (
            load_catalog_from_directory(catalog_dir),
            TeamPublicationService(catalog_dir),
            DailyTeamSetService(config_path=resolved_research_config_path, root=resolved_research_root),
        )

    def agent_services() -> tuple[
        ManifestCatalog,
        AgentAccessPublicationService,
        AgentInstructionsPublicationService,
    ]:
        config = load_advisor_config(resolved_research_config_path)
        catalog_dir = resolve_research_catalog(config, resolved_research_root)
        return (
            load_catalog_from_directory(catalog_dir),
            AgentAccessPublicationService(catalog_dir, allowed_rids_path=resolved_allowed_rids_path),
            AgentInstructionsPublicationService(catalog_dir),
        )

    def service_manager() -> ServiceSetManager:
        options = {
            "root": resolved_research_root,
            "database_path": resolved_db_path,
            "events_database_path": resolved_mx_events_db_path,
            "allowed_rids_path": resolved_allowed_rids_path,
        }
        return service_manager_factory(**options) if service_manager_factory is not None else ServiceSetManager(**options)

    def control_repository(*, writable: bool) -> ResearchRepository:
        if writable:
            return ResearchRepository.open(resolved_db_path)
        if not resolved_db_path.is_file() or resolved_db_path.is_symlink():
            raise OSError("research control database is unavailable")
        return ResearchRepository(connect_database(resolved_db_path))

    def research_artifact_store(*, writable: bool = False) -> ArtifactStore:
        config = load_advisor_config(resolved_research_config_path)
        root = resolve_research_artifact_dir(config, resolved_research_root)
        if not writable and (not root.is_dir() or root.is_symlink()):
            raise OSError("research artifact store is unavailable")
        return ArtifactStore(root)

    from advisor.research.lagent_api import register_lagent_routes
    register_lagent_routes(app, repository_factory=control_repository, catalog_factory=lambda: research_services()[0],
                           artifact_store_factory=research_artifact_store, root=resolved_research_root)
    from advisor.research.experiments.api import register_experiment_routes
    register_experiment_routes(app, repository_factory=control_repository, catalog_factory=lambda: research_services()[0],
                               artifact_store_factory=research_artifact_store)

    @app.get("/api/health")
    def health() -> dict:
        return _health_payload(resolved_state_dir)

    @app.get("/api/current-state")
    def current_state() -> dict:
        return _current_state(resolved_state_dir, resolved_db_path, report_cursor_secret)

    @app.get("/api/services")
    def services() -> dict:
        connection = _read_connection(resolved_db_path)
        if connection is None:
            raise HTTPException(status_code=503, detail="服务状态数据库不可用")
        connection.close()
        manager = service_manager()
        return statuses_as_dict(manager.statuses())

    @app.get("/api/mx/listener/status")
    def mx_listener_status() -> dict:
        try:
            status = service_manager().mx_listener_status(datetime.now(_SHANGHAI))
        except (OSError, RuntimeError, ValueError):
            raise HTTPException(status_code=503, detail="MX Listener 状态暂不可读取") from None
        return _mx_listener_status_payload(status)

    @app.post("/api/mx/listener/start")
    async def start_mx_listener(request: Request) -> dict:
        try:
            await _empty_local_json_request(request)
            manager = service_manager()
            changed = manager.start_mx_listener()
            status = manager.mx_listener_status(datetime.now(_SHANGHAI))
        except _MxListenerRequestError:
            raise HTTPException(status_code=400, detail="MX Listener 操作请求无效") from None
        except (OSError, RuntimeError, ValueError):
            raise HTTPException(status_code=503, detail="MX Listener 暂不可启动") from None
        return {
            "changed": changed,
            "service": _mx_listener_status_payload(status),
            "message": "已请求启动 MX Listener；不会打开、登录或操作 Chrome 页面",
        }

    @app.post("/api/mx/listener/stop")
    async def stop_mx_listener(request: Request) -> dict:
        try:
            await _empty_local_json_request(request)
            manager = service_manager()
            changed = manager.stop_mx_listener()
            status = manager.mx_listener_status(datetime.now(_SHANGHAI))
        except _MxListenerRequestError:
            raise HTTPException(status_code=400, detail="MX Listener 操作请求无效") from None
        except (OSError, RuntimeError, ValueError):
            raise HTTPException(status_code=503, detail="MX Listener 暂不可停止") from None
        return {
            "changed": changed,
            "service": _mx_listener_status_payload(status),
            "message": "已请求停止 MX Listener；Chrome、RID 和已保存资讯均未改变",
        }

    @app.post("/api/mx/chrome/start")
    async def start_mx_chrome(request: Request) -> dict:
        try:
            await _empty_local_json_request(request, _MxChromeRequestError)
            result = await run_in_threadpool(chrome_launcher.start)
        except _MxChromeRequestError:
            raise HTTPException(status_code=400, detail="专用 Chrome 启动请求无效") from None
        except (ChromeLaunchError, OSError, RuntimeError, ValueError):
            raise HTTPException(status_code=503, detail="专用 Chrome 暂不可启动") from None
        if not result.changed:
            message = "专用 Chrome 已在运行；请在其中自行登录并打开 MX 页面"
        elif result.ready:
            message = "专用 Chrome 已启动；请在其中自行登录并打开 MX 页面"
        else:
            message = "已请求启动专用 Chrome；请稍候在其中自行登录并打开 MX 页面"
        return {"changed": result.changed, "ready": result.ready, "message": message}

    @app.get("/api/mx/rids")
    def mx_rids() -> dict:
        try:
            return RidAuthorizationStore(resolved_allowed_rids_path).read().as_dict()
        except (RidAuthorizationUnavailable, RidAuthorizationValidationError):
            raise HTTPException(status_code=503, detail="RID 授权配置暂不可读取") from None

    @app.put("/api/mx/rids")
    async def replace_mx_rids(request: Request) -> dict:
        try:
            payload = await _mx_rid_request_payload(request)
            updated = RidAuthorizationStore(resolved_allowed_rids_path).replace(
                payload["rids"], expected_version=payload["version"]
            )
        except _MxRidRequestError:
            raise HTTPException(status_code=400, detail="RID 授权输入无效") from None
        except RidAuthorizationConflict:
            raise HTTPException(status_code=409, detail="RID 配置已更新，请刷新后再提交") from None
        except RidAuthorizationValidationError:
            raise HTTPException(status_code=400, detail="RID 授权输入无效") from None
        except RidAuthorizationUnavailable:
            raise HTTPException(status_code=503, detail="RID 授权配置暂不可更新") from None
        return {
            **updated.as_dict(),
            "message": "RID 授权已更新，Listener 会在下一次配置轮询时热加载",
        }

    @app.get("/api/mx/events")
    def mx_events(
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = None,
        rid: list[str] = Query(default=[]),
        authorization: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        has_media: str | None = None,
        q: str | None = None,
    ) -> dict:
        try:
            return mx_information.list_events(
                limit=limit,
                cursor=cursor,
                rids=_mx_rid_filters(rid),
                authorization=authorization,
                start_at=_mx_timestamp_filter(start_at),
                end_at=_mx_timestamp_filter(end_at),
                has_media=_mx_media_filter(has_media),
                query=q,
            )
        except (MxInformationCursorConflict, MxInformationNotFound):
            raise HTTPException(status_code=409, detail="MX 资讯游标与当前筛选条件不一致") from None
        except MxInformationValidationError:
            raise HTTPException(status_code=400, detail="MX 资讯筛选条件无效") from None
        except MxInformationUnavailable:
            raise HTTPException(status_code=503, detail="MX 资讯暂不可读取") from None

    @app.get("/api/mx/events/{event_id}")
    def mx_event(event_id: str) -> dict:
        try:
            detail = mx_information.event_detail(event_id)
        except (MxInformationNotFound, MxInformationValidationError):
            raise HTTPException(status_code=404, detail="MX 资讯不存在") from None
        except MxInformationUnavailable:
            raise HTTPException(status_code=503, detail="MX 资讯暂不可读取") from None
        for block in detail["blocks"]:
            if block.get("type") == "media":
                block["href"] = f"/api/mx/events/{event_id}/media/{block['media_id']}"
        return detail

    @app.get("/api/mx/events/{event_id}/media/{media_id}")
    def mx_event_media(event_id: str, media_id: str):
        try:
            descriptor = mx_information.open_media(event_id, media_id)
        except (MxInformationNotFound, MxInformationValidationError):
            raise HTTPException(status_code=404, detail="MX 图片不存在") from None
        except MxInformationUnavailable:
            raise HTTPException(status_code=503, detail="MX 图片暂不可读取") from None
        return StreamingResponse(
            descriptor.stream(),
            media_type=descriptor.content_type,
            headers={"Content-Length": str(descriptor.size), "Cache-Control": "private, no-store"},
        )

    @app.get("/api/market-daily/status")
    def market_daily_status() -> dict:
        connection = _read_connection(resolved_db_path)
        if connection is None:
            raise HTTPException(status_code=503, detail="Market Daily 状态数据库不可用")
        try:
            return _market_daily_status_payload(connection, datetime.now(_SHANGHAI))
        except sqlite3.Error:
            raise HTTPException(status_code=503, detail="Market Daily 状态不可读取") from None
        finally:
            connection.close()

    @app.post("/api/market-daily/cold-start", status_code=202)
    def submit_market_daily_cold_start() -> dict:
        """Queue, but never execute, the one idempotent five-year cold start."""
        try:
            request = MarketDailyControlPlane(resolved_db_path).submit_cold_start_intent(
                datetime.now(_SHANGHAI)
            )
        except (MarketRunError, OSError, sqlite3.Error):
            raise HTTPException(status_code=503, detail="Market Daily 冷启动请求不可提交") from None
        return {
            "request_id": request.request_id,
            "request_status": request.status,
            "message": "冷启动请求已在本地队列中；Market Daily 服务会在 21:00 后执行",
        }

    @app.get("/api/market-daily/runs")
    def market_daily_runs(limit: int = Query(default=30, ge=1, le=_MAX_MARKET_DAILY_RUNS)) -> dict:
        connection = _read_connection(resolved_db_path)
        if connection is None:
            raise HTTPException(status_code=503, detail="Market Daily 状态数据库不可用")
        try:
            return {"runs": _market_daily_run_list(connection, limit)}
        except sqlite3.Error:
            raise HTTPException(status_code=503, detail="Market Daily 运行记录不可读取") from None
        finally:
            connection.close()

    @app.get("/api/market-daily/runs/{run_id}")
    def market_daily_run(run_id: str) -> dict:
        if not _RUN_ID_RE.fullmatch(run_id):
            raise HTTPException(status_code=404, detail="Market Daily 运行不存在")
        connection = _read_connection(resolved_db_path)
        if connection is None:
            raise HTTPException(status_code=503, detail="Market Daily 状态数据库不可用")
        try:
            payload = _market_daily_run_detail(connection, run_id)
        except sqlite3.Error:
            raise HTTPException(status_code=503, detail="Market Daily 运行记录不可读取") from None
        finally:
            connection.close()
        if payload is None:
            raise HTTPException(status_code=404, detail="Market Daily 运行不存在")
        return payload

    @app.get("/api/market-daily/runs/{run_id}/failures")
    def market_daily_failures(
        run_id: str,
        limit: int = Query(default=50, ge=1, le=_MAX_MARKET_DAILY_FAILURES),
        offset: int = Query(default=0, ge=0, le=10_000),
    ) -> dict:
        if not _RUN_ID_RE.fullmatch(run_id):
            raise HTTPException(status_code=404, detail="Market Daily 运行不存在")
        connection = _read_connection(resolved_db_path)
        if connection is None:
            raise HTTPException(status_code=503, detail="Market Daily 状态数据库不可用")
        try:
            return _market_daily_failures(connection, run_id, limit, offset)
        except sqlite3.Error:
            raise HTTPException(status_code=503, detail="Market Daily 失败项不可读取") from None
        finally:
            connection.close()

    @app.get("/api/reports")
    def reports(
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        connection = _read_connection(resolved_db_path)
        try:
            page = _page_db_backed_archives(
                connection,
                advisor_paths.reports_dir(),
                limit=limit,
                cursor=cursor,
                start_date=start_date,
                end_date=end_date,
                cursor_secret=report_cursor_secret,
            )
        except StaleArchiveCursorError:
            raise HTTPException(status_code=409, detail="report cursor stale") from None
        except ValueError:
            raise HTTPException(status_code=503, detail="report listing unavailable") from None
        finally:
            if connection is not None:
                connection.close()
        page["reports"] = [
            {
                **item,
                "href": f"/api/reports/{item['report_date']}/{item['report_type']}?run_id={item['run_id']}",
            }
            for item in page["items"]
        ]
        return page

    @app.get("/api/reports/{report_date}/{report_type}")
    def report(report_date: str, report_type: str, run_id: str = "initial") -> dict:
        if report_type not in {"premarket", "review"}:
            raise HTTPException(status_code=404, detail="report not found")
        connection = _read_connection(resolved_db_path)
        try:
            quality = _resolve_current_run_quality(
                connection,
                _shanghai_today().isoformat(),
                database_present=_database_entry_present(resolved_db_path),
            )
            eligible = _eligible_report_keys(
                connection,
                advisor_paths.reports_dir(),
                candidate_keys={(report_date, report_type, run_id)},
            )
        finally:
            if connection is not None:
                connection.close()
        if not quality["safe"]:
            raise HTTPException(status_code=503, detail="current report quality unavailable")
        if (report_date, report_type, run_id) not in eligible:
            raise HTTPException(status_code=404, detail="report not found")
        try:
            archive = read_verified_archive(
                advisor_paths.reports_dir(), report_date, report_type, run_id
            )
        except (OSError, ValueError, RuntimeError):
            raise HTTPException(status_code=404, detail="report not found") from None
        return archive

    @app.get("/api/research/cycles")
    def research_cycles(limit: int = Query(default=50, ge=1, le=100)) -> dict:
        return {"cycles": _list_research_cycles(advisor_paths.reports_dir(), limit=limit)}

    @app.get("/api/research/cycles/{report_date}/{cycle_id}")
    def research_cycle(report_date: str, cycle_id: str) -> dict:
        try:
            return _read_research_cycle(advisor_paths.reports_dir(), report_date, cycle_id)
        except (OSError, ValueError, json.JSONDecodeError):
            raise HTTPException(status_code=404, detail="research cycle not found") from None

    @app.get("/api/research/execution-settings")
    def research_execution_settings() -> dict:
        try:
            return ExecutionSettingsService(root=resolved_research_root, config_path=resolved_research_config_path).read()
        except (OSError, ValueError):
            raise HTTPException(status_code=503, detail="研究模型配置暂不可读取") from None

    @app.put("/api/research/execution-settings")
    async def update_research_execution_settings(request: Request) -> dict:
        try:
            _require_local_json_request(request, ValueError)
            payload = await _bounded_json_object(request, 2048, ValueError)
            if set(payload) != {"expected_policy_ref", "model", "reasoning_effort"} or any(not isinstance(v, str) for v in payload.values()):
                raise ValueError("invalid model settings")
            return ExecutionSettingsService(root=resolved_research_root, config_path=resolved_research_config_path).update(**payload)
        except ExecutionSettingsConflict:
            raise HTTPException(status_code=409, detail="模型配置已更新，请刷新后重试") from None
        except ValueError:
            raise HTTPException(status_code=400, detail="请选择有效的模型、推理强度并刷新配置后重试") from None
        except (OSError, DailyTeamSetStorageError):
            raise HTTPException(status_code=503, detail="研究模型配置暂不可保存") from None

    @app.get("/api/research/agents")
    def research_agents() -> dict:
        try:
            catalog, _publication, _daily_teams = research_services()
        except (OSError, ValueError):
            raise HTTPException(status_code=503, detail="研究团队目录暂不可读取") from None
        return {"agents": _research_agent_list(catalog)}

    @app.get("/api/research/data-catalog")
    def research_data_catalog() -> dict:
        try:
            catalog, _access_publication, _instructions_publication = agent_services()
        except (OSError, ValueError):
            raise HTTPException(status_code=503, detail="研究数据目录暂不可读取") from None
        return {"products": _research_data_catalog(catalog)}

    @app.get("/api/research/agent-access")
    def research_agent_access() -> dict:
        try:
            catalog, _access_publication, _instructions_publication = agent_services()
            authorization = RidAuthorizationStore(resolved_allowed_rids_path).read()
        except (RidAuthorizationUnavailable, RidAuthorizationValidationError):
            raise HTTPException(status_code=503, detail="RID 授权配置暂不可读取") from None
        except (OSError, ValueError):
            raise HTTPException(status_code=503, detail="研究数据目录暂不可读取") from None
        return {
            "rid_version": authorization.version,
            "agents": _research_agent_access_list(catalog, authorization.rids),
        }

    @app.post("/api/research/agents/{agent_ref}/access-revisions")
    async def publish_agent_access_revision(agent_ref: str, request: Request):
        try:
            payload = await _agent_access_request_payload(request)
            catalog, publication, _instructions_publication = agent_services()
            authorization = RidAuthorizationStore(resolved_allowed_rids_path).read()
            published = publication.publish(
                agent_ref,
                payload["data_access"],
                expected_rid_version=payload["rid_version"],
            )
        except _AgentAccessRequestError:
            raise HTTPException(status_code=400, detail="Agent 数据访问输入无效") from None
        except AgentAccessPublicationNotFound:
            raise HTTPException(status_code=404, detail="指定的 Agent 版本不存在") from None
        except AgentAccessPublicationConflict:
            raise HTTPException(status_code=409, detail="Agent 或 RID 配置已更新，请刷新后再提交") from None
        except AgentAccessPublicationValidationError:
            raise HTTPException(status_code=400, detail="Agent 数据访问输入无效") from None
        except (AgentAccessPublicationStorageError, RidAuthorizationUnavailable, RidAuthorizationValidationError):
            raise HTTPException(status_code=503, detail="Agent 数据访问发布暂不可用") from None
        except (OSError, ValueError):
            raise HTTPException(status_code=503, detail="研究数据目录暂不可读取") from None
        reloaded, _access_service, _instructions_service = agent_services()
        response = {
            "created": published.created,
            "agent": _research_agent_access_item(reloaded, published.agent, authorization.rids),
            "impact": {
                "fixed_team_refs": list(published.impact.fixed_team_refs),
                "revoked_rids": list(published.impact.revoked_rids),
            },
            "message": "已发布新的 Agent 数据访问版本；Team 和每日启用未改变",
        }
        return JSONResponse(response, status_code=201 if published.created else 200)

    @app.post("/api/research/agents/{agent_ref}/instruction-revisions")
    async def publish_agent_instructions_revision(agent_ref: str, request: Request):
        try:
            instructions = await _agent_instructions_request_payload(request)
            _catalog, _access_publication, publication = agent_services()
            authorization = RidAuthorizationStore(resolved_allowed_rids_path).read()
            published = publication.publish(agent_ref, instructions)
        except _AgentInstructionsRequestError:
            raise HTTPException(status_code=400, detail="Agent Instructions 输入无效") from None
        except AgentInstructionsPublicationNotFound:
            raise HTTPException(status_code=404, detail="指定的 Agent 版本不存在") from None
        except AgentInstructionsPublicationConflict:
            raise HTTPException(status_code=409, detail="Agent 已更新，请刷新后再提交") from None
        except AgentInstructionsPublicationValidationError:
            raise HTTPException(status_code=400, detail="Agent Instructions 输入无效") from None
        except (
            AgentInstructionsPublicationStorageError,
            RidAuthorizationUnavailable,
            RidAuthorizationValidationError,
        ):
            raise HTTPException(status_code=503, detail="Agent Instructions 发布暂不可用") from None
        except (OSError, ValueError):
            raise HTTPException(status_code=503, detail="研究数据目录暂不可读取") from None
        reloaded, _access_service, _instructions_service = agent_services()
        response = {
            "created": published.created,
            "agent": _research_agent_access_item(reloaded, published.agent, authorization.rids),
            "impact": {"fixed_team_refs": list(published.impact.fixed_team_refs)},
            "message": "已发布新的 Agent Instructions 版本；Team 和每日启用未改变",
        }
        return JSONResponse(response, status_code=201 if published.created else 200)

    @app.get("/api/research/teams")
    def research_teams() -> dict:
        try:
            catalog, _publication, daily_teams = research_services()
            enabled = daily_teams.read().team_refs
        except (DailyTeamSetValidationError, OSError, ValueError):
            raise HTTPException(status_code=503, detail="研究团队目录暂不可读取") from None
        return {"teams": _research_team_list(catalog, enabled)}

    @app.post("/api/research/teams")
    async def publish_research_team(request: Request):
        try:
            payload = await _research_team_request_payload(request)
            _catalog, publication, _daily_teams = research_services()
            published = publication.publish(
                payload["team_id"],
                payload["title"],
                payload["agent_ids"],
                scope=payload["scope"],
            )
        except _ResearchTeamRequestError:
            raise HTTPException(status_code=400, detail="研究团队输入无效") from None
        except TeamPublicationNotFound:
            raise HTTPException(status_code=404, detail="未找到指定的研究 Agent") from None
        except TeamPublicationConflict:
            raise HTTPException(status_code=409, detail="该成员组合已用于其他研究团队") from None
        except TeamPublicationValidationError:
            raise HTTPException(status_code=400, detail="研究团队输入无效") from None
        except TeamPublicationStorageError:
            raise HTTPException(status_code=503, detail="研究团队发布暂不可用") from None
        except (OSError, ValueError):
            raise HTTPException(status_code=503, detail="研究团队目录暂不可读取") from None
        response = {
            "created": published.created,
            "team": _research_team_item(published.team),
        }
        return JSONResponse(response, status_code=201 if published.created else 200)

    @app.post("/api/research/requests")
    async def submit_research_request(request: Request):
        repository: ResearchRepository | None = None
        try:
            payload = await _research_request_payload(request)
            catalog, _publication, _daily_teams = research_services()
            team = catalog.team(payload["team_ref"])
            subject = _request_subject(payload["scope"], payload["code"])
            catalog.validate_subject_for_team(team.team, subject)
            repository = control_repository(writable=True)
            with repository.transaction():
                submitted = repository.submit_request(
                    team_ref=team.team,
                    subject=subject,
                    origin="web",
                    submission_identity=payload["submission_identity"],
                )
            return JSONResponse({"request": _research_request_item(submitted)}, status_code=202)
        except _ResearchRequestPayloadError:
            raise HTTPException(status_code=400, detail="研究请求输入无效") from None
        except ValueError:
            raise HTTPException(status_code=400, detail="研究请求与 Team 范围不匹配") from None
        except (OSError, sqlite3.Error):
            raise HTTPException(status_code=503, detail="研究请求控制面暂不可用") from None
        finally:
            if repository is not None:
                repository.close()

    @app.get("/api/research/requests/current")
    def current_research_requests() -> dict:
        repository: ResearchRepository | None = None
        try:
            repository = control_repository(writable=False)
            lease = repository.service_lease_status()
            requests = repository.current_requests()
            active_test = repository.connection.execute("SELECT work_id FROM research_work_queue WHERE kind='experiment' AND state='running' LIMIT 1").fetchone()
            return {
                "service": {
                    "state": "idle" if lease["online"] and active_test is None and not any(item.status == "running" for item in requests) else ("running" if lease["online"] else "offline"),
                    "heartbeat_at": lease["heartbeat_at"],
                    "active_request_id": next((item.request_id for item in requests if item.status == "running"), None),
                    "active_test_id": active_test[0] if active_test else None,
                    "queued_count": repository.connection.execute("SELECT COUNT(*) FROM research_work_queue WHERE state='queued'").fetchone()[0],
                    "reason_code": None,
                },
                "requests": [_research_request_item(item) for item in requests],
            }
        except (OSError, sqlite3.Error, ValueError):
            raise HTTPException(status_code=503, detail="研究请求控制面暂不可读取") from None
        finally:
            if repository is not None:
                repository.close()

    @app.get("/api/research/requests/{request_id}")
    def research_request_status(request_id: str) -> dict:
        """Return one durable Request, including its terminal no-report state."""
        repository: ResearchRepository | None = None
        try:
            repository = control_repository(writable=False)
            return {"request": _research_request_item(repository.get_request(request_id))}
        except ValueError:
            raise HTTPException(status_code=404, detail="研究请求不存在") from None
        except (OSError, sqlite3.Error):
            raise HTTPException(status_code=503, detail="研究请求状态暂不可读取") from None
        finally:
            if repository is not None:
                repository.close()

    @app.post("/api/research/requests/{request_id}/cancel")
    async def cancel_research_request(request_id: str, request: Request):
        repository: ResearchRepository | None = None
        try:
            await _empty_local_json_request(request, _ResearchRequestPayloadError)
            repository = control_repository(writable=True)
            with repository.transaction():
                cancelled = repository.request_cancel(request_id)
            return {"request": _research_request_item(cancelled)}
        except _ResearchRequestPayloadError:
            raise HTTPException(status_code=400, detail="取消研究请求输入无效") from None
        except ValueError as error:
            detail = "研究请求不存在或已结束" if "unknown" in str(error) or "terminal" in str(error) else "研究请求当前不可取消"
            raise HTTPException(status_code=409, detail=detail) from None
        except (OSError, sqlite3.Error):
            raise HTTPException(status_code=503, detail="研究请求控制面暂不可用") from None
        finally:
            if repository is not None:
                repository.close()

    @app.post("/api/research/requests/{request_id}/rerun")
    async def rerun_research_request(request_id: str, request: Request):
        repository: ResearchRepository | None = None
        try:
            payload = await _research_rerun_payload(request)
            repository = control_repository(writable=True)
            with repository.transaction():
                rerun = repository.rerun_request(request_id, submission_identity=payload["submission_identity"])
            return JSONResponse({"request": _research_request_item(rerun)}, status_code=202)
        except _ResearchRequestPayloadError:
            raise HTTPException(status_code=400, detail="再次研究输入无效") from None
        except ValueError as error:
            detail = "只有已结束的研究记录可以再次研究" if "terminal" in str(error) else "研究请求不存在"
            raise HTTPException(status_code=409, detail=detail) from None
        except (OSError, sqlite3.Error):
            raise HTTPException(status_code=503, detail="研究请求控制面暂不可用") from None
        finally:
            if repository is not None:
                repository.close()

    @app.get("/api/research/records")
    def research_records(
        team_id: str | None = None,
        team_ref: str | None = None,
        status: list[str] | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        repository: ResearchRepository | None = None
        try:
            repository = control_repository(writable=False)
            records = repository.list_records(
                team_id=team_id,
                team_ref=team_ref,
                statuses=tuple(status) if status is not None else ("passed", "partial"),
                limit=limit,
                offset=offset,
            )
            store: ArtifactStore | None = None
            if any(item.status in {"passed", "partial"} for item in records):
                try:
                    store = research_artifact_store()
                except OSError:
                    # A Record remains listable even when a historical report
                    # artifact is unavailable; expose only that bounded state.
                    store = None
            return {
                "records": [
                    _research_record_item(item, quality_summary=_research_quality_summary(item, store))
                    for item in records
                ],
                "limit": limit,
                "offset": offset,
            }
        except ValueError:
            raise HTTPException(status_code=400, detail="研究记录筛选条件无效") from None
        except (OSError, sqlite3.Error):
            raise HTTPException(status_code=503, detail="研究记录暂不可读取") from None
        finally:
            if repository is not None:
                repository.close()

    @app.get("/api/research/records/{record_id}")
    def research_record_detail(record_id: str) -> dict:
        repository: ResearchRepository | None = None
        try:
            repository = control_repository(writable=False)
            record = repository.get_record(record_id)
            payload = _research_record_item(record)
            if record.status in {"passed", "partial"}:
                store = research_artifact_store()
                if not record.report_json_hash or not record.report_markdown_hash:
                    raise ValueError("report references missing")
                report_json = store.read_json(record.report_json_hash)
                markdown = store.read_bytes(record.report_markdown_hash).decode("utf-8")
                if not isinstance(report_json, dict) or len(markdown) > 2_000_000 or not _safe_research_report(report_json, markdown):
                    raise ValueError("report content invalid")
                payload["quality_summary"] = _quality_summary_from_report(report_json)
                payload["report"] = {"json": report_json, "markdown": markdown}
            return payload
        except ValueError as error:
            if "unknown" in str(error):
                raise HTTPException(status_code=404, detail="研究记录不存在") from None
            raise HTTPException(status_code=409, detail="研究报告不可用或未通过完整性校验") from None
        except (OSError, sqlite3.Error, UnicodeDecodeError):
            raise HTTPException(status_code=503, detail="研究记录暂不可读取") from None
        finally:
            if repository is not None:
                repository.close()

    @app.put("/api/research/daily-teams/{team_ref}")
    def enable_daily_research_team(team_ref: str) -> dict:
        try:
            _catalog, _publication, daily_teams = research_services()
            result = daily_teams.enable(team_ref)
        except DailyTeamSetNotFound:
            raise HTTPException(status_code=404, detail="指定的研究团队版本不存在") from None
        except DailyTeamSetValidationError:
            raise HTTPException(status_code=400, detail="研究团队版本无效") from None
        except DailyTeamSetStorageError:
            raise HTTPException(status_code=503, detail="每日 Team 配置暂不可更新") from None
        except (OSError, ValueError):
            raise HTTPException(status_code=503, detail="研究团队目录暂不可读取") from None
        return {"daily_teams": list(result.team_refs), "message": "已启用每日 Team"}

    @app.delete("/api/research/daily-teams/{team_ref}")
    def disable_daily_research_team(team_ref: str) -> dict:
        try:
            _catalog, _publication, daily_teams = research_services()
            result = daily_teams.disable(team_ref)
        except DailyTeamSetNotFound:
            raise HTTPException(status_code=404, detail="指定的研究团队版本不存在") from None
        except DailyTeamSetValidationError:
            raise HTTPException(status_code=400, detail="研究团队版本无效") from None
        except DailyTeamSetStorageError:
            raise HTTPException(status_code=503, detail="每日 Team 配置暂不可更新") from None
        except (OSError, ValueError):
            raise HTTPException(status_code=503, detail="研究团队目录暂不可读取") from None
        return {"daily_teams": list(result.team_refs), "message": "已取消每日启用"}

    @app.get("/api/profiles")
    def profiles(limit: int = Query(default=50, ge=1, le=100)) -> dict:
        connection = _read_connection(resolved_db_path)
        try:
            return {"profiles": _read_profile_links(connection)[:limit]}
        finally:
            if connection is not None:
                connection.close()

    @app.get("/api/profiles/{code}")
    def profile(code: str) -> dict:
        if not _CODE_RE.fullmatch(code):
            raise HTTPException(status_code=404, detail="profile not found")
        connection = _read_connection(resolved_db_path)
        try:
            payload = _read_profile(connection, code)
        finally:
            if connection is not None:
                connection.close()
        if payload is None:
            raise HTTPException(status_code=404, detail="profile not found")
        return payload

    @app.get("/api/charts")
    def charts(limit: int = Query(default=50, ge=1, le=100)) -> dict:
        connection = _read_connection(resolved_db_path)
        try:
            return {"charts": _read_chart_links(connection, resolved_state_dir)[:limit]}
        finally:
            if connection is not None:
                connection.close()

    @app.get("/api/charts/{asset_id}")
    def chart(asset_id: str):
        if not _ASSET_ID_RE.fullmatch(asset_id):
            raise HTTPException(status_code=404, detail="chart not found")
        connection = _read_connection(resolved_db_path)
        try:
            descriptor = _open_chart_descriptor(connection, resolved_state_dir, asset_id)
        finally:
            if connection is not None:
                connection.close()
        if descriptor is None:
            raise HTTPException(status_code=404, detail="chart not found")
        return StreamingResponse(_stream_descriptor(descriptor), media_type="image/png")

    @app.get("/api/ledger/transactions")
    def ledger_transactions(limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0, le=1000)) -> dict:
        connection = _read_connection(resolved_db_path)
        try:
            rows = _read_ledger_records(connection, limit=limit, offset=offset)
            try:
                ledger = _read_ledger_state(connection)
            except _LedgerCapacityError:
                raise HTTPException(status_code=503, detail="ledger history exceeds replay limit") from None
            return {"transactions": rows, "ledger": ledger}
        finally:
            if connection is not None:
                connection.close()

    @app.post("/api/ledger/transactions", status_code=201)
    async def add_ledger_transaction(request: Request) -> dict:
        try:
            payload = await _ledger_request_payload(request)
            account_id, transaction = _transaction_from_payload(payload)
            return _write_ledger_transactions(resolved_db_path, [(account_id, transaction)], "manual")
        except _LedgerValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        except _LedgerCapacityError:
            raise HTTPException(status_code=503, detail="ledger history exceeds replay limit") from None
        except _LedgerConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @app.post("/api/ledger/import", status_code=201)
    async def import_ledger_transactions(request: Request) -> dict:
        try:
            payload = await _ledger_request_payload(request)
            rows = _validate_ledger_import_shape(payload)
            transactions = [_transaction_from_payload(item) for item in rows]
            return _write_ledger_transactions(resolved_db_path, transactions, "import")
        except _LedgerValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        except _LedgerCapacityError:
            raise HTTPException(status_code=503, detail="ledger history exceeds replay limit") from None
        except _LedgerConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    return app


class _ResearchTeamRequestError(ValueError):
    pass


class _ResearchRequestPayloadError(ValueError):
    pass


class _AgentAccessRequestError(ValueError):
    pass


class _AgentInstructionsRequestError(ValueError):
    pass


class _MxListenerRequestError(ValueError):
    pass


class _MxChromeRequestError(ValueError):
    pass


class _MxRidRequestError(ValueError):
    pass


def _mx_rid_filters(values: list[str]) -> tuple[int, ...]:
    parsed: list[int] = []
    for value in values:
        if not isinstance(value, str):
            raise MxInformationValidationError("invalid RID filter")
        for item in value.split(","):
            if not re.fullmatch(r"[1-9][0-9]{0,15}", item):
                raise MxInformationValidationError("invalid RID filter")
            number = int(item)
            if number > 2**53 - 1:
                raise MxInformationValidationError("invalid RID filter")
            parsed.append(number)
    if len(parsed) > 100 or len(set(parsed)) != len(parsed):
        raise MxInformationValidationError("invalid RID filter")
    return tuple(sorted(parsed))


def _mx_timestamp_filter(value: str | None) -> int | None:
    if value is None:
        return None
    if re.fullmatch(r"[0-9]{1,16}", value):
        parsed = int(value)
        if parsed > 2**63 - 1:
            raise MxInformationValidationError("invalid time filter")
        return parsed
    try:
        parsed_time = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise MxInformationValidationError("invalid time filter") from error
    if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
        raise MxInformationValidationError("invalid time filter")
    return int(parsed_time.timestamp() * 1000)


def _mx_media_filter(value: str | None) -> bool | None:
    if value is None:
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    raise MxInformationValidationError("invalid media filter")


async def _mx_rid_request_payload(request: Request) -> dict[str, object]:
    _require_local_json_request(request)
    payload = await _bounded_json_object(request, _MAX_MX_RID_REQUEST_BYTES, _MxRidRequestError)
    if set(payload) != _MX_RID_PAYLOAD_KEYS:
        raise _MxRidRequestError("invalid payload shape")
    rids = payload["rids"]
    if not isinstance(rids, list) or len(rids) > 1_000 or any(type(value) is not int for value in rids):
        raise _MxRidRequestError("invalid RID values")
    if not isinstance(payload["version"], str) or len(payload["version"]) != 64:
        raise _MxRidRequestError("invalid version")
    return payload


async def _empty_local_json_request(
    request: Request,
    error_type: type[ValueError] = _MxListenerRequestError,
) -> None:
    _require_local_json_request(request, error_type)
    payload = await _bounded_json_object(request, 1_024, error_type)
    if payload:
        raise error_type("unexpected request fields")


def _require_local_json_request(
    request: Request,
    error_type: type[ValueError] = _MxRidRequestError,
) -> None:
    if not _is_loopback_request(request):
        raise error_type("non-local request")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise error_type("invalid content type")
    origin = request.headers.get("origin")
    if origin is not None and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise error_type("cross origin request")
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site not in {None, "same-origin", "none"}:
        raise error_type("cross origin request")


def _is_loopback_request(request: Request) -> bool:
    """Require both a loopback Host and a loopback transport peer.

    The explicit TestClient identity is accepted only with its synthetic host
    (or a loopback host), so production requests cannot opt into the exception
    through caller-controlled HTTP headers.
    """

    hostname = (request.url.hostname or "").lower()
    host_is_loopback = hostname == "localhost"
    if not host_is_loopback:
        try:
            host_is_loopback = ip_address(hostname).is_loopback
        except ValueError:
            host_is_loopback = False
    client_host = request.client.host if request.client is not None else ""
    if client_host == "testclient":
        return hostname == "testserver" or host_is_loopback
    try:
        peer = ip_address(client_host.split("%", 1)[0])
    except ValueError:
        return False
    peer_is_loopback = peer.is_loopback
    if not peer_is_loopback and peer.version == 6 and peer.ipv4_mapped is not None:
        peer_is_loopback = peer.ipv4_mapped.is_loopback
    return host_is_loopback and peer_is_loopback


async def _bounded_json_object(
    request: Request,
    maximum_bytes: int,
    error_type: type[ValueError],
) -> dict[str, object]:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) < 0 or int(content_length) > maximum_bytes:
                raise error_type("request body too large")
        except ValueError as error:
            raise error_type("invalid request body") from error
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum_bytes:
            raise error_type("request body too large")
        body.extend(chunk)
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise error_type("invalid JSON") from error
    if not isinstance(payload, dict):
        raise error_type("invalid payload")
    return payload


async def _research_team_request_payload(request: Request) -> dict[str, object]:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_RESEARCH_TEAM_REQUEST_BYTES:
                raise _ResearchTeamRequestError("request body too large")
        except ValueError as error:
            raise _ResearchTeamRequestError("invalid request body") from error
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_RESEARCH_TEAM_REQUEST_BYTES:
            raise _ResearchTeamRequestError("request body too large")
        body.extend(chunk)
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _ResearchTeamRequestError("invalid JSON") from error
    if not isinstance(payload, dict) or set(payload) != _RESEARCH_TEAM_PAYLOAD_KEYS:
        raise _ResearchTeamRequestError("invalid payload shape")
    if (
        not isinstance(payload["team_id"], str)
        or not isinstance(payload["title"], str)
        or not isinstance(payload["scope"], str)
    ):
        raise _ResearchTeamRequestError("invalid Team fields")
    try:
        ResearchScope(payload["scope"])
    except ValueError as error:
        raise _ResearchTeamRequestError("invalid Team scope") from error
    agent_ids = payload["agent_ids"]
    if not isinstance(agent_ids, list) or any(not isinstance(item, str) for item in agent_ids):
        raise _ResearchTeamRequestError("invalid Agent IDs")
    return payload


async def _research_request_payload(request: Request) -> dict[str, object]:
    _require_local_json_request(request, _ResearchRequestPayloadError)
    payload = await _bounded_json_object(request, _MAX_RESEARCH_REQUEST_BYTES, _ResearchRequestPayloadError)
    if set(payload) != _RESEARCH_REQUEST_PAYLOAD_KEYS:
        raise _ResearchRequestPayloadError("invalid payload shape")
    team_ref, scope, code, identity = (
        payload["team_ref"], payload["scope"], payload["code"], payload["submission_identity"]
    )
    if not isinstance(team_ref, str) or not isinstance(scope, str) or not isinstance(identity, str) or not identity.strip() or len(identity) > 256:
        raise _ResearchRequestPayloadError("invalid request fields")
    try:
        if str(VersionRef.parse(team_ref)) != team_ref:
            raise ValueError("not canonical")
        ResearchScope(scope)
    except (TypeError, ValueError) as error:
        raise _ResearchRequestPayloadError("invalid request scope") from error
    if code is not None and (not isinstance(code, str) or len(code) != 6):
        raise _ResearchRequestPayloadError("invalid subject code")
    return payload


async def _research_rerun_payload(request: Request) -> dict[str, str]:
    _require_local_json_request(request, _ResearchRequestPayloadError)
    payload = await _bounded_json_object(request, _MAX_RESEARCH_REQUEST_BYTES, _ResearchRequestPayloadError)
    if set(payload) != _RESEARCH_RERUN_PAYLOAD_KEYS:
        raise _ResearchRequestPayloadError("invalid rerun payload")
    identity = payload["submission_identity"]
    if not isinstance(identity, str) or not identity.strip() or len(identity) > 256:
        raise _ResearchRequestPayloadError("invalid rerun identity")
    return {"submission_identity": identity}


def _request_subject(scope_value: object, code: object) -> ResearchSubject:
    try:
        scope = ResearchScope(scope_value)
    except (TypeError, ValueError) as error:
        raise _ResearchRequestPayloadError("invalid scope") from error
    try:
        return ResearchSubject(scope=scope, code=code)
    except ValueError as error:
        raise _ResearchRequestPayloadError("invalid subject") from error


async def _agent_access_request_payload(request: Request) -> dict[str, object]:
    _require_local_json_request(request, _AgentAccessRequestError)
    payload = await _bounded_json_object(request, _MAX_AGENT_ACCESS_REQUEST_BYTES, _AgentAccessRequestError)
    if set(payload) != _AGENT_ACCESS_PAYLOAD_KEYS:
        raise _AgentAccessRequestError("invalid payload shape")
    version = payload["rid_version"]
    if (
        not isinstance(version, str)
        or len(version) != 64
        or any(character not in "0123456789abcdef" for character in version)
    ):
        raise _AgentAccessRequestError("invalid RID version")
    access = payload["data_access"]
    if not isinstance(access, list) or not 1 <= len(access) <= 50:
        raise _AgentAccessRequestError("invalid data access")
    normalized: list[dict[str, object]] = []
    for item in access:
        if not isinstance(item, dict) or set(item) not in ({"product"}, {"product", "feed_scope"}):
            raise _AgentAccessRequestError("invalid data access item")
        product = item.get("product")
        if not isinstance(product, str) or len(product) > 72:
            raise _AgentAccessRequestError("invalid product reference")
        try:
            if str(VersionRef.parse(product)) != product:
                raise ValueError("not canonical")
        except (TypeError, ValueError) as error:
            raise _AgentAccessRequestError("invalid product reference") from error
        normalized_item: dict[str, object] = {"product": product}
        if "feed_scope" in item:
            scope = item["feed_scope"]
            if not isinstance(scope, dict) or set(scope) != {"rids"}:
                raise _AgentAccessRequestError("invalid Feed scope")
            rids = scope.get("rids")
            if (
                not isinstance(rids, list)
                or not 1 <= len(rids) <= 100
                or any(type(rid) is not int or not 0 < rid <= 2**53 - 1 for rid in rids)
                or len(set(rids)) != len(rids)
            ):
                raise _AgentAccessRequestError("invalid Feed scope")
            normalized_item["feed_scope"] = {"rids": sorted(rids)}
        normalized.append(normalized_item)
    if len({item["product"] for item in normalized}) != len(normalized):
        raise _AgentAccessRequestError("duplicate product access")
    return {"data_access": normalized, "rid_version": version}


async def _agent_instructions_request_payload(request: Request) -> str:
    _require_local_json_request(request, _AgentInstructionsRequestError)
    payload = await _bounded_json_object(
        request,
        _MAX_AGENT_INSTRUCTIONS_REQUEST_BYTES,
        _AgentInstructionsRequestError,
    )
    if set(payload) != _AGENT_INSTRUCTIONS_PAYLOAD_KEYS:
        raise _AgentInstructionsRequestError("invalid payload shape")
    instructions = payload["instructions"]
    if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > 20_000:
        raise _AgentInstructionsRequestError("invalid Agent Instructions")
    return instructions


def _research_agent_list(catalog: ManifestCatalog) -> list[dict[str, object]]:
    latest: dict[str, object] = {}
    for agent in catalog.agents.values():
        current = latest.get(agent.agent.id)
        if current is None or agent.agent.version > current.agent.version:  # type: ignore[union-attr]
            latest[agent.agent.id] = agent
    return [
        {
            "agent_id": agent_id,
            "agent_ref": str(agent.agent),
            "scope": agent.scope.value,
            "title": agent.title,
            "summary": _agent_summary(agent.instructions),
        }
        for agent_id, agent in sorted(latest.items())
    ]


_PROVIDER_NAMES = {
    "local-market": "本地日线行情库",
    "local-mx": "本地 MX 资讯库",
    "public-a-share": "公开 A 股数据源",
}


def _research_data_catalog(catalog: ManifestCatalog) -> list[dict[str, object]]:
    grouped: dict[str, list[object]] = defaultdict(list)
    for product in catalog.products.values():
        grouped[product.product.id].append(product)
    result: list[dict[str, object]] = []
    for product_id, versions in sorted(grouped.items()):
        ordered = sorted(versions, key=lambda item: item.product.version)  # type: ignore[union-attr]
        items = [_research_product_item(item) for item in ordered]
        result.append({
            "product_id": product_id,
            "latest": items[-1],
            "history": items,
        })
    return result


def _research_product_item(product) -> dict[str, object]:
    return {
        "product_ref": str(product.product),
        "title": product.title,
        "dependencies": [str(item) for item in product.dependencies],
        "providers": [
            {"provider_id": provider, "display_name": _PROVIDER_NAMES.get(provider, provider)}
            for provider in product.providers
        ],
        "supports_feed_scope": bool(product.feed_scope),
        "feed_scope_contract": product.feed_scope,
    }


def _research_agent_access_list(
    catalog: ManifestCatalog,
    authorized_rids: tuple[int, ...],
) -> list[dict[str, object]]:
    grouped: dict[str, list[object]] = defaultdict(list)
    for agent in catalog.agents.values():
        grouped[agent.agent.id].append(agent)
    result: list[dict[str, object]] = []
    for agent_id, versions in sorted(grouped.items()):
        ordered = sorted(versions, key=lambda item: item.agent.version)  # type: ignore[union-attr]
        items = [_research_agent_access_item(catalog, item, authorized_rids) for item in ordered]
        result.append({"agent_id": agent_id, "latest": items[-1], "history": items})
    return result


def _research_agent_access_item(
    catalog: ManifestCatalog,
    agent,
    authorized_rids: tuple[int, ...],
) -> dict[str, object]:
    allowed = set(authorized_rids)
    access_items: list[dict[str, object]] = []
    assigned: set[int] = set()
    revoked: list[int] = []
    for access in agent.product_accesses:
        item: dict[str, object] = {"product_ref": str(access.product)}
        if access.feed_scope is not None:
            rid_items = []
            for rid in access.feed_scope.rids:
                state = "current" if rid in allowed else "revoked"
                rid_items.append({"rid": rid, "authorization": state})
                assigned.add(rid)
                if state == "revoked":
                    revoked.append(rid)
            item["feed_scope"] = {"rids": rid_items}
        access_items.append(item)
    team_refs = sorted(
        str(team.team)
        for team in catalog.teams.values()
        if any(member == agent.agent for member in team.agents)
    )
    return {
        "agent_ref": str(agent.agent),
        "scope": agent.scope.value,
        "title": agent.title,
        "summary": _agent_summary(agent.instructions),
        "instructions": agent.instructions,
        "data_access": access_items,
        "used_by_team_refs": team_refs,
        "unassigned_rids": sorted(allowed - assigned),
        "blocked_reasons": [f"RID {rid} 的授权已撤销" for rid in sorted(set(revoked))],
        "read_only": {
            "query_budget": agent.query_budget,
            "max_result_rows": agent.max_result_rows,
            "implementation": agent.implementation,
        },
    }


def _research_team_list(catalog: ManifestCatalog, enabled_refs: tuple[str, ...]) -> list[dict[str, object]]:
    enabled_by_id = {VersionRef.parse(reference).id: reference for reference in enabled_refs}
    grouped: dict[str, list[object]] = defaultdict(list)
    for team in catalog.teams.values():
        grouped[team.team.id].append(team)
    items: list[dict[str, object]] = []
    for team_id, versions in sorted(grouped.items()):
        ordered = sorted(versions, key=lambda item: item.team.version)  # type: ignore[union-attr]
        latest = ordered[-1]
        items.append(
            {
                "team_id": team_id,
                "latest": _research_team_item(latest),
                "history": [_research_team_item(item) for item in ordered],
                "daily_enabled_ref": enabled_by_id.get(team_id),
            }
        )
    return items


def _research_team_item(team) -> dict[str, object]:
    return {
        "team_ref": str(team.team),
        "scope": team.scope.value,
        "title": team.title,
        "agents": [str(agent) for agent in team.agents],
    }


def _research_subject_item(subject: ResearchSubject) -> dict[str, object]:
    return {"scope": subject.scope.value, "code": subject.code, "name": subject.name}


def _research_request_item(request: ResearchRequest) -> dict[str, object]:
    return {
        "mode": request.mode,
        "request_id": request.request_id,
        "team_ref": str(request.team),
        "scope": request.scope.value,
        "subject": _research_subject_item(request.subject),
        "origin": request.origin,
        "requested_at": request.requested_at.isoformat(),
        "accepted_at": request.accepted_at.isoformat(),
        "boundary_at": request.boundary.as_of.isoformat() if request.boundary is not None else None,
        "status": request.status,
        "phase": request.phase,
        "agents_completed": request.agents_completed,
        "agents_total": request.agents_total,
        "decision_stage": request.decision_stage,
        "reason_code": request.reason_code,
        "published_at": request.published_at.isoformat() if request.published_at is not None else None,
        "rerun_of": request.rerun_of,
        "last_updated_at": request.last_updated_at.isoformat(),
        "can_cancel": request.status in {"queued", "running"},
        "can_rerun": request.status in {"passed", "partial", "blocked", "failed", "cancelled"},
    }


def _research_record_item(
    record: ResearchRecord,
    *,
    quality_summary: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "mode": record.mode,
        "record_id": record.record_id,
        "request_id": record.request_id,
        "team_ref": str(record.team),
        "scope": record.scope.value,
        "subject": _research_subject_item(record.subject),
        "origin": record.origin,
        "requested_at": record.requested_at.isoformat(),
        "accepted_at": record.accepted_at.isoformat(),
        "boundary_at": record.boundary.as_of.isoformat() if record.boundary is not None else None,
        "status": record.status,
        "phase": record.phase,
        "reason_code": record.reason_code,
        "published_at": record.published_at.isoformat() if record.published_at is not None else None,
        "rerun_of": record.rerun_of,
        "has_report": record.status in {"passed", "partial"},
        "quality_summary": quality_summary or _research_quality_summary(record, None),
    }


def _research_quality_summary(record: ResearchRecord, store: ArtifactStore | None) -> dict[str, object]:
    """A list-safe quality projection; it never exposes report content."""
    if record.status not in {"passed", "partial"}:
        return {"status": "not_applicable", "limitations_count": 0, "blocked_insights": 0}
    if store is None or not record.report_json_hash:
        return {"status": "unavailable", "limitations_count": 0, "blocked_insights": 0}
    try:
        report = store.read_json(record.report_json_hash)
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {"status": "unavailable", "limitations_count": 0, "blocked_insights": 0}
    if not isinstance(report, dict) or not _safe_research_report(report, ""):
        return {"status": "unavailable", "limitations_count": 0, "blocked_insights": 0}
    return _quality_summary_from_report(report)


def _quality_summary_from_report(report: dict[str, object]) -> dict[str, object]:
    quality = report.get("evidence_quality")
    if not isinstance(quality, dict) or quality.get("status") not in {"passed", "warning", "blocked"}:
        return {"status": "unavailable", "limitations_count": 0, "blocked_insights": 0}
    limitations = quality.get("limitations")
    limitations_count = len(limitations) if isinstance(limitations, list) and len(limitations) <= 50 else 0
    insights = report.get("insights")
    blocked_insights = sum(
        1 for item in insights
        if isinstance(item, dict) and item.get("status") == "blocked"
    ) if isinstance(insights, list) and len(insights) <= 20 else 0
    return {
        "status": quality["status"],
        "limitations_count": limitations_count,
        "blocked_insights": blocked_insights,
    }


def _safe_research_report(payload: object, markdown: str, *, depth: int = 0) -> bool:
    from advisor.research.reporting.safety import is_public_report
    return is_public_report(payload, markdown, depth=depth)


def _mx_listener_status_payload(status) -> dict[str, object]:
    """Return only the three-dimensional Listener status contract.

    Logs and all runtime connection identifiers remain available to the local
    service manager, but are intentionally not copied onto this web surface.
    """
    return {
        "service_id": status.service_id,
        "status": status.status,
        "details": status.details,
        **status.extra,
    }


def _agent_summary(instructions: str) -> str:
    first_line = next((line.strip() for line in instructions.splitlines() if line.strip()), "")
    return first_line[:240]


def _health_payload(state_dir: Path) -> dict:
    components = {component: "unknown" for component in _COMPONENTS}
    components["api"] = "ok"
    snapshot = _load_health_snapshot(state_dir / "health.json")
    for component in _COMPONENTS:
        if component == "api":
            continue
        value = snapshot.get(component)
        if isinstance(value, str) and value in _HEALTH_STATUSES:
            components[component] = value
    return {"status": "ok", "service": "advisor-api", **components}


def _list_research_cycles(reports_root: Path, *, limit: int) -> list[dict]:
    items: list[dict] = []
    try:
        date_dirs = sorted((item for item in reports_root.iterdir() if item.is_dir() and not item.is_symlink()), reverse=True)
    except OSError:
        return []
    for date_dir in date_dirs:
        for cycle_dir in sorted((item for item in date_dir.iterdir() if item.is_dir() and not item.is_symlink()), reverse=True):
            if len(items) >= limit:
                return items
            try:
                payload = _read_research_cycle(reports_root, date_dir.name, cycle_dir.name)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            items.append({
                "report_date": date_dir.name,
                "cycle_id": cycle_dir.name,
                "status": payload.get("status"),
                "subject": payload.get("subject"),
                "href": f"/api/research/cycles/{date_dir.name}/{cycle_dir.name}",
            })
    return items


def _read_research_cycle(reports_root: Path, report_date: str, cycle_id: str) -> dict:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", cycle_id):
        raise ValueError("invalid Research Cycle path")
    root = (reports_root / report_date / cycle_id).resolve()
    report_base = reports_root.resolve()
    if not root.is_relative_to(report_base) or root.is_symlink() or not root.is_dir():
        raise ValueError("Research Cycle directory is unavailable")
    cycle_path = root / "cycle.json"
    index_path = root / "index.md"
    marker_path = root / "complete.json"
    if any(path.is_symlink() or not path.is_file() for path in (cycle_path, index_path, marker_path)):
        raise ValueError("Research Cycle publication is incomplete")
    payload = json.loads(cycle_path.read_text(encoding="utf-8"))
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(marker, dict):
        raise ValueError("Research Cycle publication is invalid")
    expected_files = marker.get("files")
    if isinstance(expected_files, dict):
        for relative, expected_hash in expected_files.items():
            target = (root / relative).resolve()
            if not target.is_relative_to(root) or target.is_symlink() or not target.is_file():
                raise ValueError("Research Cycle artifact is unavailable")
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected_hash:
                raise ValueError("Research Cycle artifact hash mismatch")
    return payload


def _load_health_snapshot(path: Path) -> dict:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_HEALTH_BYTES:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    nested = payload.get("components")
    return nested if isinstance(nested, dict) else payload


app = create_app()


def _current_state(state_dir: Path, db_path: Path, report_cursor_secret: bytes) -> dict:
    today = _shanghai_today().isoformat()
    health = _health_payload(state_dir)
    database_present = _database_entry_present(db_path)
    connection = _read_connection(db_path)
    try:
        try:
            ledger = _read_current_ledger_state(connection)
        except _LedgerCapacityError:
            ledger = {**_empty_ledger_state(), "status": "degraded"}
        flows = _read_flows(connection)
        last_successful_data_update = _last_successful_data_update(connection)
        current_quality = _resolve_current_run_quality(
            connection,
            today,
            database_present=database_present,
        )
        profiles, profile_list = _read_profile_links_with_status(connection)
        charts, chart_list = _read_chart_links_with_status(connection, state_dir)
        report_start = (
            date.fromisoformat(today) - timedelta(days=_CURRENT_REPORT_LOOKBACK_DAYS)
        ).isoformat()
        report_keys = _eligible_report_keys(
            connection,
            advisor_paths.reports_dir(),
            start_date=report_start,
            end_date=today,
        )
        try:
            eligible_reports = {
                _report_key(item)
                for item in _read_verified_report_items(advisor_paths.reports_dir(), report_keys)
            }
            report_verification_failed = False
        except (OSError, ValueError, RuntimeError):
            eligible_reports = set()
            report_verification_failed = True
    finally:
        if connection is not None:
            connection.close()

    reports, report_list = _read_report_links(today, report_cursor_secret, eligible_reports)
    if report_verification_failed:
        reports, report_list = [], {"status": "degraded", "truncated": True}
    premarket = _read_today_report(today, "premarket", eligible_reports)
    review = _read_today_report(today, "review", eligible_reports)
    premarket_status = _report_status(premarket)
    review_status = _report_status(review)
    quality_blocks_empty_state = current_quality["available"] and not current_quality["safe"]
    unbacked_premarket = premarket is None and _has_today_verified_report(today, "premarket")
    unbacked_review = review is None and _has_today_verified_report(today, "review")
    premarket_blocked = (
        premarket_status == "blocked"
        or quality_blocks_empty_state
        or (unbacked_premarket and not current_quality["safe"])
        or (premarket is not None and not current_quality["safe"])
    )
    review_blocked = (
        review_status == "blocked"
        or quality_blocks_empty_state
        or (unbacked_review and not current_quality["safe"])
        or (review is not None and not current_quality["safe"])
    )
    checks = current_quality["blocking_checks"]
    return {
        "today": today,
        "last_successful_data_update": last_successful_data_update,
        "advice": [] if premarket_blocked else premarket["json"].get("advice", []) if premarket else [],
        "advice_status": "blocked" if premarket_blocked else premarket_status,
        "review": {
            "status": "blocked" if review_blocked else review_status,
            "items": [] if review_blocked else review["json"].get("reviews", []) if review else [],
        },
        "ledger": ledger,
        "flows": flows,
        "blocking_quality_checks": checks,
        "reports": reports,
        "report_list": report_list,
        "profiles": profiles,
        "profile_list": profile_list,
        "charts": charts,
        "chart_list": chart_list,
        "health": health,
    }


def _market_daily_status_payload(connection: sqlite3.Connection, now: datetime) -> dict:
    latest = connection.execute(
        """
        SELECT run_id, run_type, status, target_session, total_items, completed_items,
               failed_items, created_at, started_at, finished_at
        FROM market_daily_runs ORDER BY created_at DESC, run_id DESC LIMIT 1
        """
    ).fetchone()
    pending_cold = int(
        connection.execute(
            "SELECT COUNT(*) FROM market_daily_requests WHERE request_type = 'cold_start' AND status IN ('pending', 'claimed')"
        ).fetchone()[0]
    )
    lease = connection.execute(
        "SELECT expires_at FROM market_daily_service_leases WHERE lease_name = 'market-daily'"
    ).fetchone()
    lease_expires_at = _safe_market_timestamp(lease[0]) if lease else None
    lease_active = bool(lease_expires_at and datetime.fromisoformat(lease_expires_at) > now)
    session = connection.execute("SELECT MAX(trade_date) FROM trading_sessions").fetchone()[0]
    latest_update = connection.execute(
        "SELECT MAX(fetched_at) FROM market_daily WHERE quality_status = 'passed'"
    ).fetchone()[0]
    if latest is None:
        state = "waiting_for_cold_start" if pending_cold else "idle"
        run_payload: dict[str, object] = {
            "run_id": None,
            "mode": None,
            "target_session": None,
            "total_items": 0,
            "completed_items": 0,
            "failed_items": 0,
            "progress": 0.0,
            "run_status": None,
        }
    else:
        total = max(0, int(latest[4]))
        completed = max(0, int(latest[5]))
        failed = max(0, int(latest[6]))
        state = latest[2] if latest[2] in {"pending", "running", "partial", "complete", "failed", "cancelled"} else "failed"
        run_payload = {
            "run_id": latest[0],
            "mode": latest[1],
            "target_session": latest[3],
            "total_items": total,
            "completed_items": completed,
            "failed_items": failed,
            "progress": round(min(1.0, (completed + failed) / total), 6) if total else 0.0,
            "run_status": latest[2],
        }
    next_due = now.astimezone(_SHANGHAI).replace(hour=21, minute=0, second=0, microsecond=0)
    if now >= next_due:
        next_due += timedelta(days=1)
    return {
        "state": state,
        "service_status": "running" if lease_active else "offline",
        "lease_active": lease_active,
        "lease_expires_at": lease_expires_at,
        "latest_observed_session": session if _valid_dashboard_date(session) else None,
        "last_successful_update": _safe_market_timestamp(latest_update),
        "next_scheduled_at": next_due.isoformat(),
        **run_payload,
    }


def _market_daily_run_list(connection: sqlite3.Connection, limit: int) -> list[dict]:
    rows = connection.execute(
        """
        SELECT run_id, run_type, status, target_session, start_date, end_date,
               total_items, completed_items, failed_items, created_at, started_at, finished_at
        FROM market_daily_runs ORDER BY created_at DESC, run_id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_market_daily_run_row(row) for row in rows]


def _market_daily_run_detail(connection: sqlite3.Connection, run_id: str) -> dict | None:
    row = connection.execute(
        """
        SELECT run_id, run_type, status, target_session, start_date, end_date,
               total_items, completed_items, failed_items, created_at, started_at, finished_at
        FROM market_daily_runs WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    counts = connection.execute(
        """
        SELECT status, COUNT(*) AS count FROM market_daily_run_items
        WHERE run_id = ? GROUP BY status ORDER BY status
        """,
        (run_id,),
    ).fetchall()
    return {
        **_market_daily_run_row(row),
        "item_status_counts": {item["status"]: int(item["count"]) for item in counts},
        "failures_href": f"/api/market-daily/runs/{run_id}/failures",
    }


def _market_daily_run_row(row: sqlite3.Row) -> dict:
    total = max(0, int(row["total_items"]))
    completed = max(0, int(row["completed_items"]))
    failed = max(0, int(row["failed_items"]))
    return {
        "run_id": row["run_id"],
        "mode": row["run_type"],
        "status": row["status"],
        "target_session": row["target_session"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "total_items": total,
        "completed_items": completed,
        "failed_items": failed,
        "progress": round(min(1.0, (completed + failed) / total), 6) if total else 0.0,
        "created_at": _safe_market_timestamp(row["created_at"]),
        "started_at": _safe_market_timestamp(row["started_at"]),
        "finished_at": _safe_market_timestamp(row["finished_at"]),
    }


def _market_daily_failures(
    connection: sqlite3.Connection, run_id: str, limit: int, offset: int
) -> dict:
    exists = connection.execute(
        "SELECT 1 FROM market_daily_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if exists is None:
        return {"items": [], "total": 0, "offset": offset, "next_offset": None}
    total = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM market_daily_run_items
            WHERE run_id = ? AND status IN ('source_missing', 'conflicted')
            """,
            (run_id,),
        ).fetchone()[0]
    )
    rows = connection.execute(
        """
        SELECT code, status, attempts, selected_source, last_error, updated_at
        FROM market_daily_run_items
        WHERE run_id = ? AND status IN ('source_missing', 'conflicted')
        ORDER BY code LIMIT ? OFFSET ?
        """,
        (run_id, limit, offset),
    ).fetchall()
    items = [
        {
            "code": row["code"],
            "status": row["status"],
            "attempts": int(row["attempts"]),
            "selected_source": row["selected_source"],
            "error": (row["last_error"] or "")[:320] or None,
            "updated_at": _safe_market_timestamp(row["updated_at"]),
        }
        for row in rows
    ]
    next_offset = offset + len(items) if offset + len(items) < total else None
    return {"items": items, "total": total, "offset": offset, "next_offset": next_offset}


def _safe_market_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _parse_shanghai_datetime(value).isoformat()
    except ValueError:
        return None


def _read_connection(db_path: Path) -> sqlite3.Connection | None:
    try:
        if db_path.is_symlink() or not db_path.is_file():
            return None
        connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error:
        return None


def _database_entry_present(db_path: Path) -> bool:
    try:
        db_path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _read_ledger_state(connection: sqlite3.Connection | None) -> dict:
    if connection is None:
        return _empty_ledger_state()
    try:
        rows = _read_capped_ledger_history(connection)
    except sqlite3.Error:
        return _empty_ledger_state()
    return _derive_ledger_state(rows, connection)


def _read_current_ledger_state(connection: sqlite3.Connection | None) -> dict:
    if connection is None:
        return {**_empty_ledger_state(), "status": "unknown"}
    try:
        rows = _read_capped_ledger_history(connection)
    except sqlite3.Error:
        return {**_empty_ledger_state(), "status": "degraded"}
    state = _derive_ledger_state(rows, connection)
    if rows and state == _empty_ledger_state():
        return {**state, "status": "degraded"}
    return {**state, "status": "ok"}


def _last_successful_data_update(connection: sqlite3.Connection | None) -> str | None:
    if connection is None:
        return None
    queries = (
        "SELECT MAX(fetched_at) FROM market_daily WHERE quality_status = 'passed'",
        "SELECT MAX(as_of) FROM events_normalized WHERE quality_status = 'passed'",
        """
        SELECT MAX(analyst_outputs.as_of)
        FROM analyst_outputs
        JOIN advisor_runs ON advisor_runs.run_id = analyst_outputs.run_id
        WHERE advisor_runs.status = 'passed'
        """,
        "SELECT MAX(created_at) FROM ledger_transactions",
    )
    candidates = []
    for query in queries:
        try:
            value = connection.execute(query).fetchone()[0]
            if value is not None:
                candidates.append(_parse_shanghai_datetime(value))
        except (sqlite3.Error, ValueError, TypeError):
            continue
    return max(candidates).isoformat() if candidates else None


def _read_ledger_records(connection: sqlite3.Connection | None, *, limit: int | None = None, offset: int = 0) -> list[dict]:
    if connection is None:
        return []
    try:
        query = f"""
            SELECT transaction_id, account_id, trade_date, transaction_type, code, quantity, price, amount, fees
            FROM ledger_transactions
            ORDER BY {_LEDGER_ORDER_BY}
        """
        parameters: tuple[int, int] = ()
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            parameters = (limit, offset)
        rows = connection.execute(query, parameters).fetchall()
    except sqlite3.Error:
        return []
    records = []
    for row in rows:
        try:
            if not _valid_account_id(row["account_id"]):
                raise ValueError("invalid account")
            _ledger_transaction_from_row(row)
        except ValueError:
            return []
        records.append(dict(row))
    return records


class _LedgerValidationError(ValueError):
    pass


class _LedgerConflictError(ValueError):
    pass


class _LedgerCapacityError(ValueError):
    pass


def _read_capped_ledger_history(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = connection.execute(
        f"""
        SELECT transaction_id, account_id, trade_date, transaction_type, code, quantity, price, amount, fees
        FROM ledger_transactions
        ORDER BY {_LEDGER_ORDER_BY}
        LIMIT ?
        """,
        (_MAX_LEDGER_REPLAY_ROWS + 1,),
    ).fetchall()
    if len(rows) > _MAX_LEDGER_REPLAY_ROWS:
        raise _LedgerCapacityError("ledger history exceeds replay limit")
    return rows


def _transaction_from_payload(payload: object) -> tuple[str, LedgerTransaction]:
    _validate_ledger_row_shape(payload)
    account_id = payload.get("account_id", "default")
    if not isinstance(account_id, str) or not _ACCOUNT_ID_RE.fullmatch(account_id):
        raise _LedgerValidationError("invalid account")
    transaction = LedgerTransaction(
        transaction_id=payload["transaction_id"],
        trade_date=payload["trade_date"],
        transaction_type=payload["transaction_type"],
        code=payload.get("code"),
        quantity=payload["quantity"],
        price=payload["price"],
        amount=payload["amount"],
        fees=payload["fees"],
    )
    try:
        validate_ledger_transaction(transaction)
    except ValueError as error:
        raise _LedgerValidationError("invalid transaction") from error
    return account_id, transaction


async def _ledger_request_payload(request: Request) -> object:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_LEDGER_REQUEST_BYTES:
                raise HTTPException(
                    status_code=413, detail="ledger request body is too large"
                )
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid ledger request body") from None
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_LEDGER_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="ledger request body is too large")
        body.extend(chunk)
    try:
        return json.loads(bytes(body))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError):
        raise HTTPException(status_code=422, detail="invalid ledger request body") from None


def _validate_ledger_import_shape(payload: object) -> list[dict]:
    if not isinstance(payload, list):
        raise _LedgerValidationError("invalid ledger request shape")
    if len(payload) > _MAX_IMPORT_TRANSACTIONS:
        raise _LedgerValidationError("too many transactions")
    for row in payload:
        _validate_ledger_row_shape(row)
    return payload


def _validate_ledger_row_shape(payload: object) -> None:
    if not isinstance(payload, dict) or len(payload) > _MAX_LEDGER_PAYLOAD_KEYS:
        raise _LedgerValidationError("invalid ledger request shape")
    for key, value in payload.items():
        if not isinstance(key, str):
            raise _LedgerValidationError("invalid ledger request shape")
        if len(key) > _MAX_LEDGER_FIELD_LENGTH or (
            isinstance(value, str) and len(value) > _MAX_LEDGER_FIELD_LENGTH
        ):
            raise _LedgerValidationError("ledger field is too long")
        if isinstance(value, (dict, list)):
            raise _LedgerValidationError("invalid ledger request shape")
    keys = set(payload)
    if keys - _LEDGER_PAYLOAD_KEYS or not _REQUIRED_LEDGER_PAYLOAD_KEYS <= keys:
        raise _LedgerValidationError("invalid ledger request shape")


def _write_ledger_transactions(
    db_path: Path,
    transactions: list[tuple[str, LedgerTransaction]],
    source: str,
) -> dict:
    if not transactions:
        raise _LedgerValidationError("transactions are required")
    try:
        imports = LedgerStore(db_path).import_many(
            transactions, source=source, as_of=datetime.now(_SHANGHAI),
            max_rows=_MAX_LEDGER_REPLAY_ROWS
        )
    except LedgerCapacityError as error:
        raise _LedgerCapacityError(str(error)) from error
    except LedgerConflictError as error:
        raise _LedgerConflictError(str(error)) from error
    except ValueError as error:
        raise _LedgerValidationError(str(error)) from error
    try:
        connection = sqlite3.connect(db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
    except sqlite3.Error as error:
        raise _LedgerConflictError("ledger unavailable") from error
    try:
        rows = [
            {
                "transaction_id": transaction.transaction_id,
                "account_id": account_id,
                "trade_date": transaction.trade_date,
                "transaction_type": transaction.transaction_type,
                "code": transaction.code,
                "quantity": transaction.quantity,
                "price": transaction.price,
                "amount": transaction.amount,
                "fees": transaction.fees,
            }
            for account_id, transaction in sorted(
                transactions,
                key=lambda item: (item[0], ledger_transaction_sort_key(item[1])),
            )
        ]
        result = {
            "transactions": rows,
            "ledger": _read_ledger_state(connection),
            "quality_flags": [flag for item in imports for flag in item.quality_flags],
        }
    finally:
        connection.close()
    return result


def _derive_ledger_state(rows: list[sqlite3.Row], connection: sqlite3.Connection | None) -> dict:
    transactions_by_account: dict[str, list[LedgerTransaction]] = defaultdict(list)
    for row in rows:
        try:
            if not _valid_account_id(row["account_id"]):
                raise ValueError("invalid account")
            transaction = _ledger_transaction_from_row(row)
        except ValueError:
            return _empty_ledger_state()
        transactions_by_account[row["account_id"]].append(transaction)

    positions: dict[str, dict[str, float | int]] = {}
    accounts = []
    cash = 0.0
    realized_pnl = 0.0
    for account_id in sorted(transactions_by_account):
        try:
            state = apply_transactions(
                sorted(transactions_by_account[account_id], key=ledger_transaction_sort_key)
            )
        except ValueError:
            return _empty_ledger_state()
        if not all(_finite_number(value) for value in (state.cash, state.realized_pnl, *state.cost_basis.values())):
            return _empty_ledger_state()
        accounts.append(
            {
                "account_id": account_id,
                "cash": state.cash,
                "realized_pnl": state.realized_pnl,
            }
        )
        cash += state.cash
        realized_pnl += state.realized_pnl
        if not _finite_number(cash) or not _finite_number(realized_pnl):
            return _empty_ledger_state()
        for code, quantity in state.positions.items():
            item = positions.setdefault(code, {"code": code, "quantity": 0, "cost_basis": 0.0})
            item["quantity"] += quantity
            item["cost_basis"] += state.cost_basis[code]

    closes = _latest_closes(connection, positions)
    unrealized_pnl = 0.0
    rendered_positions = []
    for code in sorted(positions):
        item = positions[code]
        close = closes.get(code)
        try:
            market_value = float(item["quantity"]) * close if close is not None else None
        except OverflowError:
            return _empty_ledger_state()
        if market_value is not None and not _finite_number(market_value):
            return _empty_ledger_state()
        item["market_price"] = close
        item["market_value"] = market_value
        item["unrealized_pnl"] = market_value - float(item["cost_basis"]) if market_value is not None else 0.0
        unrealized_pnl += float(item["unrealized_pnl"])
        rendered_positions.append(item)
    return {
        "cash": cash,
        "positions": rendered_positions,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "accounts": accounts,
    }


def _ledger_transaction_from_row(row: sqlite3.Row) -> LedgerTransaction:
    transaction = LedgerTransaction(
        transaction_id=row["transaction_id"],
        trade_date=row["trade_date"],
        transaction_type=row["transaction_type"],
        code=row["code"],
        quantity=row["quantity"],
        price=row["price"],
        amount=row["amount"],
        fees=row["fees"],
    )
    validate_ledger_transaction(transaction)
    return transaction


def _latest_closes(connection: sqlite3.Connection | None, positions: dict[str, dict]) -> dict[str, float]:
    if connection is None or not positions:
        return {}
    placeholders = ",".join("?" for _ in positions)
    try:
        rows = connection.execute(
            f"""
            SELECT market_daily.code, market_daily.close
            FROM market_daily
            JOIN (
                SELECT code, MAX(trade_date) AS trade_date
                FROM market_daily
                WHERE code IN ({placeholders})
                GROUP BY code
            ) latest ON latest.code = market_daily.code AND latest.trade_date = market_daily.trade_date
            """,
            tuple(positions),
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {row["code"]: float(row["close"]) for row in rows if _finite_number(row["close"])}


def _empty_ledger_state() -> dict:
    return {"cash": 0.0, "positions": [], "realized_pnl": 0.0, "unrealized_pnl": 0.0, "accounts": []}


def _read_flows(connection: sqlite3.Connection | None) -> dict:
    return {
        "information": _flow_status(connection, "events_normalized"),
        "capital": _flow_status(connection, "market_daily"),
        "analyst": _flow_status(connection, "analyst_outputs"),
    }


def _flow_status(connection: sqlite3.Connection | None, table: str) -> dict:
    if connection is None:
        return {"status": "unknown", "count": 0}
    try:
        count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return {"status": "unknown", "count": 0}
    return {"status": "ok", "count": int(count)}


def _resolve_current_run_quality(
    connection: sqlite3.Connection | None, today: str, *, database_present: bool
) -> dict:
    if connection is None:
        return {"safe": False, "available": database_present, "blocking_checks": [], "active_run": None}
    try:
        parsed_today = date.fromisoformat(today)
        prefixes = tuple((parsed_today + timedelta(days=offset)).isoformat() for offset in (-1, 0, 1))
        rows = connection.execute(
            """
            SELECT run_id, run_type, as_of, status, started_at
            FROM advisor_runs
            WHERE (substr(as_of, 1, 10) IN (?, ?, ?)
               OR substr(started_at, 1, 10) IN (?, ?, ?))
              AND run_type IN ('premarket', 'review', 'failure')
            LIMIT ?
            """,
            (*prefixes, *prefixes, _MAX_CURRENT_RUN_CANDIDATES + 1),
        ).fetchall()
    except (sqlite3.Error, ValueError):
        return {"safe": False, "available": True, "blocking_checks": [], "active_run": None}
    if len(rows) > _MAX_CURRENT_RUN_CANDIDATES:
        return {"safe": False, "available": True, "blocking_checks": [], "active_run": None}
    candidates: list[tuple[datetime, str, sqlite3.Row]] = []
    for row in rows:
        try:
            as_of = _parse_shanghai_datetime(row["as_of"])
            started = _parse_shanghai_datetime(row["started_at"])
        except (KeyError, ValueError):
            return {"safe": False, "available": True, "blocking_checks": [], "active_run": None}
        if (
            not isinstance(row["run_id"], str)
            or not row["run_id"]
            or row["run_type"] not in {"premarket", "review", "failure"}
            or row["status"] not in {"passed", "failed", "blocked", "running"}
        ):
            return {"safe": False, "available": True, "blocking_checks": [], "active_run": None}
        if as_of.date().isoformat() == today:
            candidates.append((started, row["run_id"], row))
    if not candidates:
        return {"safe": False, "available": True, "blocking_checks": [], "active_run": None}
    latest = max(candidates, key=lambda item: (item[0], item[1]))[2]
    try:
        check_rows = connection.execute(
            """
            SELECT check_name, severity, status, details_json, created_at
            FROM data_quality_checks
            WHERE run_id = ?
            ORDER BY created_at DESC, check_name ASC
            LIMIT ?
            """,
            (latest["run_id"], _MAX_CURRENT_QUALITY_CHECKS + 1),
        ).fetchall()
    except sqlite3.Error:
        return {"safe": False, "available": True, "blocking_checks": [], "active_run": dict(latest)}
    if len(check_rows) > _MAX_CURRENT_QUALITY_CHECKS:
        return {"safe": False, "available": True, "blocking_checks": [], "active_run": dict(latest)}
    checks = [dict(row) for row in check_rows]
    normalized_checks = []
    try:
        valid_checks = bool(checks)
        for check in checks:
            created_at = _parse_shanghai_datetime(check["created_at"])
            if not (
                isinstance(check["check_name"], str)
                and 0 < len(check["check_name"]) <= 128
                and check["severity"] in {"blocking", "warning", "info"}
                and check["status"] in {"passed", "failed"}
                and _valid_quality_details(check["details_json"])
            ):
                valid_checks = False
                break
            normalized_checks.append(
                {
                    "check_name": check["check_name"],
                    "severity": check["severity"],
                    "status": check["status"],
                    "created_at": created_at.isoformat(),
                }
            )
    except (KeyError, ValueError):
        valid_checks = False
    blocking = (
        [
            check
            for check in normalized_checks
            if check.get("severity") == "blocking" and check.get("status") == "failed"
        ]
        if valid_checks
        else []
    )
    return {
        "safe": latest["run_type"] != "failure" and latest["status"] == "passed" and valid_checks and not blocking,
        "available": True,
        "blocking_checks": blocking,
        "active_run": dict(latest),
    }


def _parse_shanghai_datetime(value: object) -> datetime:
    if not _bounded_timestamp_string(value):
        raise ValueError("invalid run timestamp")
    if len(value) == 10:
        try:
            return datetime.combine(date.fromisoformat(value), time.min, tzinfo=_SHANGHAI)
        except ValueError as error:
            raise ValueError("invalid run timestamp") from error
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))
    except ValueError as error:
        raise ValueError("invalid run timestamp") from error
    return parsed.replace(tzinfo=_SHANGHAI) if parsed.tzinfo is None else parsed.astimezone(_SHANGHAI)


def _shanghai_today() -> date:
    return datetime.now(_SHANGHAI).date()


def _valid_quality_details(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        if len(value.encode("utf-8")) > _MAX_QUALITY_DETAILS_BYTES:
            return False
        payload = json.loads(value)
        if not isinstance(payload, dict):
            return False
        _validate_profile_json_value(payload)
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
        return False
    return True


def _read_profile_links(connection: sqlite3.Connection | None) -> list[dict]:
    return _read_profile_links_with_status(connection, dashboard_contract=False)[0]


def _read_profile_links_with_status(
    connection: sqlite3.Connection | None, *, dashboard_contract: bool = True
) -> tuple[list[dict], dict]:
    if connection is None:
        return [], {"status": "degraded"}
    try:
        rows = connection.execute(
            """
            SELECT stock_profiles.code, securities.name
            FROM stock_profiles
            LEFT JOIN securities ON securities.code = stock_profiles.code
            ORDER BY stock_profiles.code
            LIMIT ?
            """,
            (_MAX_CURRENT_PROFILE_LINKS + 1 if dashboard_contract else _MAX_CURRENT_PROFILE_LINKS,),
        ).fetchall()
    except sqlite3.Error:
        return [], {"status": "degraded"}
    if dashboard_contract and len(rows) > _MAX_CURRENT_PROFILE_LINKS:
        return [], {"status": "degraded"}
    profiles = []
    for row in rows:
        code_pattern = _DASHBOARD_CODE_RE if dashboard_contract else _CODE_RE
        if not isinstance(row["code"], str) or not code_pattern.fullmatch(row["code"]):
            return [], {"status": "degraded"}
        profile = _read_profile(connection, row["code"])
        if profile is None:
            return [], {"status": "degraded"}
        profiles.append(
            {"code": row["code"], "name": profile["name"], "href": f"/api/profiles/{row['code']}"}
        )
    return profiles, {"status": "ok"}


def _read_profile(connection: sqlite3.Connection | None, code: str) -> dict | None:
    if connection is None:
        return None
    try:
        row = connection.execute(
            """
            SELECT stock_profiles.code, securities.name, securities.industry,
                   thesis_json, information_flow_json, capital_flow_json, fundamentals_json,
                   analyst_flow_json, ledger_exposure_json, assets_json, stock_profiles.updated_at
            FROM stock_profiles
            LEFT JOIN securities ON securities.code = stock_profiles.code
            WHERE stock_profiles.code = ?
            """,
            (code,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        name = row["name"] or row["code"]
        if not _bounded_db_string(name, _MAX_DB_NAME_LENGTH):
            raise ValueError("invalid profile scalar")
        industry = row["industry"]
        if industry is not None and not _bounded_db_string(industry, _MAX_DB_INDUSTRY_LENGTH):
            raise ValueError("invalid profile scalar")
        if not _valid_db_timestamp(row["updated_at"]):
            raise ValueError("invalid profile scalar")
        thesis = _load_json_field(row["thesis_json"], dict)
        information_flow = _load_json_field(row["information_flow_json"], list)
        capital_flow = _load_json_field(row["capital_flow_json"], list)
        fundamentals = _load_json_field(row["fundamentals_json"], dict)
        analyst_flow = _load_json_field(row["analyst_flow_json"], list)
        ledger_exposure = _load_json_field(row["ledger_exposure_json"], dict)
        assets = _load_json_field(row["assets_json"], list)
    except ValueError:
        return None
    if not all(isinstance(item, str) for item in information_flow + capital_flow + analyst_flow + assets):
        return None
    if any(Path(asset).is_absolute() or ".." in Path(asset).parts for asset in assets):
        return None
    return {
        "code": row["code"],
        "name": name,
        "industry": industry,
        "thesis": thesis,
        "information_flow": information_flow,
        "capital_flow": capital_flow,
        "fundamentals": fundamentals,
        "analyst_flow": analyst_flow,
        "ledger_exposure": ledger_exposure,
        "assets": assets,
        "updated_at": row["updated_at"],
    }


def _load_json_field(value: object, expected_type: type) -> object:
    if not isinstance(value, str):
        raise ValueError("invalid stored json")
    try:
        if len(value.encode("utf-8")) > _MAX_PROFILE_JSON_BYTES:
            raise ValueError("invalid stored json")
        payload = json.loads(value)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("invalid stored json") from error
    if not isinstance(payload, expected_type):
        raise ValueError("invalid stored json")
    _validate_profile_json_value(payload)
    return payload


def _validate_profile_json_value(payload: object) -> None:
    item_count = 0

    def visit(value: object, depth: int) -> None:
        nonlocal item_count
        item_count += 1
        if item_count > _MAX_PROFILE_JSON_ITEMS or depth > _MAX_PROFILE_JSON_DEPTH:
            raise ValueError("invalid stored json")
        if isinstance(value, str):
            if len(value) > _MAX_PROFILE_STRING_LENGTH:
                raise ValueError("invalid stored json")
            return
        if value is None or isinstance(value, bool):
            return
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not _finite_number(value):
                raise ValueError("invalid stored json")
            return
        if isinstance(value, list):
            for item in value:
                visit(item, depth + 1)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str) or len(key) > _MAX_PROFILE_STRING_LENGTH:
                    raise ValueError("invalid stored json")
                visit(item, depth + 1)
            return
        raise ValueError("invalid stored json")

    visit(payload, 0)


def _read_chart_links(connection: sqlite3.Connection | None, state_dir: Path) -> list[dict]:
    return _read_chart_links_with_status(connection, state_dir, dashboard_contract=False)[0]


def _read_chart_links_with_status(
    connection: sqlite3.Connection | None,
    state_dir: Path,
    *,
    dashboard_contract: bool = True,
) -> tuple[list[dict], dict]:
    if connection is None:
        return [], {"status": "degraded"}
    try:
        rows = connection.execute(
            """
            SELECT asset_id, code, chart_type, as_of, path
            FROM chart_assets
            ORDER BY as_of DESC, asset_id ASC
            LIMIT ?
            """,
            (_MAX_CURRENT_CHART_LINKS + 1 if dashboard_contract else _MAX_CURRENT_CHART_LINKS,),
        ).fetchall()
    except sqlite3.Error:
        return [], {"status": "degraded"}
    if dashboard_contract and len(rows) > _MAX_CURRENT_CHART_LINKS:
        return [], {"status": "degraded"}
    charts = []
    for row in rows:
        code_pattern = _DASHBOARD_CODE_RE if dashboard_contract else _CODE_RE
        valid_chart_type = row["chart_type"] == "kline" if dashboard_contract else (
            isinstance(row["chart_type"], str) and _CHART_TYPE_RE.fullmatch(row["chart_type"])
        )
        if not (
            isinstance(row["asset_id"], str)
            and _ASSET_ID_RE.fullmatch(row["asset_id"])
            and isinstance(row["code"], str)
            and code_pattern.fullmatch(row["code"])
            and valid_chart_type
            and (
                _valid_dashboard_date(row["as_of"])
                if dashboard_contract
                else _valid_db_timestamp(row["as_of"])
            )
            and _safe_chart_path(row["path"], state_dir) is not None
        ):
            return [], {"status": "degraded"}
        charts.append(
            {
                "asset_id": row["asset_id"],
                "code": row["code"],
                "chart_type": row["chart_type"],
                "as_of": row["as_of"],
                "href": f"/api/charts/{row['asset_id']}",
            }
        )
    return charts, {"status": "ok"}


def _open_chart_descriptor(connection: sqlite3.Connection | None, state_dir: Path, asset_id: str) -> int | None:
    if connection is None:
        return None
    try:
        row = connection.execute("SELECT path FROM chart_assets WHERE asset_id = ?", (asset_id,)).fetchone()
    except sqlite3.Error:
        return None
    return _open_chart_asset_fd(row["path"], state_dir) if row is not None else None


def _open_chart_asset_fd(raw_path: object, state_dir: Path) -> int | None:
    if not isinstance(raw_path, str) or len(raw_path) > 1024:
        return None
    stored_path = Path(raw_path)
    candidates = [stored_path] if stored_path.is_absolute() else [state_dir / stored_path, advisor_paths.reports_dir() / stored_path]
    for candidate in candidates:
        for root in (state_dir / "charts", advisor_paths.reports_dir()):
            try:
                relative = candidate.relative_to(root)
                if not relative.parts or ".." in relative.parts or candidate.suffix.lower() != ".png":
                    continue
                descriptor = _open_contained_regular_fd(root, relative)
            except (OSError, ValueError):
                continue
            _chart_hook("fd_opened", descriptor=descriptor)
            return descriptor
    return None


def _open_contained_regular_fd(root: Path, relative: Path) -> int:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current_fd = os.open(root, directory_flags)
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        descriptor = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current_fd)
    finally:
        os.close(current_fd)
    try:
        entry_stat = os.fstat(descriptor)
        if not stat.S_ISREG(entry_stat.st_mode) or entry_stat.st_size > _MAX_CHART_BYTES:
            raise ValueError("invalid chart")
        if os.read(descriptor, 8) != b"\x89PNG\r\n\x1a\n":
            raise ValueError("invalid chart")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


async def _stream_descriptor(descriptor: int):
    try:
        while chunk := os.read(descriptor, 64 * 1024):
            yield chunk
    finally:
        os.close(descriptor)


def _safe_chart_path(raw_path: object, state_dir: Path) -> Path | None:
    if not isinstance(raw_path, str) or len(raw_path) > 1024:
        return None
    stored_path = Path(raw_path)
    candidates = [stored_path] if stored_path.is_absolute() else [state_dir / stored_path, advisor_paths.reports_dir() / stored_path]
    roots = (state_dir / "charts", advisor_paths.reports_dir())
    for candidate in candidates:
        for root in roots:
            if _regular_file_within(candidate, root):
                return candidate
    return None


def _regular_file_within(path: Path, root: Path) -> bool:
    try:
        root_stat = root.lstat()
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            return False
        path.relative_to(root)
        current = root
        for part in path.relative_to(root).parts:
            current = current / part
            if stat.S_ISLNK(current.lstat().st_mode):
                return False
        return path.suffix.lower() == ".png" and path.lstat().st_size <= _MAX_CHART_BYTES and stat.S_ISREG(path.lstat().st_mode)
    except (OSError, ValueError):
        return False


def _eligible_report_keys(
    connection: sqlite3.Connection | None,
    reports_root: Path,
    *,
    candidate_keys: set[tuple[str, str, str]] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> set[tuple[str, str, str]]:
    if connection is None or (candidate_keys is not None and not candidate_keys):
        return set()
    root = reports_root.resolve(strict=False)
    filters = []
    parameters: list[object] = []
    if candidate_keys is not None:
        candidate_filters = []
        for report_date, report_type in sorted(
            {(key[0], key[1]) for key in candidate_keys}
        ):
            candidate_filters.append(
                "(report_archive.report_date = ? AND report_archive.report_type = ?)"
            )
            parameters.extend((report_date, report_type))
        filters.append(f"({' OR '.join(candidate_filters)})")
    if start_date is not None:
        filters.append("report_archive.report_date >= ?")
        parameters.append(start_date)
    if end_date is not None:
        filters.append("report_archive.report_date <= ?")
        parameters.append(end_date)
    bounded_filter = f" AND {' AND '.join(filters)}" if filters else ""
    row_order = (
        "ORDER BY report_archive.report_date DESC, report_archive.created_at DESC"
        if candidate_keys is None
        else ""
    )
    try:
        rows = connection.execute(
            f"""
            SELECT report_archive.report_type, report_archive.report_date,
                   report_archive.markdown_path, report_archive.json_path
            FROM report_archive
            JOIN advisor_runs ON advisor_runs.run_id = report_archive.run_id
            WHERE ((report_archive.report_type IN ('premarket', 'review')
                    AND advisor_runs.status = 'passed')
               OR (report_archive.report_type = 'failure'
                   AND advisor_runs.status = 'blocked'))
              {bounded_filter}
            {row_order}
            """,
            parameters,
        )
    except sqlite3.Error:
        return set()

    keys: set[tuple[str, str, str]] = set()
    try:
        for row in rows:
            report_type = row["report_type"]
            report_date = row["report_date"]
            if report_type not in {"premarket", "review", "failure"}:
                continue
            try:
                if date.fromisoformat(report_date).isoformat() != report_date:
                    continue
            except (TypeError, ValueError):
                continue
            try:
                json_path = Path(row["json_path"])
                markdown_path = Path(row["markdown_path"])
            except TypeError:
                continue
            run_id = _report_run_id(report_type, json_path.name)
            if run_id is None:
                continue
            key = (report_date, report_type, run_id)
            if candidate_keys is not None and key not in candidate_keys:
                continue
            suffix = "" if run_id == "initial" else f".{run_id}"
            expected_directory = root / report_date
            if (
                json_path.resolve(strict=False)
                != expected_directory / f"{report_type}{suffix}.json"
                or markdown_path.resolve(strict=False)
                != expected_directory / f"{report_type}{suffix}.md"
            ):
                continue
            keys.add(key)
            if candidate_keys is not None and keys == candidate_keys:
                break
    except sqlite3.Error:
        return set()
    return keys


def _report_run_id(report_type: str, filename: str) -> str | None:
    if filename == f"{report_type}.json":
        return "initial"
    match = re.fullmatch(
        rf"{re.escape(report_type)}\.([A-Za-z0-9][A-Za-z0-9_-]{{0,63}})\.json",
        filename,
    )
    return match.group(1) if match is not None else None


def _report_key(report: dict) -> tuple[str, str, str]:
    return report["report_date"], report["report_type"], report["run_id"]


def _page_db_backed_archives(
    connection: sqlite3.Connection | None,
    reports_root: Path,
    *,
    limit: int,
    cursor: str | None,
    start_date: str | None,
    end_date: str | None,
    cursor_secret: bytes,
) -> dict:
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("invalid report page limit")
    today = _shanghai_today()
    start = date.fromisoformat(start_date) if start_date else today - timedelta(days=365)
    end = date.fromisoformat(end_date) if end_date else today
    if start > end or (end - start).days > 365:
        raise ValueError("invalid report date window")
    requested_start = start.isoformat()
    requested_end = end.isoformat()
    after, expected_snapshot = _decode_report_cursor(
        cursor, cursor_secret, requested_start, requested_end
    ) if cursor else (None, None)
    snapshot_digest = _report_archive_snapshot_digest(
        connection, requested_start, requested_end
    )
    if expected_snapshot is not None and not hmac.compare_digest(
        expected_snapshot, snapshot_digest
    ):
        raise StaleArchiveCursorError("stale report cursor")
    page_items, truncated, verified_count = _read_report_page_candidates(
        connection,
        reports_root,
        start_date=requested_start,
        end_date=requested_end,
        after=after,
        limit=limit,
    )
    next_cursor = (
        _encode_report_cursor(
            page_items[-1], cursor_secret, requested_start, requested_end, snapshot_digest
        )
        if truncated and page_items
        else None
    )
    return {
        "items": page_items,
        "next_cursor": next_cursor,
        "truncated": truncated,
        "requested_start_date": requested_start,
        "requested_end_date": requested_end,
        "verified_candidate_count": verified_count,
    }


def _report_archive_snapshot_digest(
    connection: sqlite3.Connection | None, start_date: str, end_date: str
) -> str:
    if connection is None:
        values = (0, None, None, 0)
    else:
        try:
            row = connection.execute(
                """
                SELECT COUNT(*), MAX(report_archive.rowid), MAX(report_archive.created_at),
                       COALESCE(SUM(LENGTH(report_archive.json_path) +
                                    LENGTH(report_archive.markdown_path)), 0)
                FROM report_archive
                JOIN advisor_runs ON advisor_runs.run_id = report_archive.run_id
                WHERE report_archive.report_type IN ('premarket', 'review')
                  AND advisor_runs.status = 'passed'
                  AND report_archive.report_date >= ?
                  AND report_archive.report_date <= ?
                """,
                (start_date, end_date),
            ).fetchone()
            values = tuple(row) if row is not None else (0, None, None, 0)
        except sqlite3.Error as error:
            raise ValueError("report archive query failed") from error
    return hashlib.sha256(
        json.dumps(values, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _read_report_page_candidates(
    connection: sqlite3.Connection | None,
    reports_root: Path,
    *,
    start_date: str,
    end_date: str,
    after: tuple[str, str, str] | None,
    limit: int,
) -> tuple[list[dict], bool, int]:
    if connection is None:
        return [], False, 0
    root = reports_root.resolve(strict=False)
    batch_size = min(100, max(20, limit * 2 + 1))
    scanned = 0
    verified = 0
    items: list[dict] = []
    last_key: tuple[str, str, str, int] | None = None
    after_path = None
    if after is not None:
        report_date, report_type, run_id = after
        suffix = "" if run_id == "initial" else f".{run_id}"
        after_path = str(root / report_date / f"{report_type}{suffix}.json")
    while scanned < _MAX_REPORT_ARCHIVE_ROWS and len(items) <= limit:
        filters = [
            "report_archive.report_date >= ?",
            "report_archive.report_date <= ?",
        ]
        parameters: list[object] = [start_date, end_date]
        if after is not None and after_path is not None:
            filters.append(
                "(report_archive.report_date, report_archive.report_type, "
                "report_archive.json_path) < (?, ?, ?)"
            )
            parameters.extend((after[0], after[1], after_path))
        if last_key is not None:
            filters.append(
                "(report_archive.report_date, report_archive.report_type, "
                "report_archive.json_path, report_archive.rowid) < (?, ?, ?, ?)"
            )
            parameters.extend(last_key)
        parameters.append(min(batch_size, _MAX_REPORT_ARCHIVE_ROWS - scanned))
        try:
            rows = connection.execute(
                f"""
                SELECT report_archive.report_type, report_archive.report_date,
                       report_archive.markdown_path, report_archive.json_path,
                       report_archive.rowid
                FROM report_archive
                JOIN advisor_runs ON advisor_runs.run_id = report_archive.run_id
                WHERE report_archive.report_type IN ('premarket', 'review')
                  AND advisor_runs.status = 'passed'
                  AND {' AND '.join(filters)}
                ORDER BY report_archive.report_date DESC,
                         report_archive.report_type DESC,
                         report_archive.json_path DESC,
                         report_archive.rowid DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        except sqlite3.Error as error:
            raise ValueError("report archive query failed") from error
        if not rows:
            break
        scanned += len(rows)
        tail = rows[-1]
        last_key = (tail["report_date"], tail["report_type"], tail["json_path"], tail["rowid"])
        for row in rows:
            item = _verified_report_row(root, reports_root, row)
            if item is None:
                continue
            verified += 1
            items.append(item)
            if len(items) > limit:
                break
        if len(rows) < batch_size:
            break
    return items[:limit], len(items) > limit or scanned >= _MAX_REPORT_ARCHIVE_ROWS, verified


def _verified_report_row(root: Path, reports_root: Path, row: sqlite3.Row) -> dict | None:
    report_type = row["report_type"]
    report_date = row["report_date"]
    try:
        if report_type not in {"premarket", "review"}:
            return None
        if date.fromisoformat(report_date).isoformat() != report_date:
            return None
        json_path = Path(row["json_path"])
        markdown_path = Path(row["markdown_path"])
    except (TypeError, ValueError):
        return None
    run_id = _report_run_id(report_type, json_path.name)
    if run_id is None:
        return None
    suffix = "" if run_id == "initial" else f".{run_id}"
    expected_directory = root / report_date
    if (
        json_path.resolve(strict=False) != expected_directory / f"{report_type}{suffix}.json"
        or markdown_path.resolve(strict=False) != expected_directory / f"{report_type}{suffix}.md"
    ):
        return None
    try:
        archive = read_verified_archive(reports_root, report_date, report_type, run_id)
    except (OSError, ValueError, RuntimeError):
        return None
    return {
        "report_date": report_date,
        "report_type": report_type,
        "run_id": run_id,
        "quality_status": archive["json"].get("quality_status", "unknown"),
    }


def _read_verified_report_items(
    reports_root: Path,
    eligible: set[tuple[str, str, str]],
) -> list[dict]:
    items = []
    for report_date, report_type, run_id in eligible:
        if report_type not in {"premarket", "review"}:
            continue
        try:
            archive = read_verified_archive(reports_root, report_date, report_type, run_id)
        except (OSError, ValueError, RuntimeError):
            continue
        items.append(
            {
                "report_date": report_date,
                "report_type": report_type,
                "run_id": run_id,
                "quality_status": archive["json"].get("quality_status", "unknown"),
            }
        )
    return sorted(items, key=_report_key, reverse=True)


def _encode_report_cursor(
    item: dict,
    secret: bytes,
    start_date: str,
    end_date: str,
    snapshot_digest: str,
) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "s": start_date,
            "e": end_date,
            "k": list(_report_key(item)),
            "g": snapshot_digest,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.digest(secret, payload, "sha256")
    return base64.urlsafe_b64encode(payload + signature).decode("ascii").rstrip("=")


def _decode_report_cursor(
    cursor: str,
    secret: bytes,
    start_date: str,
    end_date: str,
) -> tuple[tuple[str, str, str], str]:
    if not isinstance(cursor, str) or not cursor or len(cursor) > 512:
        raise ValueError("invalid report cursor")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload, signature = raw[:-32], raw[-32:]
        if not hmac.compare_digest(signature, hmac.digest(secret, payload, "sha256")):
            raise ValueError("invalid report cursor")
        values = json.loads(payload)
        report_date, report_type, run_id = values["k"]
        if (
            values.get("v") != 1
            or values.get("s") != start_date
            or values.get("e") != end_date
            or report_type not in {"premarket", "review"}
            or not isinstance(run_id, str)
            or _report_run_id(report_type, f"{report_type}.{run_id}.json") != run_id
            or date.fromisoformat(report_date).isoformat() != report_date
            or not isinstance(values.get("g"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", values["g"])
        ):
            raise ValueError("invalid report cursor")
        return (report_date, report_type, run_id), values["g"]
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid report cursor") from error


def _read_report_links(
    today: str,
    cursor_secret: bytes,
    eligible: set[tuple[str, str, str]],
) -> tuple[list[dict], dict]:
    del today, cursor_secret
    try:
        items = _read_verified_report_items(advisor_paths.reports_dir(), eligible)
    except (OSError, ValueError, RuntimeError):
        return [], {"status": "degraded", "truncated": True}
    links = [
        {
            **archive,
            "href": f"/api/reports/{archive['report_date']}/{archive['report_type']}?run_id={archive['run_id']}",
        }
        for archive in items[:_MAX_CURRENT_REPORT_LINKS]
    ]
    return links, {"status": "ok", "truncated": len(items) > _MAX_CURRENT_REPORT_LINKS}


def _read_today_report(
    today: str,
    report_type: str,
    eligible: set[tuple[str, str, str]],
) -> dict | None:
    run_ids = {
        run_id
        for report_date, candidate_type, run_id in eligible
        if report_date == today and candidate_type == report_type
    }
    if not run_ids:
        return None
    try:
        archives = {
            run_id: read_verified_archive(
                advisor_paths.reports_dir(), today, report_type, run_id
            )
            for run_id in run_ids
        }
    except (OSError, ValueError, RuntimeError):
        return None
    predecessors: set[str] = set()
    for run_id, archive in archives.items():
        supersession = archive["json"].get("supersession")
        if run_id == "initial":
            if supersession is not None:
                return None
            continue
        if (
            not isinstance(supersession, dict)
            or set(supersession) != {"reason", "supersedes"}
            or not isinstance(supersession["reason"], str)
            or supersession["supersedes"] not in archives
        ):
            return None
        predecessors.add(supersession["supersedes"])
    heads = set(archives) - predecessors
    if len(heads) != 1:
        return None
    head = heads.pop()
    seen: set[str] = set()
    current = head
    while current != "initial":
        if current in seen:
            return None
        seen.add(current)
        current = archives[current]["json"]["supersession"]["supersedes"]
    if set(archives) != seen | {"initial"}:
        return None
    return archives[head]


def _has_today_verified_report(today: str, report_type: str) -> bool:
    try:
        read_active_verified_archive(advisor_paths.reports_dir(), today, report_type)
    except (OSError, ValueError, RuntimeError):
        return False
    return True


def _report_status(report: dict | None) -> str:
    if report is None:
        return "missing"
    status = report["json"].get("quality_status")
    return status if status in {"passed", "blocked"} else "blocked"


def _chart_hook(_event: str, **_context) -> None:
    return None


def _finite_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    if isinstance(value, int) and not -_MAX_SQLITE_INTEGER <= value <= _MAX_SQLITE_INTEGER:
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _bounded_db_string(value: object, max_length: int) -> bool:
    if not isinstance(value, str) or not value or len(value) > max_length:
        return False
    try:
        return len(value.encode("utf-8")) <= max_length * 4
    except UnicodeError:
        return False


def _bounded_timestamp_string(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > _MAX_DB_TIMESTAMP_LENGTH:
        return False
    try:
        return len(value.encode("utf-8")) <= _MAX_DB_TIMESTAMP_LENGTH
    except UnicodeError:
        return False


def _valid_db_timestamp(value: object) -> bool:
    if not _bounded_db_string(value, _MAX_DB_TIMESTAMP_LENGTH):
        return False
    try:
        _parse_shanghai_datetime(value)
    except ValueError:
        return False
    return True


def _valid_dashboard_date(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _valid_account_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ACCOUNT_ID_RE.fullmatch(value))
