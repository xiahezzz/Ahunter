---
id: MD-016
status: in_progress
depends_on: [MD-015, MD-018]
adrs: [0031, 0033, 0040, 0046, 0048, 0049, 0051, 0057]
---

# 清空旧行情并完成真实冷启动

## 结果

在所有离线验收通过后，直接清空不合格的旧行情缓存，安装并启动常驻 Market Daily Service，提交真实五年沪深冷启动并验证全市场基线完成。

## 范围

- 执行 live preflight：数据库可写、磁盘空间、runtime 环境、新浪日 K、复权因子、沪深交易日、沪深股票池、日志目录和 Service Lease。
- 先输出精确清理预览；不制作备份，直接清空并重建 Market Daily 自有表；保留账本、报告、Research Artifacts、Profile、MX 事件和其他业务数据。
- 刷新 `.venv-runtime`，渲染、安装并加载 Market Daily KeepAlive LaunchAgent。
- 通过 `advisor-market-daily cold-start` 或 WebUI 的同一幂等入口提交唯一请求，记录 Request ID、Run ID、目标交易日、窗口和股票池哈希。
- 持续观察 Service 日志和 WebUI；partial 时按既定重试与补洞规则恢复，禁止手工伪造 complete。
- 完成后校验证券范围、日期窗口、行数、单位、可空成交额、停牌缺席、因子覆盖、交易日和 readiness。
- 验证单股 Research Product 可读取新基线，并确认 Service 保持常驻、下一次 21:00 时间正确。
- 在本 ticket 追加真实验收记录和关键计数，不写投资结论。

## 不包含

- 自动启动或改造 MX Listener。
- 修改 Chrome、RID 配置、账本、报告结论或 Agent Team。
- 在质量门未完成时将冷启动标记为成功。

## 验收条件

- [x] 清理操作仅命中 Market Daily 自有表，且不可恢复的删除结果被明确记录。
- [x] LaunchAgent 已加载，Market Daily Service 状态健康且只有一个实例持有租约。
- [ ] 冷启动 Run 为 `complete`，不存在 `source_missing` 或 `conflicted` 项。
- [ ] 股票池只含新浪当前沪深普通 A 股，含符合条件的 ST 与停牌证券，不含北交所、CDR 及其他证券类型。
- [ ] 所有 K 线位于五年闭区间和上市区间内，OHLC 合法，volume 为整数股；amount 有值时为真实非负元值，来源未提供时为 `NULL`。
- [ ] WebUI、CLI 和数据库报告相同的目标日期、总数、完成数和最新 session。
- [ ] 全量离线检查在 live 操作前后均通过，且 MX Listener 运行方式没有变化。

## 可能触点

- `data/advisor/advisor.sqlite`
- `~/Library/LaunchAgents/com.ahunter.market-daily.plist`
- `logs/market-daily.out.log`
- `logs/market-daily.err.log`
- `advisor/market_daily/live_validation.py`
- `tests/advisor/market_daily/test_live_validation.py`
- `docs/market-daily-operations.md`
- 本 ticket 的“验收记录”小节

## 验证

```bash
./.venv-runtime/bin/advisor-market-daily preflight
./.venv-runtime/bin/advisor-services status
./.venv-runtime/bin/advisor-market-daily status
./.venv-runtime/bin/advisor-market-daily validate
sqlite3 -header -column data/advisor/advisor.sqlite \
  "SELECT status, target_session, total_items, completed_items, failed_items FROM market_daily_runs ORDER BY created_at DESC LIMIT 1;"
./.venv311/bin/python -m pytest tests/advisor
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
git diff --check
```

## 验收记录

### 2026-08-07：新浪唯一源改造完成，等待重新冷启动

- 运行时已收敛为一个 `SinaDailyBarProvider`：股票清单、原始日 K、前复权因子、沪深基准交易日和停牌缺席均不再调用其他来源。
- 所有请求共享每秒 2 次的进程级限流器；连接错误、HTTP 429 和 5xx 最多尝试 3 次并尊重最长 30 秒的 `Retry-After`，没有备源回退。
- 新浪历史日 K 不提供成交额时保存 `NULL`，不再把缺失值伪造成 0；空表会在本次直接清理后按可空 Schema 重建。
- 真实新浪预检全项通过；沪市节点精确返回 2,310 只、深市节点精确返回 2,895 只，共 5,205 只且无北交所/CDR。五年样本返回 1,211 根日 K 和同区间因子，沪深基准最近交易日一致。
- 2026-08-08 00:23 在服务停止且无租约时直接清空 Market Daily 状态，没有制作备份：删除旧请求 1 条、交易日 1,211 条、交易日观察 102 条，共 1,314 条；其他命中项均为 0。重建后 `market_daily.amount` 已确认可为 `NULL`。
- 新冷启动请求为 `mdreq-c2c28608551dc2c7684c7cee`，创建于 `2026-08-08T00:23:41+08:00`，当前为 `pending`，尚未创建 Run。
- 项目配置已收敛为 `source: sina`，`.venv-runtime` 已卸载 `tdxpy`；导入实际 CLI 后没有加载任何 Eastmoney/TDX Provider 模块，`pip check` 通过。
- LaunchAgent 已重新加载，PID `36998`；旧进程的 120 秒租约自然到期后，新实例 `market-daily-60b497df31e8` 于 00:29:31 接管并持续续期。由于当前早于当日 21:00，日志正确显示 `waiting / before_21_00`，今晚 21:00 才开始真实新浪冷启动。

后续记录：Run ID、目标交易日、五年起点、Universe hash、证券数、K 线数、合法缺席数、因子覆盖、总耗时、最终服务状态和验证结果。
