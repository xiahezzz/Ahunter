# Market Daily 运维说明

Market Daily 是项目内常驻的沪深 A 股日线服务。它在每晚 21:00（Asia/Shanghai）自行检查交易日、补齐行情并维护运行进度；不开放网络端口。股票清单、日 K、前复权因子、交易日观察和缺失 K 线证据都只来自新浪公开接口，不需要任何数据源密钥。

## 日常查看

在项目根目录执行：

```bash
./.venv-runtime/bin/advisor-services status
./.venv-runtime/bin/advisor-market-daily status
```

也可以在 WebUI 查看“Market Daily 行情服务”和“服务组”卡片。页面加载、刷新和状态读取都不会触发抓取、重试或修改数据；只有在 `空闲` 时由用户明确点击“启动五年同步”，才会提交一个本地冷启动请求。

状态含义：

- `已完整完成`：该 Run 的所有证券已完成。
- `部分完成`：成功数据已经保留；只有“来源未证明”的证券会在后续运行中重试。
- `数据冲突`：旧事实不会被覆盖，需要先核对来源并走明确修复流程。
- `等待冷启动`：还没有完整的五年基线，日常补洞不会开始。

## 首次冷启动

先执行不写入行情的预检；只有所有项目都通过，才能继续：

```bash
./.venv-runtime/bin/advisor-market-daily preflight
```

确认服务已安装并加载后，只提交一次请求。可以选择以下任一入口：

- 在 WebUI 的“Market Daily 行情服务”卡片处于 `空闲` 时，点击“启动五年同步”。按钮只写入本地队列，成功后显示“冷启动请求已排队”。
- 或在项目根目录执行：

```bash
./.venv-runtime/bin/advisor-market-daily cold-start
```

两个入口都只写入同一个幂等本地队列，不会立即抓取数据。常驻服务会在 21:00 后：

1. 用新浪沪深基准指数日线确认已完成交易日；
2. 从新浪沪市、深市 A 股节点冻结当前股票池和五年窗口，过滤北交所、CDR 与非普通 A 股；
3. 用同一个新浪适配器逐只获取未复权日 K 和前复权因子；新浪明确覆盖的交易日没有 K 线时只记录停牌缺席，不伪造零成交数据；
4. 在 WebUI、CLI 和 SQLite 中同步展示进度。

不要重复提交不同日期的冷启动，也不要手工把 Run 改成完成状态。

首次运行还需要逐只确定上市日期，约 5,200 只股票在每秒 2 次的限制下通常需要约 2～3 小时。后续每日运行复用已保存的上市日期，只增量补齐缺口和当天数据。

## 新浪访问与限流

- 所有新浪请求共享同一个进程级限流器，默认最多每秒 2 次，不会因并发任务绕过限制。
- 连接错误、HTTP 429 和 5xx 最多尝试 3 次；若响应提供 `Retry-After`，等待时间最多采用 30 秒。
- 重试耗尽后将证券标为 `source_missing`，Run 保持 `partial` 并在后续 21:00 运行补洞；没有第二数据源兜底。
- 新浪历史日 K 未提供成交额时，`amount` 保存为 `NULL`。不得填成 0，也不得从其他来源补字段。
- 新浪股票列表接口单页按 100 条分页，并先读取总数，防止静默截断。

## 完成后的只读验收

当 CLI 状态显示冷启动已完整完成后，执行以下命令。它不会联网、重试或写入数据库；会针对最新冷启动 Run 自动核对运行状态、冻结股票池、交易日覆盖、原始 K 线契约和复权因子覆盖。

```bash
./.venv-runtime/bin/advisor-market-daily validate
```

如需核验某次历史 Run，可指定其编号：

```bash
./.venv-runtime/bin/advisor-market-daily validate --run-id '<运行编号>'
```

只有命令返回“通过”后，才能将全市场冷启动基线作为完整状态使用；若返回“未通过”，保留现有事实并根据输出的项目排查，不得手工修改 Run 状态。

## 安装和启停

先准备 runtime 环境，再安装 LaunchAgent：

```bash
./.venv-runtime/bin/advisor-runtime-bootstrap
./.venv-runtime/bin/advisor-services install market-daily
./.venv-runtime/bin/advisor-services load market-daily
```

查看、停止和重新启动：

```bash
./.venv-runtime/bin/advisor-services status
./.venv-runtime/bin/advisor-services stop market-daily
./.venv-runtime/bin/advisor-services start market-daily
```

Market Daily 使用 KeepAlive。`stop` 会卸载该服务；`start` 会重新加载。MX Listener 仅显示只读状态，以上命令不会启动、停止或改动它。

## partial 排查与恢复

先查看失败证券：

```bash
./.venv-runtime/bin/advisor-market-daily status
sqlite3 -header -column data/advisor/advisor.sqlite \
  "SELECT code, status, attempts, selected_source, last_error
   FROM market_daily_run_items
   WHERE run_id = '<运行编号>' AND status IN ('source_missing', 'conflicted')
   ORDER BY code;"
```

- `source_missing`：等待下一个 21:00；服务只重试未证明的缺口，不会重复写入已完成日线。
- `conflicted`：保留原始日线，核对新浪原始响应与本地既有事实；不得用手工数据覆盖。
- 若服务进程中断，租约到期后新实例会恢复已认领请求或运行；已提交成功的逐证券事实不会丢失。

## 日志与健康检查

LaunchAgent 日志位于项目的 `logs/market-daily.out.log` 与 `logs/market-daily.err.log`。常用检查：

```bash
tail -n 200 logs/market-daily.out.log
tail -n 200 logs/market-daily.err.log
./.venv-runtime/bin/advisor-services status
```

21:00 前服务不会抓取；周末和休市日也会执行一次新浪交易日观察。若没有当日观察心跳，质量门会阻断研究，而不是猜测市场休市。所有外部请求都遵守同一个每秒 2 次限流器和最多 3 次的有界重试；不会切换到其他行情来源。

单只证券的长耗时请求会在各个外部请求边界续期服务租约和逐证券租约。其他实例无法在该租约有效时接管同一证券；若实例真正中断，租约到期后才会由新的实例恢复未完成项。

## 研究读取

Research Engine 仅从本地完成覆盖的 Market Daily 事实读取原始价格、复权研究价格和交易日证明。单只证券满足覆盖时可以研究，即使全市场 Run 仍是 `partial`；没有证实覆盖的数据不会被自动补造。
