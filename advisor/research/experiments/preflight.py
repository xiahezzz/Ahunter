"""Read-only readiness inspection. No backfill, model invocation or simulation."""
from datetime import datetime
import hashlib
from zoneinfo import ZoneInfo

from .contracts import ExperimentDraft, date_search_boundary
from .repository import digest
from .resolution import validation_errors
from advisor.research.lagent import settings_view


REQUIRED = {
    "clock": ("initial_research_at", "auction_as_of", "auction_submit_at", "postauction_as_of",
              "postauction_submit_at", "postmarket_as_of"),
    "account": ("policy_ref", "available_credit", "valuation_policy_ref"),
    "execution": ("market_rule_ref", "calendar_ref", "fill_model_ref", "fees_ref", "participation_rate",
                  "minute_slippage_ticks", "queue_slippage_ticks", "order_latency_ms", "cancel_latency_ms", "receipt_latency_ms"),
    "budget": ("task_cost_limit", "environment_reserve", "evaluation_reserve", "cost_currency", "price_table_ref"),
    "runtime": ("timeout_seconds", "retries", "subagent_concurrency", "cancel_wait_seconds"),
    "evaluate": ("repeats", "minimum_improvement", "stability_policy_ref"),
    "data": ("historical_time_policy_ref",),
}


def inspect(repository, experiment, catalog):
    errors = validation_errors(experiment["config"])
    if errors:
        return {"status": "blocked", "config_hash": experiment["config_hash"],
                "blocking_codes": ["required_configuration"],
                "checks": [{"code": "required_configuration", "status": "blocked",
                            "errors": [error.model_dump(mode="json") for error in errors]}],
                "model_calls": 0, "orders_created": 0, "return": None,
                "scope": "configuration_and_local_inventory_only", "implementation": "lagent-preflight@1"}
    spec = ExperimentDraft.model_validate(experiment["config"])
    checks = []

    def check(code, status, message, **evidence):
        checks.append({"code": code, "status": status, "message": message, "evidence": evidence})

    missing = [f"{group}.{field}" for group, fields in REQUIRED.items()
               for field in fields if getattr(getattr(spec, group), field) in (None, "unresolved")]
    if spec.data.search.enabled and spec.data.search.provider is None:
        missing.append("data.search.provider")
    check("required_configuration", "blocked" if missing else "passed",
          "必需运行参数尚未选择" if missing else "必需字段已填写；具体规则引用仍须由执行实现核验",
          missing_fields=missing)

    products = sorted(str(ref) for ref in catalog.products)
    unavailable = sorted(set(spec.data.product_refs or ()) - set(products))
    check("published_products", "blocked" if unavailable else "passed", "已发布产品引用检查",
          requested=spec.data.product_refs, unavailable=unavailable, published=products)
    check("historical_execution_data", "blocked",
          "尚未接入可用于正式成交评估的历史分钟、开盘竞价及必要排队证据适配器；即时快照不能替代",
          implemented_historical_execution_adapters=[],
          required=spec.execution.evidence)
    if spec.data.search.enabled:
        check("historical_web_search", "blocked", "历史 web 搜索适配器尚未接入；配置服务名称不等于能力可用",
              selected_provider=spec.data.search.provider, trust_policy=spec.data.search.trust_policy)
    else:
        check("historical_web_search", "not_requested", "本实验明确关闭 web 搜索")
    check("historical_time_semantics", "blocked",
          "自有数据尚未建立实验用历史可用时间映射；新浪日线 source_at 当前等于采集时间",
          policy_ref=spec.data.historical_time_policy_ref,
          daily_fields=["trade_date", "source_at", "fetched_at", "as_of_date"],
          mx_fields={"received_at": "required", "source_created_at": "nullable"})

    tasks = []
    db = repository.connection
    db.execute("BEGIN")
    try:
        for task in spec.tasks:
            if task.period.mode == "range":
                calendar = db.execute("SELECT trade_date, content_hash, fetched_at FROM trading_sessions "
                                      "WHERE trade_date >= ? AND trade_date <= ? ORDER BY trade_date",
                                      (task.period.start.isoformat(), task.period.end.isoformat())).fetchall()
            else:
                calendar = db.execute("SELECT trade_date, content_hash, fetched_at FROM trading_sessions "
                                      "WHERE trade_date >= ? ORDER BY trade_date LIMIT ?",
                                      (task.period.start.isoformat(), task.period.trading_days)).fetchall()
            dates = [row[0] for row in calendar]
            observed_hash = digest([list(row) for row in calendar])
            enough = bool(dates) and dates[0] == task.period.start.isoformat()
            if task.period.mode == "range":
                enough = enough and dates[-1] == task.period.end.isoformat()
            else:
                enough = enough and len(dates) == task.period.trading_days
            check("stored_session_observations", "observed" if enough else "blocked",
                  "仅列出数据库已存交易日，不证明日历完整性或历史公开时间",
                  task_id=task.task_id, observed_dates=dates, observations_hash=observed_hash,
                  requested=task.period.model_dump(mode="json"))
            daily = []
            for day in dates:
                row = db.execute(
                    "SELECT COUNT(*), MIN(source_at), MAX(source_at), MIN(fetched_at), MAX(fetched_at), "
                    "SUM(CASE WHEN quality_status != 'passed' THEN 1 ELSE 0 END) "
                    "FROM market_daily WHERE trade_date=?", (day,)).fetchone()
                hashes = hashlib.sha256()
                for item in db.execute("SELECT code, content_hash, source_at, fetched_at, quality_status FROM market_daily "
                                       "WHERE trade_date=? ORDER BY code", (day,)):
                    hashes.update((digest(list(item)) + "\n").encode())
                daily.append({"trade_date": day, "stored_rows": row[0], "source_at_min": row[1],
                              "source_at_max": row[2], "fetched_at_min": row[3], "fetched_at_max": row[4],
                              "nonpassed_rows": row[5] or 0, "stored_rows_hash": hashes.hexdigest()})
            check("stored_daily_inventory", "observed", "日线库存统计不是分钟覆盖或成交证据，也不证明全市场完整覆盖",
                  task_id=task.task_id, days=daily)
            phase_plan = []
            for day in dates:
                for phase, local_time in (("auction", spec.clock.auction_as_of),
                                          ("postauction", spec.clock.postauction_as_of),
                                          ("postmarket", spec.clock.postmarket_as_of)):
                    if local_time is not None:
                        cutoff = datetime.combine(datetime.fromisoformat(day).date(), local_time, ZoneInfo(spec.clock.timezone))
                        phase_plan.append({"phase": phase, "as_of": cutoff.isoformat(),
                                           "search_boundary_example": date_search_boundary(cutoff, source_timezone=spec.clock.timezone),
                                           "requires_provider_timezone_mapping": True})
            initial = None
            if spec.clock.initial_research_at is not None:
                initial = datetime.combine(task.research_start_date, spec.clock.initial_research_at, ZoneInfo(spec.clock.timezone))
                if phase_plan and initial >= datetime.fromisoformat(phase_plan[0]["as_of"]):
                    check("initial_phase_order", "blocked", "首次盘后研究必须早于首次交易研究阶段", task_id=task.task_id)
            tasks.append({"task_id": task.task_id, "role": task.role, "observed_trading_dates": dates,
                          "initial_as_of": initial.isoformat() if initial else None,
                          "provisional_phases": phase_plan, "calendar_verified": False})
        observed_settings = settings_view(repository)
        db.commit()
    except Exception:
        db.rollback()
        raise

    check("calendar_and_universe", "blocked", "尚未接入版本化实验日历与逐时点证券资格验证；库存日期不可冒充已验证日历",
          calendar_ref=spec.execution.calendar_ref, universe=spec.universe)
    check("episode_executor", "blocked", "已有内部 fixture 回放，但完整生产模型、数据与资源验收尚未接入；本入口不会启动执行")
    check("candidate_sealing", "blocked", "本次草案预检未绑定并验证正式执行候选；业务 LAgent 设置观察值不等于候选封存",
          observed_settings_hash=digest(observed_settings))
    check("cost_metering", "blocked", "内部预算账本已实现，实际模型/环境/评估计量及硬上界尚未完整验收；usage 不等于已核算总成本")
    return {"status": "blocked", "config_hash": experiment["config_hash"], "checks": checks,
            "blocking_codes": sorted({c["code"] for c in checks if c["status"] == "blocked"}),
            "task_resolution": tasks, "observed_lagent_settings": observed_settings,
            "observed_lagent_settings_hash": digest(observed_settings),
            "model_calls": 0, "orders_created": 0, "return": None,
            "scope": "configuration_and_local_inventory_only", "implementation": "lagent-preflight@1"}
