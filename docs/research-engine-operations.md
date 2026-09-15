# Research Engine 运维手册

Research Engine 是仓库自包含的研究运行面：本地 Manifest Catalog 定义 Products、Research Agents、Teams、固定 Decision Pipeline 和 Codex Execution Policy；`market_daily` 日线产品只读取通过质量门的本地事实，其他允许的产品再按 Manifest 顺序使用仓库内公开来源适配器，最终只构建 sealed Snapshot；所有生成式任务由本机 Codex CLI 在独立 Run Capsule 中完成。

Market 与 Security 共用同一个 Request、Snapshot、Agent、审计和报告生命周期。Market Subject 固定表示沪深 A 股整体，永远没有虚拟股票代码；Security Subject 必须是一个合法六位 A 股代码。Research Engine 不调用券商、不创建订单，也不把 Team 之间的结论进行聚合、比较或排名。

## 环境准备

测试环境使用 `.venv311`，运行环境使用 `.venv-runtime`。首次创建运行环境：

```bash
/opt/local/bin/python3.11 -m advisor.runtime_env
```

离线检查使用固定 Node 24 路径：

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
```

## 手动提交研究请求

先做预检。预检检查 Catalog、数据库、Artifact Store、输出目录、公开来源、本机 Codex 可执行文件/会话和版本化执行策略：

```bash
.venv-runtime/bin/advisor-research preflight
```

CLI 只写入 durable Research Request，绝不在当前终端进程中调用 Provider、Codex、State Machine 或报告写入器。Security Team 必须显式提供一个六位代码；候选证券不会由模型选择：

```bash
.venv-runtime/bin/advisor-research run \
  --codes 600519 \
  --team a_share_core@1 \
  --submission-id local-security-001
```

Market Team 不接受 `--codes`：

```bash
.venv-runtime/bin/advisor-research run \
  --team a_share_market_overview@1 \
  --submission-id local-market-001
```

命令立即返回 Request ID 和 `queued` 状态。`--wait-seconds` 只轮询控制面；它不会让 CLI 执行研究。Security Request 的 Boundary 在后端接受时固定；Market Request 的 Boundary 由 Research Service 开始并封存本次 Whole-Market Snapshot 时固定。

## Research Service 与队列

只有一个持有 SQLite 租约的 Research Service 能认领并顺序执行队列。它在 Cycle 内仍按执行策略对独立 Agent 做有界并发，但不会同时执行两个 Cycle。

```bash
.venv-runtime/bin/advisor-research service status
.venv-runtime/bin/advisor-research service run --once
.venv-runtime/bin/advisor-services status
```

`service status` 与 WebUI 会真实显示 `offline`、`idle`、`running`、`stopping` 或 `degraded`，以及心跳、唯一 active Request 和 queued 数量。Service 离线时请求继续保持 `queued`，不会伪造执行或预计完成时间。手动 Request 按接受时间 FIFO，优先于尚未开始的 scheduled Request；已运行的 Cycle 不抢占。

LaunchAgent 的渲染、安装、加载、停止均是显式运维动作，例如 `advisor-services install research`。它们会改变本机 LaunchAgent 状态，因此只有在完成离线验收并获得明确授权后才能执行；不要把本手册中的命令当作自动上线步骤。

## Scope、取消、再次研究与报告

在本机 WebUI 的 Research 页面，选择任一精确已发布的 Team 版本后：Market Team 不显示代码输入，Security Team 要求一个六位代码。提交后可在“当前研究”查看全局队列、阶段、Agent 进度和有限错误原因。页面加载与轮询均为只读，不会自动提交请求。

- queued Request 的取消会立即进入 `cancelled`；running Request 会记录取消意图，并在安全边界停止。
- terminal Request 可点击“再次研究”。系统创建新的 Request ID、接受时间和 Boundary；旧 Snapshot、报告和 `rerun_of` 链保持不变。
- Report Explorer 默认按 publication time 读取 `passed` 与 `partial` Records，可按稳定 Team 或精确版本、状态分页筛选。Market `partial` 报告会逐项标出被阻断的 Insight 和有限原因；`blocked`、`failed`、`cancelled` 没有伪报告。
- 后端先校验报告 JSON/Markdown artifact hash，前端再安全渲染 Markdown；校验失败时只显示报告不可用。

成功运行的目录为：

```text
reports/YYYY-MM-DD/<cycle_id>/
├── cycle.json
├── index.md
├── complete.json
└── teams/<team>@<version>/
    ├── artifacts.json
    ├── conclusion.json
    └── report.md
```

Security blocked Team 只生成自身的 `status.json`，不生成部分结论。Market Team 在至少一个 typed Insight 可发布时生成 `partial` 报告，并逐项标明 blocked Insight；全部 Insight 都不可发布时同样只生成 `status.json`。发布会先在同级暂存目录完整写入，再以原子改名暴露最终目录；`complete.json` 出现后才能把该 Cycle 视为可读发布物。重启或同步重试只能复用文件集、内容和完成标记均与本次 Cycle 字节一致的既有发布物，不完整或不匹配目录会保留作审计证据且拒绝覆盖。JSON、Markdown 与 Artifact Store 中的内容均以 SHA-256 校验。

## 研究团队配置

在本地 WebUI 的“研究团队”区域完成以下操作：

1. 填写小写下划线 Team ID、中文名称，并选择至少一个当前 Agent。
2. 点击“发布 Team”。首次发布生成 `team_id@1`；同一 Team ID 的修订从“基于此版本新建”进入，系统自动创建下一个版本。
3. 发布后的版本保存在 `config/research/teams/`，其中固定发布时最新的 Agent 版本。历史版本只读、永久保留，没有覆盖、删除或归档操作。
4. 发布不会自动安排 08:30 工作。需要单独点击某个版本的“每日启用”；同一 Team 启用新版本会替换该 Team 的旧每日版本，不影响其他 Team。
5. 如需停止每日工作，点击当前启用版本的“取消每日启用”。最后一个 Team 取消后，页面会显示“当前未启用每日 Team”。

页面草稿只保存在当前页面内存；刷新、离开或取消修订都会丢弃未发布内容，不会修改 Manifest 或每日集合。Agent 目录和 Team 目录每次读取时都会从当前本地 Catalog 重载，因此新增 Agent 或 Agent 新版本无需修改页面注册表。

本地 API 与页面使用同一领域服务：`GET /api/research/agents` 查看最新 Agent，`GET/POST /api/research/teams` 查看或发布 Team，`PUT/DELETE /api/research/daily-teams/<team>@<version>` 单独启用或取消每日版本。它们不会启动 Provider、Codex、Research Cycle 或报告生成。

## Agent Instructions 修订

“研究配置”的 Research Agents 面板统一展示每个 Research Agent 的当前版本和全部历史版本。Instructions 按纯文本原样显示，不进行 Markdown 渲染；Decision Stage 和运行时由引擎附加的约束不在此处编辑。

点击当前版本的“修订指令”，或在历史版本上点击“恢复此版本指令”，都会从当前最新版本创建页面内草稿。历史恢复只复用所选历史文本，不会从旧版本分叉。草稿不会持久化；刷新或离开页面前会提示，确认离开后草稿丢弃。

发布前必须查看逐行差异、目标新版本和仍固定引用旧版本的 Team，并进行第二次确认。发布生成下一个不可变 `agent@版本`，只改变 `instructions`；标题、实现方式、数据访问、输出契约和预算均从当前最新版本保留。相同文本不会创建新版本，编辑期间最新版本变化会返回冲突。发布也不会自动修订 Team、替换每日启用集合、调用 Codex 或启动 Research Cycle。

页面通过 `GET /api/research/agent-access` 读取统一 Agent 目录，通过 `POST /api/research/agents/<agent>@<version>/instruction-revisions` 发布修订；写接口只接受 `instructions`。该控制面永久仅限本机回环地址与同源页面，不作为局域网或互联网管理接口。

## Agent Data Access 与 MX RID Feed

“研究配置”页面把 Data Product Catalog、统一的 Research Agents 面板和 Team 配置分区显示；Agent Instructions 与 Agent Data Access 在同一 Agent 卡片中分别修订。Data Product Catalog 是只读目录：它展示已发布的 Product、依赖、Provider 名称和历史版本，不展示连接地址、数据库路径或凭据，也不会连接 Provider。

为 Agent 配置数据访问时，先选择一个精确的现有 Agent 版本，再逐项选择 Product。`mx_events@2` 只能选择当前授权集合中的一个或多个明确 RID；没有“全部 RID”、模糊范围或从历史事件推断 RID 的入口。页面草稿只在内存中存在，刷新、离开或取消均不会改变 Catalog。

点击“发布 Agent 数据访问版本”后，系统基于所选基础版本生成下一个不可变 `agent@版本`：

- 仅 `data_access` 和当时的 RID 配置版本参与该发布；Agent 的标题、指令、实现方式、查询预算和其他运行契约保持不变。
- RID 配置在编辑期间发生变化时，发布会冲突而不是猜测或覆盖；刷新后人工比较并重新选择。
- 发布不会启动 Provider、Codex、Research Cycle 或报告生成，也不会修改 Team 或每日启用集合。
- 旧 Agent 版本和引用它的历史 Team 永远保持原样。需要使用新 Agent 时，必须在“研究团队”中显式发布新的 Team 版本；每日启用仍是另一项独立操作。

Research Snapshot 只为该次所选 Team 中声明的 MX RID Feed union 物化一次，但每个 Agent Capsule 只能打开自己精确声明的 Feed artifact。撤销某个 RID 后，只会阻断仍固定依赖该 RID 的 Agent 和 Team；不依赖它的 Team 继续运行。恢复方式是用户重新授权 RID，或发布移除该 RID 的新 Agent 版本并显式发布新的 Team 版本，绝不重写历史版本。

本地 API 只接受固定形状的本地 JSON：`GET /api/research/data-catalog`、`GET /api/research/agent-access` 读取目录与版本；`POST /api/research/agents/<agent>@<version>/access-revisions` 只接收 `data_access` 与 `rid_version`。这些接口不接受 Provider、执行器、网络地址或任意脚本参数。

## 报告语言

新 Cycle 的 Agent 和 Decision Stage prompt 统一要求使用简体中文、短句和日常表达；专业术语第一次出现时必须用中文解释。`index.md`、Team `report.md`、Daily Team Brief 和晚间复盘 Markdown 的固定文案也只使用中文。

发布质量门会拒绝纯英文摘要、观点、证据摘要、风险、失效条件、时间范围或仓位说明，拒绝后不会生成 `complete.json`。JSON 的键名、固定枚举值、Agent/Team/Stage 引用和证据编号继续保持稳定的机器契约；其中面向读者的正文值必须是中文。已经发布的历史 Cycle 不会被改写，需要用新的 Cycle ID 运行才能生成新版中文报告。

## 每日批次

08:30 调度只提交 durable Requests。Security Team 可按显式代码或确定性候选集合逐 Subject 提交；Market Team 每个 occurrence 提交一个无代码的 Market Request：

```bash
.venv-runtime/bin/advisor-research batch \
  --date 2026-08-06 \
  --events-db data/state/events.sqlite \
  --allowed-rids config/allowed-rids.yaml \
  --output-dir reports
```

同一天的不同 occurrence 不会被 Team、Subject 或日期去重；只有同一个 submission identity 的传输重试是幂等的。每个已认领 Subject 最终都有独立 Cycle 和 Snapshot。Daily Team Brief 只列出所选 Team 的报告入口或 blocked 状态，不重新调用 Codex，也不产生跨 Team 结论。

每日 Team 集合只存于 `config/advisor.yaml` 的 `research.default_teams`。该列表可以为空：08:30 调度会成功返回明确的 `skipped`，不创建 Batch、Snapshot、Provider 请求或 Codex Invocation。发布新 Team 或新版本不会自动改变该列表。

## 22:30 Team 复盘

复盘只读取已发布的 Team Conclusion，并使用截止复盘时间可验证的收盘行情；它不调用 Codex、不创建新的 Decision Stance，也不把复盘写回未来 Capsule：

```bash
.venv-runtime/bin/advisor-scheduled-review \
  --date 2026-08-06 \
  --output-dir reports
```

输出位于 `reports/YYYY-MM-DD/reviews/teams/<team>@<version>/<code>.{json,md}`。缺少可靠收盘、晨间结论或精确 Team/Subject 关联时，只写 blocked review。

## blocked、partial 与恢复排查

1. 先在 WebUI 的当前队列或 `advisor-research service status` 确认 Service 是否在线，再查看 Request 的 phase、有限 reason code 和取消状态。
2. 再查看 Report Explorer 或 `reports/YYYY-MM-DD/<cycle_id>/cycle.json`。Market `partial` 会列出每个 blocked Insight；没有任何可发布 Insight 时对应 Team 只有 `status.json`。
3. 用 SQLite 只读查询核对 `research_requests`、`research_records`、`research_service_leases`、`research_scope_snapshots`、`research_scope_invocations`、`research_scope_invocation_attempts`，以及既有 Security 的 `research_cycles`、`research_invocations`、`research_invocation_attempts` 和 `research_stage_runs`。每一次技术 Attempt 都必须保留，不覆盖已接受结果。
4. 重启后的 Service 会从 sealed Snapshot、已通过 Finding 和已持久化阶段恢复；不会重新抓取同一 Market Snapshot，也不会尝试恢复失联 Codex 进程。修复来源、配置或本机 Codex 后请用 rerun 创建新的 Request，不要覆盖旧报告，也不要补写被质量门阻断的结论。

## 当前离线验收与上线边界

- Scope Research 的自动化验收使用临时 Catalog、SQLite、Artifact Store、固定 Provider fixture、fake clock 和 fake Codex；覆盖 Security 既有路径，以及 Market 的 API 提交、Service、sealed Snapshot、三个 typed Insights、partial、报告详情和进程重启恢复。
- 联网 Provider、真实 Codex session、LaunchAgent 安装、Market Daily reset、真实 Market Request 和真实报告结论不属于离线验收，也不会由测试或本文档自动触发。
- RE-038 的实机上线仍需要用户在离线全绿后明确授权。届时应只记录有限状态、版本和 Request ID，不在运维文档写入 Provider response、prompt、日志、凭据或报告正文。

## 查询语义与失败诊断

- Market 输出校验区分结构化证据来源与研究正文，避免把上市数据提供方名称和其他段落中的市场涨跌拼接成个股建议。来源字段本身及证据摘录中的真实个股建议仍受校验，并在进入 Team 汇总前阻断对应 Insight。

- 查询规划说明列出聚合 metric 的合法值（count、sum、mean、median、min、max），校验失败后的重试收到有界错误及 query_id。
- breadth 只按 change_pct 的正负统计涨跌。历史产品没有该字段时不得用 close 代替；模型应依据已声明产品选择可支持的方法，数据不足则明确阻断。
- 数值聚合遇到缺失或非有限值返回 null，并披露 numeric_count / missing_count；不把缺失成交额变成零，也不把部分数值之和当成完整总量。新的全市场历史摘要通过 field_coverage 提前披露成交额覆盖。
- window_compare 披露两个窗口实际观测的交易日数量及起止日。总量比较遇到交易日数量不一致时不生成 delta；实际观测日不等于已证明交易日历完整，仍须核对 sessions 与数据来源。
- 同一已验证查询计划中的多个有界历史查询先从原封存数据流生成一个日期范围临时视图，再逐项执行原查询、访问校验和审计。临时视图在成功、失败或取消后删除；原始 Artifact 不改写。
- Agent 主动返回 quality.status=blocked 时保留 limitations 并结束该 Agent，不再把数据不足作为格式错误重试。技术超时仍遵守原执行策略。
- Research Agent 查询不再设置查询次数、累计结果行数、字节数、证券数量、排名条数或交易日窗口预算。API 预算字段为 `null`，页面显示“无限制”。历史发布的 YAML 和 Artifact 保留原样，其中旧预算数值仅为历史元数据，不约束新运行；新的 Capsule 记录无上限语义。查询仍只读取当前 Agent 声明的封存 Product，完整记录查询次数、返回行数/字节数和结果哈希。权限及数据质量错误继续阻断；规划和决策等仅控制阶段的 Capsule 仍禁止执行数据查询。
- 每次失败尝试的阶段、错误类和有界诊断保存为 application/vnd.a-hunter.attempt-diagnostics+json Artifact，以 invocation_key 关联，包括最终成功之前的失败尝试。报告仍只显示有限原因类别，不显示内部诊断正文。
- 修复适用于新执行。历史 Snapshot 和报告保持原样；缺失成交额或历史日期需由真实来源补齐，不能通过放宽质量门恢复结论。

## WebUI 研究模型设置

“研究配置”顶部的“研究模型”面板可通过两个下拉框修改统一默认模型和推理强度，点击“保存模型配置”同时保存。模型及其支持的强度读取本机 Codex 的 models_cache.json（遵守 CODEX_HOME），过滤隐藏模型；缓存不可读取时保留当前配置。切换模型时保留兼容的强度，否则选用该模型的默认强度；后端再次校验组合。超时、并发、重试和连接设置保持不变；保存不会提交研究或调用模型。

每次实际变更新建不可变 Execution Policy 版本，并原子切换默认引用。保存时校验页面读到的旧版本；其他页面已修改时返回冲突，避免覆盖。写入与每日团队配置共用配置锁。

Research Service 在新任务开始执行时读取默认模型和推理强度并将完整策略固定到 research_request_policies。后续保存、重试和进程恢复不会改变已开始任务的模型和推理强度。排队但尚未开始的任务采用执行开始时的默认设置。模型是否可用仍由启动时的现有 Codex preflight 验证。

接口为 GET /api/research/execution-settings 和 PUT /api/research/execution-settings。写入只接受 expected_policy_ref、model 与 reasoning_effort，并遵守本机同源限制；读取不暴露可执行文件路径、代理或环境配置。

Market 输出校验在识别证券代码前排除完整时间戳，避免把六位微秒当成证券代码；同一输出中其他位置的真实证券代码、名称及建议仍按原规则检查。
# LAgent 模式

研究配置页面提供 LAgent 自由研究入口：一个主 agent 自主读取数据产品、创建临时子研究任务并汇总报告。
展开「配置 LAgent」可修改全部行为参数；展开「运行观测与历史」查看固定配置、委托关系、调用、耗时、token 用量和失败原因。
数量限制留空表示不限。观测及质量门禁始终启用。主 agent 按需依次执行委托。
CLI 操作见 `skills/ahunter/references/workflows.md` 的 LAgent 流程。
