# Episode 持久回放契约（开发 fixture）

`advisor/research/experiments/episode.py` 将已有阶段时钟、计划工作流、分钟/队列成交、
DAY 失效、应付款、企业行动/特殊结算和 NAV 串成一个 Test 的持久回放路径。当前只接受显式
`evidence_kind=fixture`，`formal_ready=false`、`resource_accounting_complete=false`。
它没有线程、模型驱动或调度队列；宿主逐次调用 `step()`，不创建第二个服务。

## 封存与时间

ReplayProgram 绑定 Test、specification_hash、所有精确 phase_id 的 SnapshotInput
以及有序回放点。时间必须带时区，顺序相同的时间点保留原输入顺序。必须有初始
NAV、每个声明交易日的收盘 checkpoint、DAY 失效及 daily NAV、原定末日 terminal
NAV；缺少日期、快照身份错配、越界、改写已封存输入均拒绝。

程序与初始 Episode 投影同事务提交；账户初始化、阶段创建均使用固定身份。
程序及后续预备动作仅供宿主读取，不进入候选网关。每个交易阶段的宿主回放先到
固定计划接收时刻，然后激活原观察边界；初始/盘后阶段到 snapshot_at。候选仍由
PhaseScope 和账户历史回执过滤器观察原 event_cutoff/snapshot_at，不能读取宿主
已处理但晚于观察边界的金融事件。active/closing/closed 期间不执行市场回放。

## 双事务恢复

每个点先写入 episode.pending：程序哈希、游标、固定 action_id、完整规范化命令
及命令哈希。工作流仅选择当时 due 的证券证明，DAY 失效仅选择实际存活证券的
收盘游标；没有到期工作流的预设点留下显式 skip。动态选择只发生在准备事务前，
恢复时使用已经持久化的原命令。队列插入证明仍由封存输入提供，缺失或多余由
原 QueueReplay 拒绝，不推造候选排队位置。

第二次 tick 调用原领域 API。领域事件/账户/执行投影先原子提交，Episode 随后
记录事件或 valuation 回执并推进游标。中间 worker 丢失，新 lease 仍重试相同
action_id 和完整输入，由原领域幂等检查返回已提交事实，不重新成交、释放或
收费。旧 worker 被 fencing 拒绝。Episode 事件可以参与已有 records.rebuild。

程序不能跳过早于下一回放点、或不晚于阶段激活边界的工作流；checkpoint 也不能
封住尚未执行的同刻工作流。已识别的 ExecutionEvidenceMissing/ValuationUnsupported、
CorporateUnsupported/SettlementUnsupported
保留失败动作和错误哈希，随后停为 blocked；通用存储/平台异常交给宿主核对，
不能推定为候选错误或零收益。

## 研究交接与结束

active 返回 `research_required`，反复 tick 不重复发起候选。换 lease 的 active
返回 `research_recovery_required`，保留原快照及实际截止时间；拥有者须先核对原
进程/调用，再通过 PhaseClock.resume 恢复权限。超时按原截止关闭，不延长时间。
`close_phase(phase_id, reason)` 检查当前阶段身份，迟到结果不能关闭下一阶段。
finish_close 继续使用原候选/模型物理清理门槛。

- 候选错误或恢复次数耗尽只结束当前阶段；原版本及已提交状态继续下一阶段。
- budget_exhausted 关闭后续研究，但回放继续到原终点，处理已接受计划并估值。
- 用户取消、平台失败、必需数据缺失分别产生 cancelled、failed、blocked 的
  Episode 投影。若已有预备动作，先核对完成这一个固定动作的游标，再停止；不
  新准备下一个动作，不生成部分任务的 terminal NAV 或正式评分。
- 全部阶段、回放点、工作流及估值回执完成后返回 `ready_for_evaluation`。这只是
  fixture 回放结果；Test 生命周期仍由未来共享队列适配器/评估器推进，不自动
  转 completed、不授予评分资格。终止的 Episode 投影不能原地重启。

## 证据与剩余工作

`test_experiment_episode.py` 的 22 项测试包含 5 日和 3 日声明窗口，第一日实际
冻结/买入、次日卖出、T+1、累计手续费、每日/终点 NAV，以及六类领域提交后的
换 worker、投影重建和幂等恢复；另验证收盘队列/DAY 实际冻结资源释放、阶段激活
提交后恢复、预算耗尽/用户取消、候选失败、坏行情/缺快照和迟到阶段回调。

行情是显式合成短序列，覆盖跨日状态而未证明全天每分钟/竞价/公司行动完整。
研究交接由测试宿主驱动，没有调用真实模型或完整 LAgent。因此 LE-010 完整验收
仍未勾选。生产 bundle/全市场事件选择、环境与评估费用预留/计量、完整模型/委派
恢复协调及正式评估仍待完成。现有共享队列接线仅接受内部 fixture，见
[共享服务契约](lagent-shared-service-contracts.md)。不得把当前程序作为真实历史实验入口。

CLI/skill：新增内部模块与恢复语义，无 API 路由或命令，catalog 不变；同步
`skills/ahunter/SKILL.md` 与 `references/workflows.md`。

后续增量：已有 [EpisodeCandidates](lagent-episode-candidate-contracts.md) 接入真实
guardian 候选执行、CPU 预留/结算、每阶段候选恢复计数及记忆继续。上述测试宿主
交接仍是回放模块自身的验证范围；完整模型/搜索/委派和生产实验接入仍未接通。

## 企业行动与特殊结算回放

程序增加五种宿主点：`corporate_register` 携带 CorporateTerms，
`corporate_apply`/`corporate_pay` 引用原 corporate_id，`special_settlement`
携带 SettlementTerms，`special_settlement_pay` 引用原 settlement_id。条款随
原程序封存，准备及执行仍使用同一个固定 action_id，不另建账户或记账路径。

封存时检查登记/注销身份唯一、后续引用已有来源、时点等于原条款，并要求原 Episode
终点以内的每次生效和现金到账恰好出现一次。配股不参与时无需生效/支付点；无现金
换股不允许虚构支付点。支付不能早于登记/生效，即使生效与支付时间相同也须先处理
生效。权益登记必须排在同刻所有市场事件之后，所有金融动作在同刻 checkpoint
之前，避免先封闭收盘账户再补入权益。初始封闭边界及更早的动作不能作为回放点。

未来到账若超过原终点，不延长任务、不提前支付；原 NAV 按账户规则保留合格应收。
送转和换股仍由账户的 available_at/sellable_on 控制可卖时刻，无需伪造一次成交。
特殊结算继续要求原条款中的逐批持仓快照与实际账户完全一致；当前尚未实现生产
数据适配器根据任意实际持仓派生这些分配证明，不能将预制 fixture 条款当作通用算法。

未知税务、零碎股份、条款不可见、持仓分配错配或新证券缺价格会保留原失败命令后
blocked，不产生部分分红/注销或正式终点收益。领域提交后 worker 丢失时，原事件
核对后才推进游标；取消也只完成已经准备的这一动作，不继续后续支付。

新增 `test_experiment_episode_corporate.py` 35 项测试，与原 Episode/企业行动/
特殊结算共 111 项通过。覆盖实际买入后的分红登记、卖出前后税款与到账 NAV、
整数送转可卖时刻、默认不配股、固定现金退市/换股及税务承接、期外应收、坏生命周期、
缺证据的具体阻断位置、五类提交后换 lease/重建，以及取消和同刻生效/支付顺序。
这些测试证明已提供条款的回放行为，未证明真实企业行动来源完整或正式计分资格。
