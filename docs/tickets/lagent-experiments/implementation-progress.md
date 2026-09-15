# LAgent 实验实现进度

更新：2026-09-09。范围保持 LE-001～015 首版；LE-016 在首版真实验收后实施。
工作区中的业务 Research、Market Daily、MX 和其他既有改动原样保留。
当前：LE-001/002 已完成内部离线验收；LE-003 历史包与规则、LE-004 时间查询/搜索、LE-005 阶段时钟、LE-006 账户/估值、LE-007 执行/撤换、LE-008 候选工具网关与模型尝试控制均在实现与集成中。LE-009 费用账本、标定、核对与汇总实现中，LE-010～015 尚待实施，最新验证与剩余边界见本文末尾。

## LE-001：已完成契约与离线验收

代码：`advisor/research/experiments/contracts.py`、`resolution.py`、`candidates.py`；
预设：`config/research/experiments/original-case.v1.json`；
验证：`tests/advisor/research/test_experiment_contracts.py`。

- 完整声明 Task/Period、账户、时钟、数据/搜索、执行、预算、runtime、评估和 trace 参数；
  数值 schema 导出单位、范围、默认来源和 null 语义，默认值来自版本化预设。
- 原例及预定选择/留出任务通过注入的完整版本化日历解析；测试日历仅是 fixture，
  没有将工作日生成器或本地日 K 库存用作真实日历证明。
- `resolve` 返回结构化错误或 `ResolvedSpecification`，保留原始输入、全部生效值、
  逐字段来源和日期列表；规格哈希包含数值规范化后的配置、字段 schema 与日历哈希。
  `verify_specification` 可检查导出被修改的情况。
- 明确 null 不限量、缺失字段、`unresolved` 资源测量和 `calibration_derived` 预算公式；
  未知资金制度和现金账户信用额度返回 unsupported。真实预算金额仍待 LE-009。
- 候选源码、提示词、依赖锁、I/O 契约复制到现有 ArtifactStore，输入校验不执行代码。
  包身份与提案身份分离；allowlist 仅含查询/委托/记忆研究策略，不能修改平台条件。
- 起步 ExperimentStore 可保留不完整/错误草案，预检返回 blocked；未增加运行或注册入口。

验收对照：

| LE-001 条件 | 证据 |
| --- | --- |
| 哈希稳定、条件/单位/规则变化可解释 | 数字规范化、时区/金额/并发/阈值/单位/日历版本测试 |
| 缺失、负天数、日期冲突、null、时序拒绝，草案保留 | 参数化错误测试、空草案/不完整草案存储及 blocked 预检 |
| 可修改期间、金额、时钟、并发、门槛且全部导出 | 任意 7 日及 range 解析、字段来源覆盖、配置差异测试 |
| 封存不受宿主编辑影响，重复提案独立 | 产物读取/损坏/路径与符号链接测试、相同包不同提案身份 |

2026-09-09 定向验证：47 passed。测试命令使用仓库要求的 clean non-login
shell，清除大小写 HTTP/HTTPS/ALL proxy，并设置 NO_PROXY/no_proxy=*。
命令：`.venv311/bin/python -m pytest tests/advisor/research/test_experiment_contracts.py -q`。

## LE-002 持久化基础阶段记录（登记与查询已在后续验收补齐）

代码：`advisor/research/experiments/records.py`、起步 `repository.py`、
共享 ResearchRepository/ArtifactStore 及 `advisor/db/schema.sql`。
没有改变原 Research queue 或创建独立调度服务。

- 通用永久事实类型覆盖定义、候选包/提案、样本计划/测试、阶段/attempt、调用、
  成本/账本、评估/比较、选择、exposure 与校正；事实和关系不可 UPDATE/DELETE。
- 事件键固定 test/phase/action/attempt，保存现实/模拟时间和 fencing generation。
  事件、状态投影及 outbox 同事务；同身份不同内容 conflict，已提交动作重试只读原结果。
- worker lease 到期后 generation 递增，恢复追加 worker attempt、仍链接原 Test；
  过期 worker 无法写入或续约。测试终态不可反转，重跑/重评通过新事实链接历史。
- 状态投影使用事件 revision 作乐观并发检查；缺失/过期投影 fail closed，
  可从不可变事件重建。outbox 是至少一次投递，接收方须按 outbox_id 幂等，
  不声称外部模型调用 exactly-once。
- ArtifactStore 内容写入先 fsync 文件，再 fsync 目录，再提交 DB 引用；
  孤儿文件不被算作已发布记录，失败重试会重新同步目录，损坏文件不建引用。
- cursor 固定查询条件和分页最高序号，新追加记录留到下一次枚举，不吞掉失败历史。

66 项新增存储测试；与 LE-001 和 ArtifactStore 定向组合验证 117 passed。
覆盖七类副作用 × 六个提交故障点、并发、fence、终态、迁移、分页、产物损坏与目录 fsync。
最后一轮新增 fsync 重试断言已通过完整 self-test。

在此基础阶段，类型化登记、原文保留策略与宿主查询隔离尚未完成；
下文“LE-002 登记、查询隔离与保留策略验收”已补齐这些工作。

## 后续顺序与未通过的运行门槛

LE-002 内部登记与查询已完成；LE-003 的最新实现与缺口见本文末尾。
LE-003 接入真实历史包与法定费率/规则证据；之后按索引依赖继续 env、budget、runtime、
Episode、比较、CLI/API 与进化树。LE-014/015 要求全流程与真实能力验收。

当前没有历史数据包、真实模型费用上界、成本标定或正式实验收益证据；
配置预设里的引用不是已存在的数据包或价格表声明。没有启动模型、模拟 Episode 或真实交易。

## CLI / skill 影响

当前仅提供内部 Python 契约，无新增 API 或可操作实验 CLI，catalog 不变；
已同步 ahunter skill/workflows 的开发状态、预算占位语义和运行门槛。
CLI maintenance：PASS，51 API operations covered，generated command reference current。
完整 self-test：152 项 Node＋989 项 Python 全部通过（1 条既有 Starlette
弃用提示）。命令使用固定 Node 24 路径及 clean-shell wrapper。
API 由原 LaunchAgent 重启，PID 16823 → 42262；`/api/services` 及
Research 的 data-catalog / agent-access / agents / teams 均 HTTP 200。
`.venv-runtime` 可加载并验证新预设。`git diff --check` 通过。
没有运行第二个 API、模型调用或真实数据研究。


## 最新回归与运行时核对（LE-002 基础变更后）

完整 self-test：152 Node＋1055 Python 全部通过；113 项是新增实验契约/存储测试。
CLI maintenance：PASS，51 operations covered；git diff --check 通过，并单独检查
尚未跟踪的新实验文件无行尾空白。现有 API LaunchAgent 再次重启（PID 42262 → 45674），
`/api/services`、`/api/research/data-catalog`、`/api/research/agent-access`、
`/api/research/agents`、`/api/research/teams` 全部 HTTP 200；runtime 可导入 ExperimentRecords。

CLI catalog 不变的原因仍是未新增实验 API/命令；skill/workflows 已同步内部持久化
能力与其尚未完成的运行集成边界。这是 LE-002 基础阶段的回归记录；最新验收见下文。


## LE-002 登记、查询隔离与保留策略验收（2026-09-09）

本轮新增 `registration.py`、`queries.py`、`sources.py`，并把单条 records.put
扩展为共享事务中的 put_many，复用既有 ResearchRepository/ArtifactStore。

- 定义登记校验 sealed specification；候选包与单父提案原子登记，复核父包可读。
  相同内容共用包但保留不同提案，不创建虚假的测试运行。
- TestPlan 校验规格、任务角色、完整 candidate×task×repeat 矩阵与交错顺序，
  计划和全部 TestRecord 一起提交；任一插入中断整组回滚。预检/排队由后续 Episode 入口负责。
  最终留出须经 LE-011 选择冻结；普通登记不放行最终留出执行。
- rerun 保留原样本与终态，新记录明确不替换原 Comparison 样本。
- 宿主 owner/optimizer 查询视图隔离隐藏详情、trace、下载和导出。owner 必须提供
  审计身份，exposure 提交成功后才返回隐藏内容。未归属任务的旧事实默认按未知范围拒绝 optimizer。
- 选择反馈只接受完成比较的数值/状态白名单，拒绝最终留出、自由文本、trace 和未完成比较。
  owner 外部已知 exposure 按日期区间保留，不因任务改名消失。
- 外部原文按 source policy 过期，元数据、内容哈希和 unavailable 状态继续保留；
  先保存过期事实再删专属原文，可从 unlink 前后中断恢复。仍有效的共享引用或内部永久引用阻止误删。
  已过期原文的登记重试只确认原记录，不恢复或延长保留期。
- 导出标明 exact_bytes / metadata_and_hash_only / integrity_failure，原记录不改分、不清理失败历史。
  引用在数据库锁内复核字节，防止 prepare 与提交之间刚好发生合法过期而提交悬空引用。

LE-002 验收证据：

| 验收要求 | 对应验证 |
| --- | --- |
| 各资金/成本副作用事务故障、同键冲突 | `test_experiment_records.py` 七类动作×六个故障点、并发 revision/fence |
| 孤儿/损坏文件不发布、旧库保留 | records 的迁移/产物/fsync 测试；registration 的整组登记中断与过期引用竞争 |
| 全部终态和修订保留 | records 的 completed/blocked/failed/cancelled 保护；typed rerun 与 linked evaluation |
| 所有节点和样本完整分页 | records 高水位 cursor；queries 多页调优/隐藏 TestRecord overview，不按收益筛选 |
| 隐藏详情宿主隔离与 exposure | owner 审计失败不返回产物；optimizer 详情/事件/导出/下载拒绝；外部日期暴露记录 |
| 来源原文保留政策 | 过期前后可回放状态、共享引用保护、symlink 防误删、unlink 故障重试 |

25 项新增登记/查询/保留策略测试；先前定向组合 141 passed，最后的过期登记幂等
测试已在完整 self-test 中通过。CLI catalog 没有新增 API 可覆盖，skill/workflows 已同步以上
内部能力及尚未开放的运行入口。它们不提供真实运行 readiness 或实验收益。

后续：LE-003 开始真实历史包、交易规则/费率与证券×日期×场景覆盖预检。
本轮按文件名检查 `data/`、`config/research/`、`reports/lagent-tests/` 的 manifest 清单，
没有发现 manifest JSON/YAML；这是所检查目录的注册输入库存，不代表搜索了整个本机，
也不把普通日线文件或无 manifest 的散落数据认定为合格历史包。


LE-002 最终回归：152 Node＋1080 Python 全部通过（含本轮 25 项新增测试）。
CLI maintenance PASS：51 operations covered；git diff --check 与新文件空白检查通过。
已重启原 API LaunchAgent，PID 45674 → 50839；services、data-catalog、agent-access、
agents、teams 接口均 HTTP 200，runtime 可加载新增三个内部模块。
LE-002 标为 done；正式 Episode、最终留出冻结和端到端审查仍由后续 tickets 完成。


## LE-003 导入与覆盖离线实现（2026-09-09，仍 in_progress）

新增 `advisor/research/experiments/data/{contracts,bundles,coverage}.py`，
共享原 ExperimentRecords/ArtifactStore，并增加不可变 `lagent_bundle_rows` 索引。
没有新建服务或改变 Market Daily 的新浪唯一实时来源。

- manifest 绑定历史股票池、完整日历、原始日线/分钟、竞价、逐笔/队列快照、状态、
  企业行动和规则/费用数据，来源证明文件与 JSONL 一起按哈希封存。
- 导入同包幂等、身份变化冲突，重复字段/自然键、时区、复权污染及文件/行哈希不符
  拒绝发布；整包索引事务中断回滚，输入失败保留不可变诊断记录。
- 分开记录事件、初次公开、版本公开和采集时间。历史语义无证明时采用采集时间；
  晚修订不能倒填。行索引保留来源文件哈希，读取复核派生可用时间。
- 按原任务全证券×交易日生成覆盖报告，保留缺失清单与请求期间；日线不能替代
  分钟、竞价或完整队列。队列验证初始订单、序号、双方成交数量/价格与完整区间。
- `HistoricalBundles.preflight` 把覆盖 JSON 封存为永久产物并关联 bundle；同身份
  不允许用新报告覆盖旧报告。fixture coverage passed 仍是正式 blocked。
- 状态/企业行动、按生效日期绑定的规则/费用具备类型契约；尚非完整官方规则实现。
  独立来源审查放行、临时停牌/复牌区间及完整跨数据集核对仍待补齐。

[实际能力报告](../../research/lagent-data-capability-status.md)记录所检查目录内没有
manifest 输入、现有 Sina/MX 的时间语义，以及官方参考下载的已取得/未取得边界。
深交所规则 PDF 已下载并记录哈希，但未完成全条款结构化审查；不宣称费用包或真实包可用。
`origin=real` 仍必须通过尚待实现的独立审查，当前正式 readiness 恒为 false。

验证：26 项新增导入/覆盖测试通过；完整 self-test 整体 PASS，1106 Python passed，
有 1 条既有 Starlette 弃用提示。CLI maintenance PASS，51 API operations covered。
`git diff --check` 和新增文件行尾空白检查通过。现有 API LaunchAgent 已重启，
PID 50839 → 57822；services、data-catalog、agent-access、agents、teams 均 HTTP 200，
`.venv-runtime` 可加载新导入/覆盖模块。

CLI/skill 影响：内部导入/预检未公开 API，因此 catalog 无变化；skill/workflows 已同步
该能力及仍未通过的真实门槛。未调用模型、创建模拟 Episode 或产生收益记录。

后续可以推进 LE-004 的时间受控访问/搜索与 LE-005 时钟快照离线实现，同时保留 LE-003
真实包与规则审查缺口；不能因输入不可得缩小原例或把该 ticket 标为 done。


## LE-004 固定时间查询与搜索（2026-09-09，仍 in_progress）

新增 `data/temporal.py`、`data/access.py`、`search.py` 与
`test_experiment_time_access.py`。详细接口与未完成边界见
[时间访问契约](../../research/lagent-time-access-contracts.md)。

- 事件界限与快照时刻分别验证，新闻晚修订、MX 晚接收、日期型材料、未知历史版本、
  衍生依赖与回执延迟具有不同映射。09:25:05 快照不会释放 09:25 后新新闻。
- 宿主固定 Test/phase/generation/产品范围，主子会话继承同一界限。只返回可见行，
  不返回未来行数量、目录细节或原始 provider 错误；必需/可选产品失败分开。
- 查询审计关联本 Test，提交及释放前复核权限；阶段已关闭或 callback 迟到被 fence。
  当前权限校验由宿主回调提供，尚待 LE-005 持久时钟生命周期接入。
- Tavily 候选接口只接受文本；连接、日期、配置与调用授权由宿主注入。HTTP 连接
  限固定 endpoint、拒绝重定向、净化错误，不持久化服务授权值。真实连接尚未取得/使用。
- 服务日期映射显式版本化；fixture 固定 inclusive/exclusive，不明时区/包含性收紧。
  08-02 初始研究允许日止于 08-01；明确未来结果拒绝，无日期结果标 date_filter_trusted。
- 响应按截点、全部设置、provider/连接/版本、信任/保留政策和采集世代缓存。已有响应
  不刷新；重复进行中/外部未知结果返回 unresolved，重放缺失或正文过期不重新搜索。
- 搜索原文按保留政策存储，永久查询审计只存状态/哈希，不产生妨碍过期的永久正文副本。
  不暴露其他候选查询清单；跨现实时间的新查询仍有索引变化限制。

26 项新增测试通过；与 LE-003 组合 52 passed。完整 self-test：152 Node＋1132 Python
全部通过（1 条既有 Starlette 弃用提示）；CLI maintenance PASS，51 operations covered。
`git diff --check` 与新增文件空白检查通过。没有新增 API/CLI，因此 catalog 不变；
ahunter skill/workflows 已同步内部能力、受控连接缺失及仍待验证的运行时隔离范围。

LE-004 不标为 done：真实连接/日期过滤、持久阶段、RPC 与 OS/shell/网络隔离及预算
授权尚需 LE-005/008/009/015 验证。不存在可正式运行的 Episode。下一项推进 LE-005。

本轮 API 运行时核对：原 LaunchAgent PID 57822 → 62161，state=running；services、
data-catalog、agent-access、agents、teams 均 HTTP 200，runtime 可导入 HistoricalQueries
与 HistoricalSearch。未创建第二个 API，也未调用真实 Tavily 或模型。


## LE-005 持久阶段、快照与接收时间（2026-09-09，仍 in_progress）

新增 `clock.py`、`timing.py`、`test_experiment_clock.py`；事实类型增加 snapshot，
复用 ExperimentRecords 的事件、投影、事务和 worker lease，没有新增调度服务。
详见[阶段时钟契约](../../research/lagent-phase-clock-contracts.md)。

- 阶段序列取 sealed Test 定义与完整交易日历；原例 16 阶段，七日/显式休市 fixture
  保留请求期间且无额外休市模型阶段。初始/竞价前/竞价后/盘后两个界限与计划接收时刻
  都由配置解析，模型现实跨午夜不扩大观察窗口。
- pending/active/closing/closed 及当前下标、快照、激活时间、固定 deadline 和 worker
  generation 持久化在事件/投影中。closing 即撤销主子权限；query 提交事务内检查。
- snapshot 准备与 active 授权分开；先封存可见内容并复核 lease，再提交激活事件。
  必需材料晚到、未来或缺失保留 blocked，不能推迟快照。外部保留政策正文不转存为永久
  快照；内部可见 JSON 产物在 observe 时复核并返回。
- worker 恢复不重置 snapshot/deadline，旧 worker 与旧 scope 被 fence。投影可重建，
  崩溃/同动作重试不会增加第二个激活或覆盖已冻结快照。期限已过不能通过恢复续时。
- 当前 owner 对迟到回调仅保留 output hash 的 discard 审计，不注入下一阶段计划/记忆。
  取消/平台失败停止推进；预算停止后的阶段继续按预定序列冻结/关闭，但不再发研究权限。
- 接收时序区分平台接收、市场收到/接受与回执，按历史允许区间和配置延迟计算。
  竞价后不能回填开盘竞价，禁止时段撤单拒绝，无窗口/规则不生效/时钟不兼容明确 blocked。
  该模块不创建订单、成交或资金释放；最终估值日期固定，实际 NAV 分离仍待 LE-006。

24 项新增测试通过；此前与 LE-004 组合 48 passed，最后新增的日历和 lease 竞争测试
已在完整回归通过。完整 self-test：152 Node＋1156 Python 全部通过，有 1 条既有
Starlette 弃用提示。CLI maintenance PASS（51 operations）；git diff --check 与新文件
空白检查通过。CLI 无新增 API 可覆盖，ahunter skill/workflows 已同步内部阶段能力。

LE-005 保持 in_progress：完整 Episode create/observe/advance 入口、进程关闭确认、实际
预算触发、账户/成本与期末估值仍需后续模块集成验收。没有真实模型调用、模拟账本或
真实交易。下一步推进 LE-006 的账户、费用和企业行动估值。

LE-005 本轮运行时核验：现有 API LaunchAgent PID 62161 → 67100，state=running；
services、data-catalog、agent-access、agents、teams 均 HTTP 200，runtime 可导入
PhaseClock 与 submission_timing。最后 CLI maintenance 仍为 51 operations PASS。


## LE-006 账户/费用基础与原始收盘估值（2026-09-09，仍 in_progress）

新增 `account.py`、`fees.py`、`valuation.py` 和 `test_experiment_account.py`，
复用原事件/投影/lease；估值为永久独立 valuation 事实。详细语义见
[模拟账户契约](../../research/lagent-account-contracts.md)。

- cash_equity_v1 按 cash/stock lots、可用/可取日期、资源冻结、应付/负债保存状态。
  整批冻结单事务提交，不能预支计划内卖款或尚未确认取消；证券/市场规则绑定检查。
- 买入部分成交增量结算实际金额与累计费用，剩余部分仍预留最大费用，价差释放等回执。
  卖出仅产生实际净款，回执前与不同结算日期分别控制可用/可取；当日买入可卖依规则。
- 每订单累计最低佣金一次，零成交无费，新单独立计费；法定条目按给定当期包解析，
  缺项、日期不适用或佣金包含关系不一致拒绝。Decimal、费用到分 HALF_UP。
- 极小首笔卖出佣金不足记应付，不透支现金或虚构信用；可用余额扣除应付，后续结清
  不再计费，现金分录重建与余额一致。
- 实际记账与回执可见快照分开。checkpoint 封闭已处理区间，估值后不能补入早期成交；
  账户事件重建、同键冲突、五个事务故障点与已提交重试均通过。
- 初始/期末按合格原始收盘估值，初始持仓不使用成本价替代；停牌旧价标 stale，不强平。
  研究费用不扣本金；同 Test 初始/终值计算数学净收益，仍明确不具备正式评分资格。
- 复权/未来/非收盘价格、未知企业行动、特殊退市及不合格应收拒绝，不默认为零。
  企业行动登记、分红到账、持有期税负预留、送转股可交易时点和特殊估值尚未实现。

26 项新增定向测试通过；完整 self-test：152 Node＋1182 Python 全部通过，1 条既有
Starlette 弃用提示。测试断言生产 ledger_transactions/positions 没有写入。
CLI maintenance PASS，51 operations；无新 API 因而 catalog 不变，skill/workflows
已同步模拟政策与未完成企业行动边界。git diff --check 和新文件空白检查通过。

LE-006 仍 in_progress。下一步继续企业行动/股息税/送转股状态机，并随后接入
LE-007/010 的执行与阶段计划原子约束。没有正式数据/费用验收、真实模型调用或真实交易。

LE-006 基础变更运行时核验：现有 API LaunchAgent PID 67100 → 71591，state=running；
services、data-catalog、agent-access、agents、teams 均 HTTP 200，runtime 可导入
SimulatedAccount、OrderFees、AccountValuations。未启动第二个 API 或修改真实账本。


## 2026-09-09：LE-006 企业行动与税务增量

新增 `corporate.py`：按来源条款登记权益，分红除息后应收计 NAV、到账转现金；
除息前不与含息股价重复计值。显式历史税务政策支持自然月/年期限、FIFO 税务批次、
截至当前日期最高待扣税预留、派息前卖出的递延应付与派息后净回款扣税。
修复实际成交顺序改变时的批次选择，保留其他委托冻结数量及可重放取得序号。

整数非应税送转/拆股分别改变持仓与可交易时点；配股记录默认不参与。停牌旧原始价格
按已生效行动变换，当期原始收盘不重复调整。遗漏行动、未应用权益、过期/未来政策、
应税股份分摊、零碎股和特殊结算缺规则均明确拒绝。仍无正式历史规则包验收或 Episode。
规则依据、模拟约定与剩余边界见[账户契约](../../research/lagent-account-contracts.md)。

新增 28 项公司行为测试，账户合计 54 项定向通过；完整 self-test PASS，1210 Python
通过，1 条既有 Starlette 弃用提示，Node suite 同时通过。五个故障点及重放保持到账一次。
CLI maintenance PASS：51 operations，无新 API/CLI，skill/workflows 已同步内部能力。
LE-006 保持 in_progress；后续仍须完成特殊条款/正式规则绑定及 LE-007/010 执行接入。

运行时复核：现有 API LaunchAgent PID 71591 → 76463，state=running；启动监听就绪后，
services、data-catalog、agent-access、agents、teams 均 HTTP 200；runtime 可导入新增企业
行动/税务接口。没有第二个 API、真实模型调用或真实账本改动。


## 2026-09-09：LE-006 明确对价特殊结算

新增 `settlement.py`，将来源已证明的逐批现金/最终换股分配作为执行输入；完整持仓快照
及批次数量必须一致，不能仅注销一部分后保留未知剩余。旧股注销、固定应收、替代股及
税务结清/承接同事务提交。固定现金到证明的到账时间才可用；新股到可交易时点才可卖，
NAV 要求新证券自己的合格价格。旧证券不会沿用失效收盘或被重新接受为新委托。

纯换股核对投资成本承接、保留原取得期及税基；现金处置扣除已有股息税义务。缺失
对价、待终态委托、未应用企业行动、不可用来源、错误持仓快照或声明抹除税负均拒绝。
没有真实特殊结算来源验收，或有对价及混合税务分摊仍不支持。合格固定债权作为 NAV
应收是显式合同范围，不能把未知破产回收金额当作固定应收。

同时补上组合边界：送股时已有股息税义务须有明确税务份额分配；同一瞬间生效的多个
行动对停牌旧价须有组合价格规则，不按行动 ID 任意顺序调整。当前明确拒绝这些缺口。

新增 24 项特殊结算测试与 2 项公司行为边界测试，账户三组共 80 项定向通过，包括五个
事务故障点下注销/到账均一次与重建一致。CLI maintenance PASS：51 operations；没有
新增 API，因此 catalog 不变，skill/workflows 已同步。LE-006 保持 in_progress，后续
将进入 LE-007 成交/撤换与 LE-010 阶段编排原子接入，正式数据/税费验收仍独立要求。

完整 self-test：152 Node＋1236 Python 全部通过，1 条既有 Starlette 弃用提示。
现有 API LaunchAgent PID 76463 → 79101，state=running；services、data-catalog、
agent-access、agents、teams 均 HTTP 200，runtime 可导入 SpecialSettlements/SettlementTerms。
未启动第二个 API，未运行真实模型或写入真实账本。新文件空白检查与 git diff --check 通过。


## 2026-09-09：LE-007 普通分钟与原子共享容量

新增 `execution.py`：宿主市场接受事实经过 tick、数量、价格界限/笼子、时间窗与资源
整批验证；接受时间及稳定序号保留优先级。普通完整分钟按 high+tick / low−tick 的
封存滑点计算，只使用申报时点严格早于分钟开始的机会。触价不足、整体停牌及共享
容量不足都有明确 model_no_fill；竞价、限价队列、部分停牌和撤单竞争缺必要证据则
execution_evidence_missing，拒绝用分钟 no_fill 替代。

每 Test/证券/分钟只有一个来源绑定的容量记录，按比例和成交精度向下取整，全部订单
共享。账户新增内存暂存器，复用费用、股息税、FIFO 与回执逻辑；整分钟所有账户效果
和 execution 投影单事件/事务/CAS/租约提交。失败不会先用掉容量或留下部分记账；
已处理分钟不同身份重放拒绝，原身份重试复用结果。旧有 NAV/观察快照仍读取账户事件。

34 项新增测试通过，结合账户/企业行动/特殊结算共 114 项定向通过。覆盖逆向滑点、
申报分钟、优先级、容量取整、费用累计、整批资源拒绝、同键冲突、五个事务故障点、
多投影重建、暂存不外泄、陈旧版本及租约拒绝。CLI maintenance PASS：51 operations；
未新增 API/CLI，skill/workflows 已同步内部接口与边界。

LE-007 已从 todo 改为 in_progress。queue_replay、整阶段一次成功计划、固定延迟/Phase
接入、DAY 到期和受限撤换尚未实现。不能将市场接受层视为完整 submit_plan 或正式评分
入口；下一步推进队列与计划/撤换状态机。详细契约见[执行说明](../../research/lagent-execution-contracts.md)。

完整 self-test：152 Node＋1270 Python 全部通过，1 条既有 Starlette 弃用提示。
现有 API LaunchAgent PID 79101 → 83410，state=running；services、data-catalog、
agent-access、agents、teams 均 HTTP 200，runtime 可导入 ExecutionEngine 及证据契约。
未启动第二个 API，未运行真实模型或写入真实账本。新文件空白检查与 git diff --check 通过。


## 2026-09-09：LE-007 来源完整的队列重放

新增 `queue.py`：验证完整历史订单簿、连续事件序号、对手方和价格/时间优先级；
来源证明的模拟插入位置固定，真实前方数量消耗/已知取消后才可能获得机会。模拟成交
不修改历史簿。固定竞价必须满足统一历史价格/时点及量的对账，未消耗可执行对手方
与零成交结果矛盾时失败。无可执行队列的完整证据可以 no_fill，缺证据不转换为零成交。

每个 executor 共用 capacity_for，分钟成交量/规则/来源绑定同一容量；minute/queue
路由不能重新消费同一历史分钟。队列按明确 time/sequence 前缀提交并持久化游标/历史簿，
后续来源片段需要与旧结尾连续，全部效果与账户单事务提交。完整捕获先验证，再处理
前缀；未来逐笔只保存在宿主不可变 artifact，前缀事件不内嵌未来行情。后续 Episode
仍须在每个观察/动作边界切分回放，并将宿主原始证据隔离于候选。

31 项新增队列测试、结合普通执行共 65 项定向通过。完整 self-test：152 Node＋1301
Python 全部通过，1 条既有 Starlette 弃用提示。CLI maintenance PASS：51 operations，
未新增 API/CLI，skill/workflows 已同步。git diff --check 与新文件空白检查通过。

LE-007 保持 in_progress：阶段一次成功计划、模拟撤单确认/受限撤换和 DAY 到期仍未
实现；cancel_pending 尚不推断确认顺序。真实历史来源与完整 Episode 验收仍未完成。
下一步继续阶段计划、确认前成交/目标剩余与撤换资源的原子约束。

现有 API LaunchAgent PID 83410 → 88891，state=running；services、data-catalog、
agent-access、agents、teams 均 HTTP 200，runtime 可导入 QueueReplay/QueueEvidence。
未启动第二个 API、真实模型调用或真实账本写入。


## 2026-09-09：LE-007 阶段计划、确认与受限撤换

新增 `orders.py`：StagePlans 将主根会话授权、当前 Phase、事务内截止/租约重验、单阶段
一次成功计划与同 plan_id 幂等接入账户/执行投影。拒绝不占成功名额，同计划新单不预支
卖款或取消释放；新/替换身份保留，重复旧引用及自成交冲突整份拒绝。

新单先等待固定市场接收，随后重新校验市场价格笼子。撤单生效前旧单继续按已证明顺序
成交，生效后停止撮合而保持冻结，回执才释放；替换按目标减累计旧成交再验数量/资金，
不自动凑整、缩量、改价或继承旧优先级。禁撤、太迟、目标已满足、资源失败及跨日回执
分别记录；已有成交与取消确认不回滚。DAY 必须有完整收盘游标且不跳过未处理工作流。

匹配期间的市场动作必须对应已推进的精确 time/sequence 队列游标；QueueReplay 可处理
有计划固定生效时点的 cancel_pending 前缀。新增等待接受/回执状态继续占用资源，税务
FIFO 重分配和企业行动/特殊结算拒绝条件已同步。record commit 增加事务内 guard，
子会话即使复用主 actor 名也不能提交；主计划相同内容跨传输 action_id 重试复用原记录。

30 项计划/撤换测试通过；期间七组定向 197 项通过，最后增量与既有执行复验继续通过。
CLI maintenance PASS：51 operations，无新 API/CLI，skill/workflows 与执行契约已同步。
代码仍为宿主内部接口，候选侧只允许交易意图及受限响应；来源/费用由宿主补全，不能
直接透传原始账本对象。完整 Episode、候选工具边界和真实来源验收仍待 LE-008/010 等
集成，因此 LE-007 保持 in_progress。

完整 self-test：152 Node＋1331 Python 全部通过，1 条既有 Starlette 弃用提示。
现有 API LaunchAgent PID 88891 → 95282，state=running；services、data-catalog、
agent-access、agents、teams 均 HTTP 200，runtime 可导入 StagePlans/StagePlan。
未启动第二个 API、真实模型调用或真实账本写入。新文件空白检查和 git diff --check 通过。


## 2026-09-09：LE-008 候选 JSON 工具网关

新增 `gateway.py`：宿主绑定固定 PhaseSession 和 Test 封存包，仅接受白名单 JSON。
查询沿用时间过滤与已过滤结果审计，禁止产品查询重试复用拒绝，同一 action_id 不能
改作其他工具。观察仅返回固定边界可见快照与账户字段，不透传执行投影、未来队列或
计划提交内部事件。新单意图不能注入历史规则、费用或时间，宿主补全后交 StagePlans。

主根会话才能提交计划与工作摘要；子会话复用主名称仍无写权限。记忆投影/确认同事务，
跨阶段恢复同 Test/同候选的最后提交版本，异常回滚保留上一版本。计划已提交而网关
响应崩溃，可在阶段关闭后复用确认，不再次解析上下文、冻结资金或创建计划。
未知工具、参数注入和 provider/宿主异常都返回固定错误码；拒绝审计不保存原始请求。

24 项定向测试通过，覆盖未来数据、权限、身份重试、记忆回滚/候选包隔离、提交截止
竞争与业务提交后崩溃恢复。初次全量测试发现新增测试夹具漏填 simulated_at；修正后
定向全过，完整复验结果另记下文。CLI maintenance PASS：51 operations；无新增 API，
catalog 不变，skill/workflows 及网关契约已同步。

LE-008 从 todo 改为 in_progress。尚未完成 OS/文件/网络隔离、LAgent 执行循环适配、
模型/子任务与预算计量、受控搜索接线、阶段进程回收；不能将宿主分发器视为安全候选
运行环境或可评分 Episode。下一步接 LE-009 成本能力与预算，再完成运行时集成。

完整复验 self-test：152 Node＋1355 Python 全部通过，1 条既有 Starlette 弃用提示。
现有 API LaunchAgent PID 95282 → 600，state=running；services、data-catalog、
agent-access、agents、teams 均 HTTP 200，runtime 可导入 CandidateGateway/ToolFrame。
未启动第二个 API、真实模型调用或真实账本写入。git diff --check 与本次文件空白检查
通过；个人 ahunter skill 链接仍指向仓库源目录。


## 2026-09-09：LE-009 价格、费用预留与结算

新增 `budget.py`：显式 Test 封存额度、版本化 USD 等价计价表、独立且完整的 billable
用量维度、绑定执行器/适配器/调用身份的最大费用证据、完整无模型资源测量证据。
研究不能消费 env/evaluate 预留，模型/搜索不能放入资源预留分账。token_cap=null
保留，单次最大费用仍必须在调用前由宿主证明。价格和资源证据被永久成本记录引用。

预留/开始/未知/未开始释放/结算以同一 Test 的 CAS 投影串行提交；成本记录和证据引用
与余额变化同事务。不同连接并发不能各拿同一余款，冲突者重读后得到不足以授权下一
调用的明确结果。已开始未知费用不清零，取消与失败仍结算；超额保留实际成本并标记
cost_upper_bound_exceeded 或 platform_resource_failure，禁止新研究，评估预留仍可用。

同 invocation 的预留/结算重试复用原事实，不重复计费；新的 retry attempt 使用新的
invocation 并收费。供应商账单与等价比较费用分列。事务中断后保留预留或一次结算，
可按事件重建；新 owner 可核对迟到 usage，旧 owner 被租约 fencing 阻断。

LE-009 为 in_progress。当前仅 explicit 内部账本；calibrated 模式仍明确拒绝缺少标定，
三次完整基线/公式、完整资源测量器、真实价格/usage/费用上界能力、模型/搜索/计算
强制接线、全实验提案/标定汇总及终态 Test 后的费用核对尚待实现。formal_ready 固定
false；没有启动真实模型或历史 Episode。不得从 fixture 声称已具备可靠硬成本控制。

30 项预算定向测试通过，覆盖独立连接并发、未知/迟到成本、主子共享预留、六个事务
故障点、结算回滚、owner 接管、环境保护、重试与计价维度。费用乘加及余额差额使用
十进制系数精确运算，不受 Decimal 默认精度影响；额外测试证明 1e-30 USD 费用不会
消失，同量极小超支仍拒绝。CLI maintenance PASS：51 operations，无新增 API/CLI，
catalog 不变，skill/workflows 及成本契约已同步。

最新完整 self-test：152 Node＋1385 Python 全部通过，1 条既有 Starlette 弃用提示。
现有 API LaunchAgent PID 600 → 4370，state=running；services、data-catalog、
agent-access、agents、teams 均 HTTP 200，runtime 可导入 CostBudget 及价格/回执契约。
未启动第二个 API、真实模型调用或真实账本写入。git diff --check 与本次新文件空白
检查通过。下一步继续 LE-009 标定/派生、汇总及迟到费用核对，再接入候选运行时。


## 2026-09-09：LE-009 冻结标定与 calibrated 额度

新增 `calibration.py`，在任何 Test 启动前按 Definition 冻结根基线、完整重复样本、
价格、倍率/取整单位，以及所有任务无模型资源包络和阶段数。不能执行后更换样本或
价格，也不能省略留出资源尺度再按其表现补定预算。campaign 继承完整 Definition 的
隐藏范围，optimizer 不可读取包含留出尺度的详细内容。

CostBudget 现支持指定原始 calibration 样本的测量模式：研究没有比较金额上限，仍
逐调用验证和记账，环境/评估预留受保护，阶段时限由既有 guard 执行。正式样本只能
在对应标定结果存在时初始化 calibrated 额度；其他 plan、rerun、条件或价格不匹配
不能借用测量额度或结果。explicit 路径保持原有验证与语义。

complete 使用原计划全体 Test 的完成状态、完整时钟/关闭事实和已结算成本投影，
没有调用者输入的人工总价或可替换样本列表。每阶段须有研究计量，但不强加模型调用
或 token 最低值，compute-only 策略也按实际费用处理。缺资源费用、未知调用、失败
包络、缺阶段或不完整重复阻止派生。三个研究成本取最大值，按配置公式精确有理数
缩放，最后一次向上取整，所有任务额度和预算指纹原子封存。

LE-009 仍 in_progress。真实完整 Episode 与资源测量、模型/搜索/计算强制费用通道、
真实费率/usage 上界、终态 Test 后核对、全实验提案/标定汇总仍待实现。formal_ready
保持 false；fixture 阶段和计量记录不证明真实市场回放或真实模型能力，不产生正式分数。

新增 26 项标定测试通过；先前预算+标定两组合计 55 项通过，最后追加资源超额拒绝
测试继续通过。覆盖三次最大成本、缺样本/未知成本/缺阶段、资源失败、预算模式隔离、
冻结条件不可替换、精确向上取整、冻结及结果提交故障、隐藏任务尺度访问限制。
CLI maintenance PASS：51 operations；没有新增实验 API，因此 catalog 不变，
skill/workflows、成本契约与 ticket 状态已同步。

最新完整 self-test：152 Node＋1411 Python 全部通过，1 条既有 Starlette 弃用提示。
现有 API LaunchAgent PID 4370 → 8634，state=running；services、data-catalog、
agent-access、agents、teams 均 HTTP 200，runtime 可导入 Calibrations 与 CostBudget。
未启动第二个 API、真实模型调用或真实账本写入。git diff --check 与本次新文件空白
检查通过。下一步补终态费用核对与全实验成本汇总，再继续运行时/持久 Episode 集成。


## 2026-09-09：LE-009 终态费用与实验汇总

新增 `costs.py`：TerminalCosts 为终态已开始/未知调用追加一次确定结算事实，为从未
开始预留追加释放事实。原预算、事件、Test 状态和账户不改写；活动 Test 不能绕过
owner，确定 usage 不接受冲突替换。绑定原序号/调用哈希，证据与事实同事务，并发
重试只有一条。真实超额继续计价与标记失败，未知费用仍保留上界。

有效预算视图重新验证晚到事实，CostBudget.read、Calibrations.complete 和全实验
费用读取使用同一视图。标定因终态未知费用阻断时，核对后可继续派生，结果记录链接
核对证据。ProposalCosts 记录外层提案尝试及失败结算，不执行模型、不授予任务预算。
ExperimentCosts 在同一读快照内汇总真实 Test/提案调用，按用途及资源分账，比较或
标定摘要引用不重复计费，重跑的新执行另外计费；未知账单、预留和缺计量分别显示。

确定回执/供应商账单的后续更正协议、真实执行身份与 provider 对接、硬费用控制、
资源测量器以及完整 Episode/评估集成仍待实现。汇总是宿主 owner 接口，不暴露给
候选或 optimizer；accounting_valid 不表示正式评分合格，formal_ready 继续 false。

19 项新增成本测试通过，既有预算/标定 56 项也通过。覆盖终态取消/失败费用、未开始
释放、并发及事务故障、冲突回执、超额、缺计量、提案失败重试、比较引用不增费、
跨 Test 重跑另计、跨实验隔离及终态核对后标定派生。CLI maintenance PASS：51
operations，无新增 API/CLI，因此 catalog 不变；skill/workflows 与成本契约已同步。

最新完整 self-test：152 Node＋1430 Python 全部通过，1 条既有 Starlette 弃用提示。
现有 API LaunchAgent PID 8634 → 12593，state=running；services、data-catalog、
agent-access、agents、teams 均 HTTP 200，runtime 可导入终态/提案成本与汇总接口。
未启动第二个 API、真实模型调用或真实账本写入。git diff --check 与本次新文件空白
检查通过。下一步进入候选运行时，将预算授权、调用身份和阶段终止接入执行边界。


## 2026-09-09：LE-008 单次模型调用与阶段清理

新增 `runtime.py` 的 ModelInvocations：使用 Test 封存候选、模型和思考强度，复用
现有 LAgentAction 验证输出；驱动器声明并封存上界/单次执行/仅宿主 action/可核对
能力，legacy CodexExecutor 缺适配时 cost_capability_missing，不运行真实调用。

每次尝试先持久准备、预留，再把预算 started 与唯一分发 claim 在同一 CAS 事务提交。
两个恢复者不能都把幂等确认当作新分发许可；提交后响应/执行中断，按全局 invocation
身份 reconcile，未知不能自动重试。仅已结算 transport_error 按配置重试且另计成本。
固定一个主 actor，同名子 actor 也隔离；在途调用、子并发/累计身份及逻辑步骤读取
封存 runtime 参数。主子都用同一预算，资金不足或模型/上界能力不匹配不分发。

poll/cancel/reconcile 为非阻塞驱动接口。费用结算与 action 发布分别检查，关闭竞争
时已发生费用保留，迟到输出只存 hash；错模型/强度、schema 错误和超额不发 action。
PhaseClock.finish_close 现在等待模型清理确认，清理完成但费用未知可继续保留预留；
清理超时不会被伪装成进程退出。旧 owner 不可写，新 owner 在原阶段期限内可核对。

测试驱动没有启动模型、候选源码或子进程，formal_ready=false。真实 OS/文件/网络
隔离、生产驱动器、完整主子任务与工具循环、计算费用接线及 Episode 仍待实施；
LAgentAction 验证也不代替下游证据权限检查。业务 LAgent 的非交易边界保持原状。

22 项新增调用控制测试通过，既有预算/时钟合计 54 项也通过。覆盖并发恢复只分发
一次、开始提交后崩溃、分发前阶段关闭抑制、worker 接管、迟到与关闭竞争、费用/
模型/schema 拒绝、未知/重试、配置步骤/子任务限制及预算不足。fixture 测试没有
执行候选源码或真实模型；清理 quiescent 是驱动合同输入，尚非 OS 隔离验收。

最新完整 self-test：152 Node＋1452 Python 全部通过，1 条既有 Starlette 弃用提示。
CLI maintenance PASS：51 operations；无新 API/CLI，catalog 不变，skill/workflows
及调用契约已同步。现有 API LaunchAgent PID 12593 → 18605，state=running；services、
data-catalog、agent-access、agents、teams 均 HTTP 200，runtime 可导入 ModelInvocations。
未启动第二个 API、真实模型调用或真实账本写入。git diff --check 与本次新文件空白
检查通过。下一步落实隔离进程/生产驱动及完整候选 action 循环，再进行 Episode 集成。

## 2026-09-09：LE-008 本机候选进程后端

新增 `isolation.py` 的 DarwinCandidateRunner，已实际运行封存 Python 包。每次从
ArtifactStore 校验/复制 manifest 字节，独占写入防止路径别名覆盖，package 只读、
scratch 独立；最小环境、关闭继承 FD，使用固定 framework Python 真正可执行文件。
macOS default-deny 策略仅开放所需系统/标准库、具体动态库及包读取，scratch 读写；
包外文件、直接网络、fork/子进程、硬链接与原生系统查询不获授权。

显式 ProcessLimits 控制 CPU 信号、文件大小、FD、wall、输入/合并输出、scratch
字节/目录项及取消宽限。候选无换行输出或不读 stdin 不会阻塞宿主；超限/取消先
TERM 后 KILL，宿主异常也回收。仅 wait4 确认该 PID 已回收后返回 quiescent，保留
该进程 CPU 秒/peak RSS；scratch 检查以目录 FD/O_NOFOLLOW 防止重命名软链接竞争。

后端是一次性字节运输层。Apple 已弃用 sandbox-exec 且不支持第三方 SBPL；运行时
镜像尚未封存，CPU 信号可捕获，wall/scratch 监测可超调，没有硬 RSS/磁盘总配额。
阶段/预算、完整 action/委托循环、费用持久化、宿主崩溃孤儿恢复和内核拒绝审计仍待
实现，capabilities/result 均 formal_ready=false，LE-008 验收项保持未勾选。
详见[进程边界契约](../../research/lagent-process-isolation-contracts.md)。

新增 15 项测试通过，其中真实本地进程场景验证封存执行与用量、包外读写/目录读取、
只读包、env/FD、软硬链接、loopback TCP/Unix socket、shell/Python 子进程与 fork，
以及 CPU/文件/scratch 限额、输出洪泛、忽略信号后的回收、取消和宿主回调异常。
使用临时哨兵数据，没有真实模型、外部服务或生产文件读取；其余用例验证限额和
缺能力失败关闭。不会以这些局部测试替代正式隔离/历史 Episode 验收。

最新完整 self-test：152 Node＋1467 Python 全部通过，1 条既有 Starlette 弃用提示。
CLI maintenance PASS：51 operations；无新 API/CLI，catalog 不变，skill/workflows、
契约与 ticket 状态已同步。现有 API PID 18605 → 25390，state=running；services、
data-catalog、agent-access、agents、teams 均 HTTP 200，runtime 可导入本机后端。
git diff --check 和本次新文件空白检查通过。下一步接入阶段会话与双向 action/工具
运输，再补持久进程身份、资源计费和生产驱动能力；正式运行继续受真实依赖验收约束。

## 2026-09-09：LE-008 隔离候选的阶段工具管道

新增 `channel.py` 和 `phase_process.py`，把真实隔离进程接到 Test/版本/actor 固定的
CandidateGateway。宿主线程保留数据库和工具执行，独立进程监控线程处理有界管道、
wall/scratch 监测和回收；慢查询不会暂停候选的物理 deadline。JSON 行协议限定一个
未完成请求，拒绝重复键、非法/未结束帧、流水线及帧/响应/总量/次数超限。超限帧不
进入工具提交；阶段关闭、取消或进程停止后不再投递排队响应。

启动前按 Test/phase/actor/角色/process_id 记录 CAS dispatch claim，身份/并发检查
使用同一投影序号，未知 claim 不盲重发。同 actor 不可有另一未回收进程，子并发遵循
封存参数。claim 提交后再次检查阶段，确定尚未启动时可记录 not_started；宿主恢复
时未知的历史执行不能借此假装已回收。正常路径仅 wait4 后记录 reaped/用量，输出
只持久保留哈希。PhaseClock.finish_close 同时等待模型与候选进程清理。

新增 17 项测试通过，真实进程覆盖主候选观察/记忆/计划、同名子权限、未来数据过滤、
关闭中等待回收、慢宿主查询、非法/重复键/未结束/超限帧、响应与次数限制、输出超限
不落记忆和宿主异常；事务用例验证不确定启动不重发、确定未启动可抑制分发。
相邻网关/时钟/本机后端测试也通过。全部使用临时证据，不运行真实模型或外部服务。

本轮没有完成主子调度/模型 action 循环、资源预算与计算计费、心跳、宿主崩溃后的
进程出生身份核对或回收恢复；失去 lease/监控证据时清理保持未知，不能手工改为
quiescent 绕过阶段门槛。底层平台与资源限制仍在，formal_ready=false。协议及确切
范围见[阶段工具管道契约](../../research/lagent-phase-process-contracts.md)。

最新规定 clean-shell 完整 self-test：152 Node＋1484 Python 全部通过，1 条既有
Starlette 弃用提示。首次完整运行虽通过，但启动命令漏清除 ALL_PROXY；已等待其
退出并用完整规定环境复验，以复验结果为准。CLI maintenance PASS：51 operations；
无新 API/CLI，catalog 不变，skill/workflows 与契约已同步。现有 API PID 25390 →
31745，state=running；services、data-catalog、agent-access、agents、teams 均 HTTP
200，runtime 可导入 PhaseCandidateProcesses/ToolChannel。git diff --check 与新文件
空白检查通过。下一步补持久进程身份/崩溃恢复与资源计费，再推进完整 Episode 编排。

## 2026-09-09：LE-008/010 guardian 与 worker 崩溃恢复

新增 `guardian.py` 的 GuardedCandidateRunner，候选由独立 guardian 拥有；worker
线程继续承担 SQLite/工具，私有有界 socketpair 只传封装请求、响应与退出结果。
guardian 不把控制 FD、日志锁或 ArtifactStore FD 传给候选。worker 断线/心跳丢失、
停止标记或管道失败触发真实候选停止；wait4 和候选临时文件清理完成后写持久凭据。

准备请求绑定执行 identity、nonce、包、输入摘要、资源限制和运行时路径；launch
标记独占写入，guardian 持启动锁检查不可撤销 cancel 标记，并在候选创建前写 intent。
当前 owner 的 CandidateProcessRecovery 可在 closing 阶段核对：合法 receipt 给出
原执行清理结果；取得锁且没有 intent 才能证明未启动；有 intent 无回收凭据继续
pending。迟到 worker 不能穿过停止标记启动候选，恢复不凭 PID 消失杀进程或补造退出。

凭据只存输出哈希和实际候选用量，校验原身份/包/状态/有限非负用量；活跃或恢复路径
把凭据 artifact、invocation 关联记录、事件与候选进程投影同事务提交。提交前故障
不解除阶段等待，提交后响应丢失可幂等核对，旧 lease 不可写。IPC 结果丢失但凭据
完整时可采用清理事实；没有证明时保留未知。

新增 10 项测试通过。真实临时 worker SIGKILL 后，guardian 继续回收候选并写凭据；
原阶段仍等待 DB 清理记录，新 lease 提交原执行凭据后才能关闭。另覆盖普通/工具
运输、迟到 Popen 被拦截、未知 intent 不假作完成、句柄错配、用量篡改、恢复提交前后
故障，以及候选读写 journal、给 guardian 发信号和继承 IPC FD 被阻断。相邻阶段/
隔离测试通过；没有强制退出真实 API worker 或调用真实模型/外部服务。

guardian 自身或整机故障后的进一步证明仍未完成；有 intent 无 receipt 时不能清除
等待。运行时镜像、硬资源包络、guardian/worker 计算计费、完整服务心跳/队列/跨日
Episode 编排仍待接通，formal_ready=false。LE-010 改为 in_progress，仅表示候选清理
恢复已有实现，完整验收项保持未勾选。详见
[guardian 恢复契约](../../research/lagent-guardian-recovery-contracts.md)。

最新规定 clean-shell 完整 self-test：152 Node＋1494 Python 全部通过，1 条既有
Starlette 弃用提示。CLI maintenance PASS：51 operations；无新 API/CLI，catalog
不变，skill/workflows、契约及 ticket 状态已同步。现有 API PID 31745 → 39541，
state=running；services、data-catalog、agent-access、agents、teams 均 HTTP 200，
runtime 可导入 GuardedCandidateRunner/CandidateProcessRecovery。git diff --check
与本次新文件空白检查通过。下一步接入资源计量/预算及完整 Episode 编排，继续保留
未验证的真实数据、模型硬费用能力与全面隔离/恢复门槛。

## 2026-09-09：候选 CPU 计量、预算与终态核对

新增 `compute.py` 的 CandidateComputeCosts，要求候选 CPU 专用计量语义：仅将原
guardian wait4 的 user/system CPU 秒相加，并使用原价格表精确计价。worker/guardian、
内存/I/O、环境回放与评估明确排除，resource_scope_complete=false。当前本机后端
没有可证明的物理硬上界，正式 prepare 仍 compute_cost_capability_missing；仅显式
fixture 上界配合 fixture 价格表允许开发验证，不把样例额度称为真实费用能力。

原 Test/phase/actor/主子/包/guardian/限制/费用身份与 reserve 同事务登记，研究桶
不足不启动候选。CostBudget.started 与唯一进程 dispatch claim 同事务/CAS，不能把
幂等开始回应当新执行许可；主/同名子费用独立但共享研究预算，不借用环境/评估预留。
原始 guardian 凭据、measurement 记录和费用结算/释放同事务写入，实际超额继续收费
并阻断新研究预留；未知 usage 保留上界。预留阶段崩溃可核对释放，预算 started 后
确证未分发则结算候选 CPU 零值，供应商账单仍 null，也不声称平台其他开销为零。

当前 lease 可继续收取已经完成清理但未到账的用量；终态通过 TerminalCosts 追加
确定事实，不重开 Test、不改原预算或账户。collect 是会停止核对并结算的宿主操作，
纯读取仍使用 budget.read。来源/账本回执错配拒绝，重复核对不重复收费。CostBudget
和 TerminalCosts 仅新增内部 guard 组合点，原幂等、验证和 lease 约束保持。

新增 12 项测试通过，覆盖真实进程 CPU 计价、同名主子费用、能力/预算拒绝不启动、
超额实付、预留/开始崩溃、未分发与未知区别、活动/终态事务故障、新 lease 与幂等。
样例价格独立区分候选与回放 CPU，并在生成包络哈希前规范化。详见
[候选计费契约](../../research/lagent-candidate-compute-contracts.md)。完整资源测量/
硬上界、真实价格、生产模型驱动及 Episode 编排仍待完成，formal_ready=false。

最新规定 clean-shell 完整 self-test：152 Node＋1506 Python 全部通过，1 条既有
Starlette 弃用提示。CLI maintenance PASS：51 operations；无新 API/CLI，catalog
不变，skill/workflows、契约与 tickets 已同步。现有 API PID 39541 → 47221，
state=running；services、data-catalog、agent-access、agents、teams 均 HTTP 200，
runtime 可导入 CandidateComputeCosts。git diff --check 与本次新文件空白检查通过。
下一步继续完整 Episode 调度/回放接线，并保留尚未验证的全面资源与真实运行门槛。

## 2026-09-09：封存 fixture Episode、跨日回放与游标恢复

新增 EpisodeReplay：在原 experiment records 中原子封存 ReplayProgram 与初始投影，
绑定单 Test/定义/全部阶段快照。逐 tick 准备完整领域命令，再调用既有分钟、队列、
计划工作流、DAY 释放、应付款、checkpoint 与 NAV 接口；领域提交和 Episode 游标
之间发生中断，新 lease 使用同一 action_id/原输入恢复。动态工作流证明和收盘
游标只在准备时选择，恢复不重新推导；原候选/模型物理清理门槛继续约束阶段关闭。

5 日和 3 日声明窗口已验证第一日冻结并买入、次日 T+1 卖出、累计费用、每日与
末日 NAV。六类领域动作提交后恢复、重建投影、旧 worker fencing，以及收盘队列/
DAY 真实冻结资源释放均无重复。研究交接只在原阶段发生；激活提交后换 worker
保留原快照/截止时间并返回 recovery_required，不盲发候选或延长时限。

预算耗尽跳过后续研究但保留原回放终点；候选失败保留低收益及后续阶段；取消在
当前预备动作核对后停止，平台失败/必需数据缺失分别 failed/blocked，不生成
部分任务正式成绩。已识别的证据缺失保留动作/错误哈希，不陷入无限重试。
新增 22 项定向集成测试通过，详见
[Episode 回放契约](../../research/lagent-episode-replay-contracts.md)。

当前强制 evidence_kind=fixture，行情仅为合成短序列，研究由测试宿主交接；不能
证明全天行情完整或真实 LAgent/模型运行。企业行动/特殊结算回放、环境/评估预算、
每阶段候选恢复次数执行、共享 ResearchService 队列/心跳/Test 生命周期与正式
评分仍待接入。ready_for_evaluation 仅是内部回放投影，formal_ready=false、
resource_accounting_complete=false；LE-010 完整验收项继续不勾选。

CLI/skill 语义审查：无新路由/命令，catalog 保持不变；内部恢复/状态语义已同步
skill、workflows、契约和 tickets。CLI maintenance PASS：51 operations。

最新规定 clean-shell 完整 self-test：152 Node＋1528 Python 全部通过，1 条既有
Starlette 弃用提示。现有 API PID 47221 → 54228，state=running；services、
data-catalog、agent-access、agents、teams 均 HTTP 200，runtime 可导入
EpisodeReplay/ReplayProgram。本次新增文档链接、git diff --check 与新文件空白
检查通过。下一步继续共享队列/候选阶段执行与环境预算接线，保留真实数据、模型
费用能力及完整 Episode 正式验收门槛。

## 2026-09-09：Episode 候选阶段执行、恢复额度与持久取消

新增 EpisodeCandidates，将封存候选包的真实 guardian 子进程、CandidateGateway
及 CandidateComputeCosts 接入回放阶段。拥有者串行 tick，不新增队列/服务。
尝试准备、dispatch 意图、原预算 start/process claim 与结果分别有持久身份；
同 generation 的未知启动不重发，新 lease 先核对原进程和可能滞留的预算。
候选自身失败按配置消耗每阶段恢复次数，worker 中断不占用该次数；未知物理
清理或 usage 保留占用并阻止推进。实际截止时间不因恢复延长。

网关增加可选宿主 attempt_id：同一尝试保持工具幂等，恢复尝试读取最新已提交
记忆，已有计划仍按 plan_id 去重，避免重复冻结。query 仅追加状态/code/响应哈希，
不重复保存 items；必需来源失败不能被候选忽略后退出 0 掩盖。没有交易入口的
阶段收到计划直接拒绝，避免误请求不存在的接收时点证据。网关审计失败另写入
进程 gateway_failure 并停止候选，不依赖可能根本无法保存的工具错误事件。

真实进程集成验证暴露并修复了两类恢复归因问题：最后一条进程传输消息丢失时，
继续使用已验证的 guardian 持久回执；一次性取消回调必须先持久化 Episode
stop_reason，再通知进程，否则下一 tick 会把确定未启动误作可继续的中断。
取消测试现在等待真实子进程启动后触发，验证回收、CPU 结算及停止后不再启动。

新增 18 项测试，连同原网关和阶段进程测试共 59 项通过。包括封存代码驱动 5 日
买卖、真实 CPU 收取、每阶段首次失败后记忆恢复、不同恢复额度、dispatch/CPU
预留/退出后 worker 丢失、未知清理占用、阶段超时、必需/可选数据区别、正常
计划拒绝、必要审计失败和最终传输丢失。详见
[候选阶段契约](../../research/lagent-episode-candidate-contracts.md)。

行情/价格仍为合成 fixture，完整模型/搜索/委派、共享 ResearchService 队列/
心跳/Test 生命周期、企业行动回放、环境/评估预算及真实运行验收继续待接入。
formal_ready=false，无新 API/CLI。skill、workflows、契约、ticket 和索引已同步；
CLI maintenance PASS：51 operations，catalog 无变化。

最新规定 clean-shell 完整 self-test：152 Node＋1546 Python 全部通过，1 条既有
Starlette 弃用提示。现有 API PID 54228 → 63263，state=running；services、
data-catalog、agent-access、agents、teams 均 HTTP 200，runtime 可导入
EpisodeCandidates。CLI 检查、11 个本次文件的空白/文档链接检查与 git diff --check
通过。下一步接入共享队列/心跳及 Test 生命周期，继续保留正式执行门槛。

## 2026-09-09：共享 ResearchService 队列、Test 租约与生命周期

新增 research_work_queue 调度索引，将现有业务 Request 与显式接入的 fixture Test
交给同一 ResearchService 串行领取。Request 触发器同步状态，旧记录可幂等回填；
Test 的永久 service_input/service_control 记录支持恢复入队和取消事实，不伪造
带 Team/Subject 的业务请求。内部入队封存 descriptor，仅接受尚未领取的 fixture
Test；生产入口仍未配置实验执行器。

领取原子绑定 Service epoch 与 Test generation。Service 被接管后，旧 Test worker
立即失去领域写入权限；续租由既有 peer 心跳在同一事务中完成，候选阻塞期间也
维持 Test 租约。未知进程清理继续占用串行队列，不能跳过或盲重发。取消保留永久
记录，交由原 guardian/CPU 清理链处理。回放完成进入 evaluating/waiting，释放
执行队列供后续业务请求使用；评估器接入前不能标为 completed 或正式成绩。

新增 13 项集成测试通过，覆盖混合队列顺序、取消、跨 Service fencing、领取回滚、
真实候选阻塞时续租、未知清理占用、旧数据迁移、索引重建和重入互斥。另验证成交
已提交但 Episode 游标未提交时重建服务，沿原程序恢复而不重复买卖/收费。
详见[共享服务契约](../../research/lagent-shared-service-contracts.md)。

现有 current-requests 接口增加 nullable active_test_id，queued_count 改读共享
队列；active_request_id 与 requests 仍表示业务 Request。没有新增路由/命令，
catalog 仍为 51 operations；CLI maintenance 通过，skill/workflows 已同步。

最新规定 clean-shell 完整 self-test 于 2026-09-09T09:51:42Z 完成：Node 测试及
1559 项 Python 测试全部通过，1 条既有 Starlette 弃用提示。重载前确认 Research
无活动/排队请求；通过现有服务启动迁移，20 条旧业务请求均映射为 finished。
ResearchService PID 16996 → 74958、API PID 63263 → 74911，均由 launchd 持有且
state=running。services、data-catalog、agent-access、agents、teams、requests/current
均 HTTP 200；新接口返回 active_test_id=null、queued_count=0、service.state=idle。

本次只完成 fixture 共享服务增量。正式模型/搜索/委派、真实数据、完整资源计量、
企业行动回放及评估晋级仍待接通；LE-010 完整验收项保持未完成。

## 2026-09-09：精确收益评分与永久评估/比较快照

新增 evaluate.py：使用精确分数计算期末净收益、固定任务权重、配对重复与阈值。
先验证完整计划矩阵和所有样本有效性，再评分；失败/缺失样本不删除重算。10bp
改善、2/3 正配对及最差 −1pp 等号直接依封存配置判断，等分始终不晋级。调优与
留出只给各自诊断。原配置解析曾直接拒绝不同长度任务，现按定稿改为允许登记，
由评估器默认分组并阻止未经 opt-in 的混合平均。

EpisodeAssessments 在一致数据库快照中读取原程序及已提交 NAV IDs、阶段、清理
和费用证据，持久保存有效性原因与非正式 NAV 诊断；不改变账户或 Test 生命周期。
不选择最新/最高估值，其他 Test 的 NAV 拒绝绑定。同身份重试返回原评估；再次
评估需 rescore_of 链接，原费用核销和后续补评不能覆盖旧结果。

ComparisonAssessments 在运行前封存候选对/全部样本，并按首个评估身份封存比较。
缺失也保留原 inconclusive；修复需新计划并链接原失败。optimizer 汇总中不可计算
数字保留 null，不伪装成零收益或零胜次。原隐藏范围/owner exposure 规则继续适用。
登记指纹明确不是完整实际运行指纹，结果不授权晋级，也不修改当前业务或实验基线。

新增 36 项测试，与原登记/配置共 108 项通过；包括真实五日 fixture Episode 的
亏损 NAV、原始估值选择、配对门槛等号、不同权重/长度、未知清理、提交前后中断、
补评不覆盖原比较。详见[评估契约](../../research/lagent-evaluation-contracts.md)。

CLI/skill 语义审查：未新增路由/命令，catalog 保持 51 operations，maintenance
通过；skill/workflows 已补充分组、null 汇总、快照及正式晋级边界。LE-011 状态
更新为实现中；完整运行指纹、基线复用/CAS、留出冻结与评估阶段计费仍未验收。

最新规定 clean-shell 完整 self-test 于 2026-09-09T10:12:53Z 完成：152 Node＋
1595 Python 全部通过，1 条既有 Starlette 弃用提示。现有 API PID 74911 → 81660，
launchd state=running；services、data-catalog、agent-access、agents、teams、
requests/current 均 HTTP 200，ResearchService idle、queued_count=0。独立 runtime
可导入 EpisodeAssessments/ComparisonAssessments，精确收益计算探针通过。本次
11 个文件的空白/Markdown 链接检查与 git diff --check 通过。

## 2026-09-09：初始基线、最终选择冻结与单组留出登记

新增 ExperimentSelection，预先固定根候选和定义，使用当前选择记录 ID 进行最终
冻结 CAS，保留前序 ID/哈希和 exposure 快照。所有预定 Test 必须终止，物理清理
未知时不能冻结。现有登记器在同一 SQLite 写事务中检查冻结状态：冻结后不再添加
候选、调优/验证/标定计划或调优 rerun，原输入重试仍保持幂等。

final_holdout 登记现要求精确引用冻结选择及原定义，只允许初始/最终候选、所有
预定留出任务和完整重复矩阵。若最终仍是初始候选，保留一份重复集合；每次冻结只
接受一组计划，换名加跑或 holdout rerun 均拒绝。计划、样本及选择关联原子提交。

暴露检查使用实际交易日，跨个人项目的实验名称/任务名保留历史 knowledge：外部
声明、已读评估/事件/数据、旧非留出任务运行日期都不能通过改名抹除。当前角色
交易日重叠或日期不明的 exposure 不能声明未见。查看登记元数据仍记原读取审计，
但不把日期、种子、冻结 ID 自身当作未来行情反馈；实际结果读取仍改变当前暴露
报告，且不篡改原冻结/计划。冻结后、首次登记前发生新暴露会阻止登记。

新增 21 项测试，与既有登记/评估共 82 项通过，包括两连接冻结竞争、提交前后
恢复、完整样本原子登记、跨实验改名、旧调优日期复用、未知清理以及 exposure
不可擦除。详见[选择与留出契约](../../research/lagent-selection-holdout-contracts.md)。

当前控制器正常路径仍保持初始基线；实际比较晋级、完整运行指纹、正式成本计量
及真实留出执行仍待接入。LE-011 的日期暴露/不宣称统计显著条件已有内部测试，
其余晋级验收项继续未完成。无新 API/CLI，51 operations maintenance 通过；skill
和 workflows 已同步冻结限制、单组计划和暴露语义。

最新规定 clean-shell 完整 self-test 于 2026-09-09T10:29:12Z 完成：152 Node＋
1616 Python 全部通过，1 条既有 Starlette 弃用提示。API PID 81660 → 86892，
launchd state=running；services、data-catalog、agent-access、agents、teams、
requests/current 均 HTTP 200，ResearchService idle、queued_count=0。独立 runtime
可导入 ExperimentSelection/holdout_exposure，登记器 selection_id 参数存在。
本次 10 个文件空白/Markdown 链接检查与 git diff --check 通过。

## 2026-09-09：比较运行条件、原证据复核与基线晋级 CAS

比较冻结新增可选 expected_selection_id 与完整任务运行条件：语料哈希/世代、
搜索政策、实际模型/推理档、执行器、费用、价格表、预算与评分版本。原 bytes
验证并永久保留；规格、候选包和精确样本计划进入完整指纹。正式比较须在写事务
中绑定当前基线及原定义，不能把对父版本的比较当作对当前基线的比较。

qualified_comparison 在完成比较与提交选择时重新核对原 Test/首个评估身份、
哈希、终态、正式有效性、样本/规格/包、实际条件及正式 NAV，再以精确算术重算。
条件声明本身不产生验收资格。合格结果只给 eligible，apply_comparison 才以
expected revision 提交 promoted，保留初始基线及前序选择/比较历史。

等分、低收益、不稳定和不完整结果只记录决定而不移动指针；旧比较保留
stale_baseline，不能重新绑定较新 revision。两个合格比较竞争同一基线时只有一个
晋级，最终冻结后保留 selection_frozen。重复提交和提交后失联恢复不再晋级一次。
晋级后最终留出计划绑定初始和实际最终候选，而非把最终候选误认为初始版本。

新增 23 项测试，与现有评估/选择/登记共 105 项通过。正向测试用合成已验收宿主
评估契约验证消费边界和并发 CAS，不冒充实际 Episode 的验收证据。当前真实
EpisodeAssessments 仍不能生成正式资格或实际条件哈希；声明运行指纹不能把
fixture 变成正式样本。基线复用、实际资格/计费采集和完整运行编排仍待接入。

无新 API/CLI，catalog 保持 51 operations，maintenance 通过；skill/workflows
已同步 eligible 与 promoted 区别、过期/冻结结果及真实资格边界。LE-011 内部验收
项已有测试，但范围内基线复用/真实资格尚未完成，ticket 继续 in_progress。

最新规定 clean-shell 完整 self-test 于 2026-09-09T10:47:47Z 完成：152 Node＋
1639 Python 全部通过，1 条既有 Starlette 弃用提示。API PID 86892 → 92665，
launchd state=running；services、data-catalog、agent-access、agents、teams、
requests/current 均 HTTP 200，ResearchService idle、queued_count=0。独立 runtime
可导入 TaskComparisonConditions 与 apply_comparison，并验证比较选择绑定参数。
本次 11 个文件空白/Markdown 链接检查与 git diff --check 通过。下一步接入明确
预定的合格基线样本复用，保留原 Test 身份、样本资格与费用，避免挑最好样本或
因复用而虚增测试次数。

## 2026-09-09：预定基线重复组复用与原身份计数

新增 BaselineReuse.register，按不可变登记序号寻找最早合格完整基线组，匹配当前
选择、规格/任务/角色、repeat index/ID/seed 支持/seed、包与实际运行条件。只取
同一原计划中的完整组，不按收益排序、不拼接缺失重复，也不用后续补评替换无效
首评。已晋级候选的原合格组可作为下一轮基线，来源直接指向原 Test。

复用计划原子封存输入占位槽位、解析后的原 Test IDs、来源计划/比较/首评 IDs 和
哈希，只新建候选 Tests。全局 Test、原账单及账户/阶段/调用状态不复制，返回
new_test_ids 与 reused_test_ids；新候选的账户、阶段、预算和调用投影保持为空。
同输入重试不因较早组后来合格或当前基线晋级而重选来源。

登记器共享原计划/样本验证逻辑；比较冻结与重算会再次校验已封存来源和实际条件，
仅复用组允许已完成且属于原 plan_id，其余候选仍必须尚未启动。未经声明的旧
Test 引用、不同种子设计/语料世代/定义/角色、calibration/holdout 复用均拒绝。
晋级仍走原 CAS，原比较及费用不改。详见
[基线复用契约](../../research/lagent-baseline-reuse-contracts.md)。

新增 16 项测试，与原登记/评估/晋级/留出共 121 项通过，覆盖只增三个候选 Test、
原费用不复制、早期低收益组优先、不拼组、来源解析后不重选、当前基线来源身份、
无效首评不能被补评冒名替换、两连接同请求和提交前后恢复。正向来源仍使用合成
已验收宿主评估契约，真实 Episode 的数据/模型/计费资格生产及完整服务接线仍待
验收；本增量不开放正式历史运行。

CLI/skill 语义审查：未新增路由/命令，catalog 保持 51 operations，maintenance
通过；skill/workflows 已同步占位与封存身份、只执行 new_test_ids、来源选择和
费用/次数不重复计入的规则。LE-011 继续 in_progress，保留真实资格与接线验收。

最新规定 clean-shell 完整 self-test 报告于 2026-09-09T11:09:47Z 完成，
ok=true、exitCode=0。原终端会话已结束，以 reports/self-test/latest.json 核验
本次通过结果；不将预计用例数当作实测输出。API PID 92665 → 99803，launchd
state=running；services、data-catalog、agent-access、agents、teams、
requests/current 均 HTTP 200，ResearchService idle、queued_count=0。独立 runtime
可导入 BaselineReuse、validate_reused_plan、ExperimentRegistry 与
ComparisonAssessments。本次 12 个文件空白/Markdown 链接检查及 git diff --check
通过；未创建真实实验、调用模型或改动真实账单。

## 2026-09-09：企业行动与特殊结算进入 Episode 持久回放

检查真实执行链发现，已有账户公司行为和特殊结算执行器尚未被 Episode 使用。
现新增权益登记/生效/支付、特殊注销/现金对价支付五类回放点，原条款随程序封存，
所有效应沿用原领域幂等 API 与准备/领域提交/游标提交恢复机制。未复制模拟账户
或新增金融记账实现。

封存时验证周期内应有步骤、唯一身份、固定时点及来源顺序。登记必须在同刻市场
事件之后、checkpoint 之前；生效先于同刻支付。未来支付超过原终点则保留合格
应收，不提前到账或延长任务。持仓分配错配、不支持税务/零碎股、不可见条款和新股
缺价格均留下失败命令后 blocked，不伪造部分注销/分红或可计分终点。

新增 35 项测试，与原 Episode/企业行动/特殊结算共 111 项通过：实际买入后的登记、
持有/先卖后的税款与支付 NAV、送转可卖、默认不配股、现金退市/换股、税务处置或
承接、期外应收、坏生命周期、具体证据阻断点、五类动作换 lease/重建、取消以及
同刻生效/支付。测试使用显式合成条款；生产来源覆盖、任意实际持仓的分配适配器、
复杂税务及完整资源/模型/评估接线仍待完成，formal_ready 保持 false。

CLI/skill 语义审查：内部 ReplayProgram 新增回放点，不新增 API/命令，catalog
保持 51 operations。skill/workflows 与账户/Episode 契约、LE-006/010、索引同步；
LE-006/010 保持 in_progress，不将本增量等同完整运行验收。

规定 clean-shell 完整 self-test 于 2026-09-09T11:28:15Z 通过：152 Node＋
1690 Python，1 条既有 Starlette 弃用提示；CLI maintenance 通过 51 operations。
10 个文件空白/本地 Markdown 链接及 git diff --check 通过。确认 Research 空闲且
无排队后重载现有服务：API PID 99803 → 5167，Research PID 74958 → 5320，
launchd 均 running；Research 已有重载后的新心跳，idle、queued_count=0。
services、data-catalog、agent-access、agents、teams、requests/current 均 HTTP 200。
独立 runtime 可导入新增回放点类型；未创建真实实验、调用模型、操作 MX 或改动
真实账本。

## 2026-09-09：实验草案、预检与审计证据 API/CLI

将既有 ExperimentStore/ExperimentQueries 接入原本地 API，新增原例 preset、
草案 create/list/show、预检/历史、记录概览/明细、Test 事件、manifest/原 bytes
下载及比较反馈 12 个 operations。草案保留原输入和校验错误；同提交身份不覆盖
原草案或重做原预检。预检语义更新为真实能力未验收，仍 blocked、零模型/订单、
return=null，不因内部 fixture 已存在而授予运行资格。

隐藏明细/事件/导出/产物读取使用同源 POST 和稳定 audit_identity，原 exposure
必须先提交。跨实验/错误类型/未链接产物先拒绝，概览只返回 metadata/状态；
不同页/动作不能重用原审计身份。产物下载保留原 bytes/hash，过期 410、完整性或
幂等冲突 409、审计失败 503 且不释放隐藏内容。比较反馈只走原数值/决定白名单，
不返回 trace 或最终留出明细，不执行晋级。

新增 27 项测试，与原 CLI/登记/选择共 111 项通过。验证 create_app 实际接线、
完整配置与无效草案预检、请求体/同源/大小校验、原观察幂等、过滤与序号上限分页、
隐藏审计及故障不泄露、原 bytes/损坏/过期、CLI 到 API 的序列化/下载/错误透传，
以及不产生业务 Request、共享 queue、Test 或真实账本分录。

CLI/skill 语义审查：catalog 从 51 增至 63 operations，maintenance 纳入新路由
模块，commands.md 已重生成；skill/workflows、API 契约、索引与 LE-012 同步。
LE-012 改为 in_progress；候选/计划写入、Test submit/cancel/rerun、calibrate、
selection/finalize-holdout、tree 和完整生产运行仍待接入，不将本增量等同首版完成。

规定 clean-shell 完整 self-test 于 2026-09-09T11:49:45Z 通过：152 Node＋
1717 Python，1 条既有 Starlette 弃用提示。maintenance 通过 63 operations；
安装的 runtime CLI 可发现其中 12 个实验命令，个人 skill 链接正确。14 个文件
空白/本地 Markdown 链接及 git diff --check 通过。

API PID 5167 → 11873，launchd running；services、data-catalog、agent-access、
agents、teams、requests/current 均 HTTP 200，ResearchService idle、queued_count=0。
安装 CLI 实际调用 preset/list 均 HTTP 200，live OpenAPI 可见完整 12 operations。
运行检查只读，没有创建真实草案/实验/预检、写入暴露记录、发起模型或改动真实账本。

## 2026-09-09：候选原字节上传、独立提案与查询入口

增加 candidates register/list/show 三个 API/CLI 操作。上传完整 CandidateInput
清单、规范 base64 文件、提案元数据与可空 diff；宿主计算包/diff 哈希，不读取
客户端给出的主机源文件路径。上传与本地 seal_candidate 共用清单规范化/封存
实现，原换行/Unicode/非 UTF-8 bytes 不改写；入口、四类文件、路径唯一性、文件/
目录冲突、允许研究配置和全部 base64 在封存前检查。

原 ExperimentRegistry 继续同事务注册 package/proposal/links，保留不同提案的
父版本、假设和来源；同内容只复用包，不合并提案或增加 Test。未知父亲/跨实验、
自父、同身份改内容和冻结后新候选拒绝；提交后响应丢失可核对相同身份，冻结后
旧提交仍可确认。注册失败前已封存的内容文件可能暂无引用，但不会留下半套记录。

list 只返回 typed proposal 元数据并固定 cursor 序号上限；show 以本地 proposal_id
读取，验证原 bytes、package 记录和父链接，损坏返回 409，不泄露 Test 结果。
返回原 record IDs 后可走已有 manifest/artifact 命令读取代码或 diff。首次实际
create_app 候选登记可创建产物目录，测试证明不执行入口、不生成队列、Test 或账单。

新增 28 项测试，与既有 API/CLI/配置封存/登记共 165 项通过，包含上传与本地哈希
一致、原 bytes/diff、重复包独立提案、分页上限、非法输入、冻结/父链、提交前后故障
及 CLI 端到端。CLI/skill 语义审查已同步 catalog/commands/workflows；总计 66 API
operations，实验命令 15 个。LE-012 保持 in_progress，计划/运行/标定/选择写入和
正式数据/模型/计费接线仍待完成。

规定 clean-shell 完整 self-test 于 2026-09-09T12:07:00Z 完成：152 Node＋
1745 Python 通过，1 条既有 Starlette 弃用提示。maintenance 通过 66 operations；
安装 CLI 可发现全部 15 个实验命令，个人 skill 链接正确。14 个文件空白/本地
Markdown 链接与 git diff --check 通过。

API PID 11873 → 17188，launchd running；services、data-catalog、agent-access、
agents、teams、requests/current 均 HTTP 200，ResearchService idle、queued_count=0。
live OpenAPI 含 15 个实验 operations。真实库当前无实验草案，安装 CLI 候选列表
对未登记作用域返回预期 404；未为烟测创建真实候选/草案或写入暴露/费用。成功登记
及首次产物目录创建的端到端证据来自隔离 create_app 测试，不冒充真实库写入验收。

## 2026-09-09：历史包导入、定义解析与完整测试计划入口

增加 bundles import、definitions resolve、plans register 三个 API/CLI 操作。
历史包导入使用显式绝对本地目录，只封存清单及其声明文件；无效清单/文件/行返回
结构化 issues 和已保存的失败 record ID。原 source_acceptance 仍为 fixture_only
或 pending_independent_review，导入不代替完整覆盖或正式来源验收。慢封存和索引
在工作线程处理，其他 API 请求可同时响应；导入请求等待结果，不伪装为队列 Test。

新 DefinitionResolutions 将完整 config 与同实验日历包绑定，按 Task 初始研究时间
检查可用性；同 period 使用最早截止点。日历原文件哈希/行数/原行与索引必须一致，
缺日、晚版本、错日历、损坏/缺失原文或被篡改索引均留 blocked 解析记录，不缩短
原期间。成功 definition 和 resolution 同事务提交，保留原配置、完整规格、来源
哈希/资格及 period 截止时点。旧草案不被覆盖，同身份重试读原决定，变输入冲突。

公开计划入口复用原 Registry，核对原定义/候选字节、完整交错 candidate×task×repeat
矩阵、种子、角色、次数与冻结条件。计划及全部 Tests 原子登记，状态 created，
不创建共享 queue、业务 Request、阶段事件或真实账本分录。新优化定义/计划在冻结
后拒绝，旧提交仍可确认；响应丢失时原身份重试不会添加另一批样本。

新增 33 项测试通过，包含实际 create_app 首次产物目录接线、CLI 全流程及 blocked/
HTTP 错误透传、原输入保留、范围/日历故障、SQL 防修改保护之外的损坏检测、冻结、
不完整矩阵和提交前后恢复。此前相关 API/CLI/登记/导入维护共 176 项通过，补充的
慢导入测试证明等待封存期间其他 API 请求仍响应。CLI/skill 语义审查同步了 69
operations/18 个实验命令、命令参考和 workflows。正式数据、模型、资源/费用准入
及运行/标定/选择写入仍待接通，LE-012 和首版目标均保持进行中。

最新代码规定 clean-shell 完整 self-test 于 2026-09-09T12:34:59Z 通过：152 Node＋
1778 Python，1 条既有 Starlette 弃用提示。此前 1777 项完整自检通过后新增了工作
线程和并发测试，因此本次重新完整验证了最终实现。maintenance 通过 69 operations；
安装 runtime CLI 可发现全部 18 个实验命令，个人 skill 链接正确。

API PID 17188 → 26760，launchd running。services、data-catalog、agent-access、
agents、teams、requests/current 均 HTTP 200；live OpenAPI 含 18 个实验 operations。
三个新增入口对空 JSON 在存储前返回 400；安装 CLI 实际 list 返回 200，真实库
仍无实验草案。没有为烟测导入真实包、登记定义/计划、写入暴露/成本、调用模型或
改动真实账本。成功操作及首次产物目录创建证据均来自隔离测试库。

## 2026-09-09：持久 Test 取消与关联重跑入口

增加 tests cancel/rerun 两个 API/CLI 操作，统一接受 submission_identity 和非空
reason。新 ExperimentControl 保存不可变取消 receipt。未入队、无旧 worker 或
admission 的 Test，取消事件/状态/请求同事务提交；并发登记在 writer lock 下重查。
原共享调度索引缺失不能当成未执行而直接取消；运行阶段不接受无 lease 的直接终态。

已入队工作继续由 ExperimentWorkQueue/ResearchService 处理。队列 cancel 在同一
writer lock 下读取最新队列状态并记录原 service-cancel 事实、取消标志及 owner
receipt，防止完成/等待评估竞争时使用过期状态。running 的物理状态未知就保持
pending；waiting/evaluating 回到原服务关闭，不重跑候选、回放或评分。取消响应以
同一只读快照观察 Test 和队列状态，HTTP 202 不代表完成。终态 HTTP 200 保留原
completed/failed/blocked/cancelled；再发取消仅留痕，不改写原结果或添加阶段事件。

重跑复用原 Registry，保留 task/candidate/sample/plan 与 rerun_of/reason，新
linked Test 为 created，eligible_for_original_comparison=false，不入队、不替换
原样本。最终留出与冻结后新增重跑拒绝；已提交重跑在冻结或自身终态后仍可按原
身份确认，返回原新 ID 及当前状态。取消始终可请求，响应只含控制元数据和状态，
隐藏结果、费用与尝试仍走既有审计读取。

新增 28 项控制测试，此前与定义/API/CLI/登记/共享服务及 CLI 维护共 162 项回归
通过，最终控制/选择测试共 49 项通过。
覆盖未执行三个状态、已入队取消到原服务终态、未知候选清理、完整 fixture Episode
结束后的评估等待取消、缺失队列拒绝假终态、三个原终态不改写、冻结/留出/跨实验/
身份重绑定、提交前后故障及 CLI 202→200。实际 create_app 的隔离全流程也验证了
取消/重跑接线，没有队列或真实账本副作用。最终增加的一致状态快照纳入完整自检。

CLI/skill 语义检查同步 71 operations/20 个实验命令、生成参考与 workflows。
LE-012 仍进行中，正式提交/标定/选择写入、模型/数据/资源准入和首版完整验收尚未
完成；没有启用内部 fixture 入队作为公开提交替代品。

留出审计补充：纯 cancel/cancel_request receipt 的读取/导出保留审计，但只含
操作元数据时不消耗未来知识。精确字段、唯一 typed Test 关联和无附件同时成立才
豁免；额外结果字段、关联或附件继续计入暴露。三种夹带场景均由 API 审计读取验证，
不会用控制记录形成隐藏结果旁路。1804 项完整自检通过后补充此规则和两个参数化
用例，因此再次运行完整自检验证最终实现。

最终 clean-shell 完整 self-test 于 2026-09-09T13:01:46Z 通过：152 Node＋1806
Python，1 条既有 Starlette 弃用提示。maintenance 覆盖 71 operations；安装 CLI
可发现 20 个实验命令，个人 skill 链接正确，15 个改动文件的空白/本地链接及
git diff --check 通过。

API PID 26760 → 37813，launchd running；services、data-catalog、agent-access、
agents、teams、requests/current 均 HTTP 200，live OpenAPI 可见 20 个实验操作。
取消/重跑入口空 JSON 均在存储前返回 400；安装 CLI list 为 200，真实库仍无实验
草案。没有向真实 Test 发送取消、创建重跑/实验工作、调用模型或改动真实账本；
运行、清理与重跑的成功证据来自隔离数据库和本地 fixture 服务测试。

## 2026-09-09：初始基线、比较选择与最终留出冻结入口

增加 selection initialize/show/apply/finalize-holdout 四个 API/CLI 操作，复用
原 ExperimentSelection，不引入另一套晋级或冻结规则。初始化使用每实验唯一
canonical 身份和明确 definition_id/baseline candidate_proposal record ID；在
任何 Test 开始或预算建立前固定根候选，重复确认不覆盖初始基线。show 返回当前
ID/哈希/初始与当前候选/包/操作/冻结状态等 allowlist，不泄露比较样本或暴露报告，
不新增审计；完整原记录继续使用 records show/export。

apply 首先沿用原 feedback 范围限制，未知/最终留出比较拒绝。原 Controller
重新计算资格、成绩及运行指纹，使用 comparison 原绑定选择版本提交决定；客户端
不能供应成绩或 formal_ready/promotion_authorized。并发合格比较至多晋级同一
基线一次，过期/不充分/不稳定/未改进/已冻结决定不改动指针。固定 comparison
决定重试保留原记录，变输入或重绑选择版本冲突。

finalize-holdout 继续核对全部 Test 终态、原候选字节、已知物理清理和跨实验的
留出日期暴露。在同一事务内永久冻结选择；随后通过既有 plans register 只能登记
同定义、初始/最终候选的唯一完整留出计划。公开链路验证保留两个不同候选的六个
交错样本，迟到比较不能覆盖冻结决定。三个写入入口在工作线程核验/提交，不启动
执行器、模型或服务；响应 execution_available:false 不被 HTTP 成功或记录内
formal_ready 字段覆盖。

新增 27 项接口测试通过；此前相关选择/API/CLI/定义/维护回归 144 项通过。包括
并发 CAS、缺原字节、原成绩/资格字段伪造、冻结清理和暴露门槛、初始/最终候选完整
留出计划、原身份/跨实验、提交前后故障、实际 create_app 与 CLI 序列化/业务状态。
正向晋级使用合成 accepted-assessor 合同测试消费/CAS 边界；真实 fixture Episode
assessor 仍不能产生合格晋级样本，不把此测试当成正式资格生产或历史收益验收。

CLI/skill 同步为 75 operations/24 个实验命令，当前指针/永久冻结/原输入重试及
HTTP 业务状态说明已更新。正式 Test 提交、比较/标定生产、tree、完整模型/数据/
资源准入和首版完整验收仍待推进，LE-012 与总体目标继续保持进行中。

最终 clean-shell 完整 self-test 于 2026-09-09T13:20:45Z 通过：152 Node＋1833
Python，1 条既有 Starlette 弃用提示。maintenance 覆盖 75 operations，安装 CLI
发现全部 24 个实验命令，个人 skill 链接正确；13 个改动文件的空白/本地链接与
git diff --check 通过。

API 重载后 PID 43918，launchd running；services、data-catalog、agent-access、
agents、teams、requests/current 均 200。live OpenAPI 有 24 个实验 operations；
三个选择写入入口空输入均在存储前返回 400，selection show 对未知实验返回 404，
安装 CLI 同一路径也返回预期 404。实际 list 为 200，真实库仍无实验草案。没有
为烟测初始化真实基线、提交选择/冻结、写入暴露或费用、调用模型或启动历史 Test。

## 2026-09-09 比较登记与不可变结果入口

新增 comparisons freeze/complete 两个 API/CLI 操作。登记先绑定原计划、候选对、
当前选择版本和逐 Task 条件；所有附件哈希必须携带精确原 bytes，即使相同哈希已
存在，也不以仅知哈希获得另一记录的内容关联。完整上传先验证再封存，声明不授予
正式运行资格。新比较必须在原 Tests 执行前固定；修复不充分结果须新计划并关联
旧结果，留出及未知范围不能通过修复链接绕过反馈限制。

公开 complete 等待全部原 Test 终态后生成 canonical 结果，锁定各 Test 首个评估
身份；缺评估或不合格样本保留 inconclusive/null，后续重评不替换。已有内部不充分
快照的重试仍返回原结果。响应只含结果身份、状态和允许的聚合反馈，样本明细仍走
审计读取；生成结果不自动移动基线，也不启动执行。

新增 24 项比较接口测试；与评估/晋级/定义/API/CLI 相关回归 158 项通过，最终
修复链接的范围限制纳入完整自检。实际 create_app 隔离流程覆盖登记比较、取消
原样本并生成不充分结果。包括原字节哈希、早执行拒绝、首次评估固定、合格消费者
合同、选择版本、跨实验/留出限制、提交前后故障与无 --json 的 complete CLI。
合格正向案例仍为合成 accepted-assessor 消费者验证，真实生产者尚未验收。

catalog 已增至 77 operations，实验命令 26 个；命令参考、skill、workflows 和 API
契约同步。执行提交、标定入口、tree、实际模型/数据/资源准入与首版验收仍待完成，
LE-012 保持 in_progress。

最终 clean-shell self-test 于 2026-09-09T13:41:53.117Z 完成，报告 ok=true、
exitCode=0（本次 startedAt 为 13:37:39.435Z）。maintenance 覆盖全部 77 operations；
15 个本轮文件的空白与本地文档链接、安装 CLI 的 77/26 命令和个人 skill 链接通过。
API 重载后 launchd PID 50295、状态 running，六个健康/目录接口均 200。live OpenAPI
含全部 26 个实验操作；比较空登记及结果夹带输入为 400，未知范围 complete 在 API
及安装 CLI 均为 404，CLI 无需 --json。真实库仍无实验草案；正向写入仅在隔离测试中
验证，未启动真实模型、Episode 或正式准入。

## 2026-09-09 标定条件冻结与结果派生入口

新增 calibrations freeze/complete 两个 API/CLI 操作：公开冻结必须绑定原 initialize
选择及相同初始根候选/定义，保存价格表和全 Task 无模型资源证据；所有引用须上传
精确原 bytes。比较和标定共用上传校验，只有哈希不能取得新证据关联。新 campaign
在写事务内检查选择开放和当前版本；所有定义 Test 尚未执行/建立预算。原候选 bytes
重新校验；canonical campaign/result 的同输入重试不随执行、后续核对或最终冻结改写。

complete 只消费原重复、完整阶段和已结算成本，保留最大研究成本与原比例/预留/取整
公式。缺阶段、缺测量、未知 usage、资源失败继续返回结构化 blocked 原因，不丢弃
昂贵或失败样本。两个写入仅返回记录身份/类型与 formal_ready=false；资源/测量/
额度明细仍经审计读取，避免完整定义的隐藏任务尺度从写入响应泄漏。所有操作在
工作线程完成，不执行模型、生成测量或入队。

新增 30 项标定入口测试；标定/比较/API/CLI 和维护相关 122 项回归通过。覆盖原始
bytes、全任务与证据一致性、初始基线、迟到冻结、候选丢失、冻结后幂等、缺测量/
未知用量/超包络、最大值派生及审计、提交前后故障、跨实验/非法上传、实际 create_app
存储接线、CLI 空 JSON complete 和并行请求响应。首轮两处新断言误写既有 finalize
接口状态码，已按其原 HTTP 200 契约修正并复验，没有更改该接口行为。

catalog 为 79 operations、实验命令 28 个；skill、workflows、命令参考和契约同步。
LE-009/012 继续 in_progress：标定入口已经接通，真实费用/资源生产者、执行提交、
tree、UI 和首版真实验收仍待推进。fixture 价格与测量不构成正式运行资格。

最终 clean-shell self-test：152 Node＋1887 Python 通过，1 条既有 Starlette 弃用提示；
本次 2026-09-09T13:53:01.261Z 开始、13:57:26.074Z 完成，报告 ok=true/exitCode=0。
maintenance 覆盖 79 operations；17 个本轮文件空白/本地链接、安装 CLI 的 79/28
命令及个人 skill 链接通过。API 重载为 launchd PID 55555、running，六个健康/
目录接口均 200；live OpenAPI 有 28 个实验操作。标定空登记及结果夹带输入为 400，
未知范围 complete 在 API/安装 CLI 均为 404，CLI 无需 --json。真实库仍无实验草案；
没有为 smoke 创建标定、测量或真实执行。

## 2026-09-09 进化树与节点历史查询

新增 tree、candidates tests/comparisons、selection history 四个 API/CLI 读取，
共 83 operations/32 个实验命令。树保留独立提案与原父链，比较对手另列；初始/当前
基线从原选择历史派生，保留决定不移动指针。所有失败/取消/原重复和关联重跑可分页
查询；基线复用按实际 Test 去重，不把比较引用或 worker attempt 计作新样本。

分页固定记录和事件两个上限，在只读事务内取一致快照。后续状态/新增节点/结果/
晋级不倒填旧页。状态只读经哈希验证的原事件，原包 bytes 丢失不遮掉永久节点；
下载/候选原包查询仍检查完整性。节点及选择历史仅元数据，比较只给原聚合 allowlist，
留出和未知范围不借列表泄漏。收益/成本/阶段/暴露报告继续经原审计入口下钻。

新增 24 项进化查询测试；与 CLI、基线复用、选择及维护相关 109 项通过。覆盖空树、
失败父链、同包独立提案、关联重跑、双上限分页、历史基线与保留决定、父版本/对手
分离、迟到结果、隐藏明细、复用去重、原文缺失、损坏记录、cursor 绑定及实际应用/
安装 CLI 接线。故障测试先被不可变数据库触发器阻止，随后只在隔离 fixture 中移除
对应更新触发器模拟存储损坏；生产约束未改动。最终身份/对手/结果关联核验纳入完整自检。

CLI/skill、生成参考、API 契约与索引已同步。LE-013 从 todo 调整为 in_progress，
此增量是树及运行界面的查询基础，前端页面、键盘/展开交互、Episode 详情与视觉
验收仍待实现；不勾选完整界面验收项。执行提交与真实数据/模型/资源验收也仍待推进。

最终 clean-shell self-test：152 Node＋1911 Python 通过，1 条既有 Starlette 弃用提示。
本次 2026-09-09T14:09:05.432Z 开始、14:13:34.461Z 完成，报告 ok=true/exitCode=0。
maintenance 覆盖 83 operations；13 个本轮文件空白/本地链接、安装 CLI 的 83/32
命令及个人 skill 链接通过。API 重载为 launchd PID 60878、running，六个健康/
目录接口均 200；live OpenAPI 有 32 个实验操作。四个新查询对未知实验返回 404，
limit=201 返回 422，安装 CLI 同样透传 404。真实库仍无实验草案，没有为验证
创建候选、Test、选择或标定记录；本轮未修改前端，不声称页面或视觉验收已完成。
