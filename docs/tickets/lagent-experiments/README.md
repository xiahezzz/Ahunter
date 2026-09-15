# LAgent 实验开发 tickets

状态：2026-09-09，已开始实现。LE-001/002 内部契约、永久登记与离线验收完成；LE-003 导入/覆盖离线实现中，真实数据与完整规则审查待验收；LE-004 时间查询/搜索内部实现中；LE-005 持久阶段/快照内部实现中；LE-006 账户/费用、企业行动与估值实现中；LE-007 分钟/队列/阶段计划与撤换内部实现中；LE-008 候选网关、模型尝试、隔离进程与 guardian 实现中；LE-009 费用账本、标定、核对与汇总实现中；LE-010 候选/回放与共享队列 fixture 接入实现中，评估及正式模型执行待接通；LE-011 评分、比较/复用、晋级 CAS 与留出冻结内部接口已接通，真实资格及完整服务接线待验收；LE-012 草案、候选、历史包导入、定义解析、计划登记、取消/关联重跑、比较登记/结果、标定冻结/派生、选择提交/留出冻结与审计证据 API/CLI 已接通，执行提交待接入；LE-013 树、节点测试/比较与选择历史查询已接通，界面待实现；LE-014/015 待实施，LE-016 属于后续阶段。证据及运行门槛见 [实现进度](implementation-progress.md)。没有启动真实模型或真实历史 Episode；代码验证后已按要求重启现有 API。

选择/留出隔离的架构依据：[ADR-0108](../../adr/0108-separate-selection-feedback-from-final-holdout.md)。

权威依据：[实施设计定稿](../../research/lagent-experiment-final-design.md)、[模块契约](../../research/lagent-experiment-contracts.md)、[验收清单](../../research/lagent-experiment-acceptance.md)。用户已授权自行决定剩余核心规则；开发依据定稿执行，不再逐条等待设计确认。

## 交付顺序

1. LE-001/002 收敛契约与永久记录，审查已有未验证 scaffold。
2. LE-003 先交付真实数据能力报告；数据不可得时其他离线开发仍可继续，正式运行维持 blocked。
3. LE-004～009 完成 env、runtime 和 budget。依赖列表是集成前置条件；实现可用契约 fixture 开展，但不能跳过最终集成验收。
4. LE-010/011 接通持久 Episode 与比较，LE-012/013 提供 CLI/API 和进化树。
5. LE-014 完成离线整体及故障验收，LE-015 才进行真实预检、标定和研究运行。
6. LE-016 在首版实际验收之后接入自动生成候选的外层循环，不计入首版范围。

## Ticket 索引

| ID | 标题 | 依赖 | 状态 |
| --- | --- | --- | --- |
| [LE-001](LE-001-specification-and-contracts.md) | 实验规格、候选契约与配置解析 | 无 | 已完成（离线契约） |
| [LE-002](LE-002-records-artifacts-and-idempotency.md) | 永久记录、产物存储与幂等事务 | LE-001 | 已完成（存储与查询离线验收） |
| [LE-003](LE-003-historical-data-and-rule-bundles.md) | 历史数据包、规则费率包与覆盖预检 | LE-001, LE-002 | 实现中（导入/覆盖 fixture 已通过；真实包待验收） |
| [LE-004](LE-004-time-bounded-data-and-search.md) | 历史数据访问与日期受控搜索 | LE-001, LE-002, LE-003 | 实现中（固定界限查询/搜索 fixture 已通过） |
| [LE-005](LE-005-clock-and-phase-snapshots.md) | 交易日历、阶段时钟与冻结快照 | LE-001, LE-002, LE-003 | 实现中（持久时钟/快照与权限已通过 fixture） |
| [LE-006](LE-006-account-fees-and-valuation.md) | 模拟账户、费用与企业行动估值 | LE-001, LE-002, LE-003 | 实现中（账户/费用、分红税务、整数送转及明确对价结算；复杂条款与正式验收待补齐） |
| [LE-007](LE-007-execution-and-replacement.md) | 保守成交、队列证据与受限撤换 | LE-002, LE-003, LE-005, LE-006 | 实现中（分钟/队列/计划撤换；完整 Episode 集成待验收） |
| [LE-008](LE-008-isolated-lagent-runtime.md) | 候选隔离与单主多子研究运行 | LE-001, LE-002, LE-004, LE-005, LE-009 | 实现中（网关/计费调用控制/隔离阶段工具管道；正式隔离与生产驱动待集成） |
| [LE-009](LE-009-cost-metering-and-calibration.md) | 成本计量、原子预算与基线标定 | LE-001, LE-002 | 实现中（预留/结算/标定/核对/汇总离线通过；真实能力待集成） |
| [LE-010](LE-010-durable-episode-orchestration.md) | 持久 Episode 编排、取消与恢复 | LE-002, LE-005, LE-006, LE-007, LE-008, LE-009 | 实现中（候选/共享队列 fixture、金融回放与企业行动恢复；正式模型/环境费用/评估待接通） |
| [LE-011](LE-011-evaluation-and-promotion.md) | 预定样本评估、比较与自动晋级 | LE-001, LE-002 | 实现中（评分、比较/复用、晋级 CAS、留出冻结；真实资格与服务接线待验收） |
| [LE-012](LE-012-api-cli-and-skill.md) | 实验 API、ahunter CLI 与 skill 同步 | LE-010, LE-011 | 实现中（草案/候选/导入/定义/计划/取消重跑/比较/标定/选择冻结/审计/树与节点历史共 32 个命令；提交待接通） |
| [LE-013](LE-013-evolution-tree-and-observability.md) | 进化树、版本详情与运行可观测界面 | LE-012 | 实现中（树、节点测试/比较与选择历史 API/CLI；界面待实现） |
| [LE-014](LE-014-offline-adversarial-acceptance.md) | 离线全流程与反作弊恢复验收 | LE-010, LE-011, LE-012, LE-013 | 待实施 |
| [LE-015](LE-015-real-preflight-and-original-case.md) | 真实能力预检、成本标定与原五日测试 | LE-003, LE-004, LE-009, LE-014 | 待实施 |
| [LE-016](LE-016-automatic-outer-optimizer.md) | 后续自动外层 optimizer 与分叉提案 | LE-015 | 后续阶段 |

## 规则覆盖

| 定稿决定 | 主要责任 tickets |
| --- | --- |
| D01 模块与可变范围 | LE-001、008、010、016 |
| D02 全量配置和原例 | LE-001、006、009、015 |
| D03 阶段和信息时钟 | LE-003、005、010 |
| D04 数据时间与搜索 | LE-003、004、008、015 |
| D05 市场/资金/费用/估值 | LE-003、006 |
| D06 成交与撤换 | LE-007 |
| D07 单主研究与候选隔离 | LE-001、008 |
| D08 成本标定与停止 | LE-009、010、015 |
| D09 持久性和故障恢复 | LE-002、008、010、014 |
| D10 样本、评分与晋级 | LE-011、015 |
| D11 进化树及永久记录 | LE-002、011、013 |
| D12 CLI/API 与完成标准 | LE-012、014、015 |

## 已知执行依赖

- 历史分钟、开收盘竞价及必要队列证据尚未取得；旧原例预检受阻不是通过记录。LE-003 必须真实导入并验证，不能用日 K 或样例行情顶替。
- 搜索日期适配器尚未接入，选定连接的凭据/可用性和日期语义需要 LE-004/015 验收；不因此自动购买或修改既有行情源。
- GPT-5.5 high 的真实执行、usage 计量、价格表和硬费用控制能力必须验证；未验证前 calibrated 金额没有真实数值，公式已确定，不能凭空填额度。
- LE-001/002 已验证契约、封存、永久登记、事务、查询隔离与原文保留策略；真实数据 preflight 与 Episode 尚待 LE-003/010 等后续模块，实验草案/证据 API/CLI 已接通，执行入口仍待完成。

以上是开发和运行依赖，不是尚未作出的核心产品决定；阻断真实运行不阻止完成契约和离线实现。

## 统一完成标准

- 每张 ticket 的勾选必须有对应代码/产物及验证证据；预检、fixture、真实研究三种完成状态分开，不以文件存在代替完成。
- 遵守根 agents.md 与 RTK.md；保留工作区既有改动，不连接券商、不写真实账本、不操作 MX 页面，不改变 Market Daily 新浪唯一实时源。
- 所有数值配置化、所有关键行为可观测；不能禁用必要审计或删除失败/取消历史。隐藏评测的查询隔离与 exposure 记录在宿主执行。
- 复用现有 Research Service/queue、ArtifactStore 和本地 API，不增加多用户/多租户体系或独立调度服务。
- 行为改动运行有关测试及仓库要求的离线 self-test；每个测试命令使用新 clean non-login shell，移除代理变量。测试 Node 使用固定 Node 24 路径。
- 每次改变实际 API/配置/运行语义都同步 CLI catalog 和 ahunter skill；新路由模块须纳入维护发现，不能等到最后才补已有入口文档。
- 运行 CLI maintenance 与 git diff --check；影响 API 行为的代码完成规定测试后重启已加载 API 并核对相关接口，不启动第二个临时 API。
- 对真实数据缺失、未知费用、能力不支持、无效样本如实记录 blocked/failed/inconclusive；不伪造零成本、零收益、分钟数据或排队可成交结论。

## 设计阶段文档验证（历史记录）

仅新增/同步设计与 tickets，不新增实际能力或操作入口，故无需修改 CLI catalog 或 ahunter skill。已检查 16 张 ticket 的唯一 ID、完整章节、依赖无环、本地链接及 D01～D12 责任覆盖，均通过；git diff --check 通过。CLI maintenance 返回 PASS：51 API operations covered，skill command reference is current。本轮没有代码变更，不运行未验证起步代码的全流程或重启服务。
