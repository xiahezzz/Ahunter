---
id: LE-007
status: in_progress
phase: phase_1
depends_on: [LE-002, LE-003, LE-005, LE-006]
---

# 保守成交、队列证据与受限撤换

## 结果

限价触及不等于可成交，涨停和竞价必须有相应排队证据；盘前承诺的撤换由 env 执行。

设计依据：[实施设计定稿](../../research/lagent-experiment-final-design.md)（D06）。数值均来自可配置 preset，不在代码中另写隐藏默认。统一完成标准见[索引](README.md)。

## 依赖

[LE-002](LE-002-records-artifacts-and-idempotency.md), [LE-003](LE-003-historical-data-and-rule-bundles.md), [LE-005](LE-005-clock-and-phase-snapshots.md), [LE-006](LE-006-account-fees-and-valuation.md)

## 范围

- 实现普通完整分钟模型：买用 high＋1tick、卖用 low－1tick、限价及价格界限检查；申报所在不完整分钟不提供回填机会。
- 实现 queue_replay_v1 的可重建队列/逐笔消耗及固定竞价价分配；特殊涨停、跌停、开收盘/复牌竞价与撤单竞争按固定路由要求细证据。
- 同 Episode 每证券每分钟买卖/所有委托共享 1% 历史容量；不同执行模型不能重复分配，所有参数取 resolved spec。
- 整份计划原子验证接收，单阶段最多一次成功；拒绝可重提，禁止自成交冲突、同旧单冲突和预计回款。
- 实现 DAY 生命周期、撤单确认前成交、合计目标扣除累计旧单成交、替换资源再验证、无自动缩量或重定价、新优先级。
- 有证据时 no_fill；缺必需队列/事件先后证据时质量失败，不能隐式转换为零成交。

## 验收条件

- [ ] 涨停有价无量、排队未消耗、部分竞价成交、价格笼子拒绝都有可解释结果。
- [ ] 旧单 100 股成交后目标 300 股，撤销确认前又成 50 股，新单只能按目标剩余 150 股再校验，不擅自凑整。
- [ ] 多个订单/撤换不重复用流动性或资金；新单不继承旧队列位置，失败不撤回既有成交。
- [ ] 竞价后失败计划不回滚早前计划；DAY 到期和迟到撤换不跨日续单；历史路径不被模拟单改写。

## 可能触点

advisor/research/experiments/execution.py；advisor/research/experiments/orders.py（新增）

## 验证

通过 submit_plan/advance 测试模拟成交与委托状态，至少包含一例 minute 足够、一例 queue 足够及一例必要证据缺失。

## CLI / skill 影响

API/CLI 必须区分 accepted、exchange accepted、partial fill、cancel confirmed、model_no_fill 和 evidence_missing。

## 2026-09-09 普通分钟与共享容量增量

新增 `ExecutionEngine` 市场接受校验、完整分钟模型、稳定接受优先级与持久共享容量。
账户暂存器支持整分钟多成交/费用/税负与执行容量一次提交；无成交与证据缺失区分。
34 项新增执行测试，结合原有账户三组共 114 项通过；queue、Phase 计划及 DAY/撤换尚未
实现，因此保持 in_progress，不能当作完整 Episode 接口。
详见[执行契约](../../research/lagent-execution-contracts.md)。

## 2026-09-09 完整队列重放增量

`QueueReplay` 已支持完整历史簿/事件验证、来源证明的插入位置、前方量消耗及已知取消、
固定竞价价格/量分配、同一共享容量和按明确时间/序号截断恢复。历史簿不被模拟成交改写，
缺失/矛盾队列失败与合法 no_fill 分开；完整捕获 artifact 不内嵌到提前截点的事件中。
31 项新增队列测试，结合普通执行共 65 项通过。阶段一次计划、模拟撤单确认/受限撤换
及 DAY 生命周期仍待实施，保持 in_progress。

## 2026-09-09 阶段计划与撤换增量

新增 `StagePlans`：main 根会话授权、事务内作用域/截止重验、一次成功阶段计划、plan_id
幂等、整份指令/资源校验与固定延迟工作流。市场动作须对应已证明队列游标；撤单生效
停止旧单，但回执前保留冻结；之后按累计旧成交计算目标剩余，独立校验并分配新优先级。
DAY 需要完整收盘游标，不能跳过早期工作流或跨日续单。30 项计划/撤换测试已加入。
完整 Episode 与真实来源验收仍待集成，保持 in_progress。
