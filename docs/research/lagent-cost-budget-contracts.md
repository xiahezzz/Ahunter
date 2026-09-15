# USD 等价成本与预留契约

`advisor/research/experiments/budget.py` 是宿主内部账本，不调用模型/搜索，也不证明
供应商能力本身。当前支持 explicit 规格，以及绑定冻结 campaign/result 的 calibrated 规格；没有标定
引用时仍返回 calibration_required。

## 计价与资源

PriceTable 固定版本、USD 等价计价基准、来源及每项资源的计量语义。适配器必须把
缓存/非缓存输入、包含推理的可计费输出等供应商计数转换为互不重叠的完整维度。
缺维度拒绝，token 必须整数；模型订阅不能用全零等价费率。费用乘加和余额差额按
十进制系数精确运算，不由 ambient Decimal 精度舍去极小费用或极小超支。供应商账单可以未知，
与比较费用分别保存。真实价格及 counter 语义须由后续能力验收证明。

ResourceEnvelope 绑定封存规格、任务、价格表及配置安全系数，来源声明必须覆盖完整
无模型行情回放和计分。CPU 等资源基准乘安全系数后的费用不得超过显式 env/evaluate
预留。数学和哈希验证不等于真的跑过完整回放：测量器与真实资源上界仍待集成。

## 调用生命周期

reserve 接受宿主 CallBound：调用/actor/phase 身份、分账、适配器、执行器、资源、
价格/语义哈希、最大用量和逐调用证据。证据必须与声明完全一致。研究余额是总额度
减环境与评估预留；主子共享同一投影，模型/搜索不能消费后两种预留。金额不够时
返回 insufficient_call_budget，可存在仍有余额但不够下一次最大费用的情况。

start 在外部执行前提交；它和 reserve 都要求宿主活动作用域 guard 在事务内检查。
只有首次确认开始才允许分发，重放原确认不授权重复外部调用。超时或崩溃转 unsettled，
持有最大费用；取消不能自动清零。只有从未 started 的预留可 release_unstarted，
之后不可再开始或结算。settle 接受绑定原调用的完整证据和实际 usage，失败/取消调用
也计价。新 retry attempt 必须用独立 invocation，原调用保持未知时仍占预留。

实际超出最大金额或任一资源维度仍按真实数额入账，研究标记 cost_upper_bound_exceeded，
环境/评估标记 platform_resource_failure，禁止新研究。不得缩减实际费用或提前固定
行情终点来伪装遵守预算。env/evaluate 继续执行的调度责任在 Episode runner。

成本事实、永久证据引用、余额与状态投影同事务；SQLite CAS 阻止并发超支。相同
invocation 的预留/结算跨传输身份重试复用原事实，不重复收费；不同输入拒绝。所有
调用保留来源，投影可以重建。迟到 usage 不受已关闭阶段限制，但需要 Test 的当前
有效 owner；终态 Test 的迟到结算使用下述独立核对记录，未知费用仍须证据解决。

## 剩余集成

三次调优标定记录核对与公式派生已有下述离线接口；真实无模型资源包络测量、
实际模型/搜索/计算适配器及强制费用控制、与正式比较指纹的集成尚未完成。
终态核对与实验费用汇总已有下述内部接口。formal_ready 固定 false，fixture 不能使预检变 ready。
标定冻结/派生现已有 [API/CLI 入口](lagent-experiment-api-contracts.md)，费用账本写入仍内部使用。后续 runner 必须持久保存授权和外部 invocation 身份，在未知
结果时核对而非盲重试，并确保开始前预留涵盖所有主子计算和重试费用。


## 冻结 campaign 与标定结果

`Calibrations.freeze(plan_id, table, envelopes, expected_selection_id=...)` 必须在该 Definition 的任何 Test 离开
created 前调用。它绑定一个根初始基线、预定完整 calibration 重复集、价格、配置
公式，以及全部任务（包括留出）的资源包络和 phase_schedule 阶段数。资源预留未
解析时由已声明完整无模型测量×安全系数计价；显式预留不得低于该值。按 Definition
只有一个 campaign，重试不允许替换基线、样本、费率或尺度；记录继承隐藏任务范围。

原始 calibration Test 的 CostBudget.initialize 接收 campaign_id，研究 limit 为 null，
表示用于测量的非比较额度；env/evaluate 仍有限、逐调用上界和作用域 guard 仍必需。
其他 Test 不能复用该模式，rerun 不能替换原始标定。正式 calibrated Test 还须引用
calibration_result_id，全部价格/任务/资源/Definition 绑定须相同。

complete 不接受人工汇总金额或任选样本列表，只读取预定 Tests 的 completed 状态、
完整 finished 时钟/关闭事实和已提交成本投影。缺完整阶段研究计量（可仅计算，无
最低模型调用要求）、环境/评估费用、未结算 usage 或失败包络时拒绝。候选错误、取消
和预算耗尽的阶段不能当作完整标定。实际市场回放/计分是否足够仍由后续 runner 与
真实验收保证，手工 fixture 完成状态不能提升 formal_ready。

C 为全体配置重复中研究已结算成本最大值，P 为调优预定阶段数。对每个目标任务按
`ceil_unit(multiplier × C × target_phases / P + target_E)` 派生，使用精确有理数再
按 rounding_unit 向上取整，不在中间步骤舍入。零模型策略不被人为加模型最低消费。
所有任务额度和包含 campaign/样本计量的 budget_fingerprint 同一结果记录提交。
初始基线成本、价格、配置倍率、任务阶段/资源或条件变化均不能复用原结果；最终比较
指纹接入和 calibration 样本的晋级排除还须在 LE-011 集成。


## 终态费用核对与汇总

`TerminalCosts.settle(test_id, invocation_id, receipt)` 只接受已处于终态的 Test，沿用
同一套 receipt 绑定、用量与计价验证。它按 Test/调用身份追加永久 cost 记录，保留
原 cost_budget、事件序列、Test 状态及模拟账户不变；不能增加原来不存在的调用，
不能覆盖已确定结算，也不能给运行中 Test 绕过 worker lease。

`release_unstarted` 仅允许原状态 reserved，要求 started 必须先于任何外部分发的
协议得到执行。已 started/unsettled 的调用继续保留最大预留，取消不构成零成本证明。
核对事实绑定原预算序号与调用哈希，证据和事实同事务，重复/并发提交只保留一份。
实际超额仍入账并标记 accounting failure，不将 cancelled Test 改成 completed。

`effective_budget` 在只读视图中重新验证并合并核对事实；CostBudget.read、标定和
实验汇总使用该视图。标定原本因终态未知 usage 被阻断时，可在全部费用核实后重算
并封存结果，结果明确引用核对来源；已有确定 usage 不允许被另一回执静默替换。
供应商账单未知仍单列未知，后续更正确定回执/补充账单的专门修正协议尚未实现。

`ProposalCosts.record_started/settle` 记录外层提案调用（含失败与重试），使用独立
实验/调用身份、封存价格/上界/回执，未结算尝试持续显示 unknown。record_started
本身不提供任务预算授权，也不执行模型；实际外层调用仍需自己的执行授权与费用
控制。提案工作不能消费 Episode 的环境或评估预留。

`ExperimentCosts.read` 在同一数据库读快照中汇总所有真实 Test 和提案调用，包括
calibration、失败、取消和重跑。每个 Test/调用身份只计一次，比较/标定结果引用不
新增执行；重跑拥有新 Test 身份，费用另计。返回已结算等价 USD、未核实/未开始的
预留上界、供应商已知账单及未知数量，并按用途/分账展开。已运行却缺预算记录是
missing_cost_tests，不把证据缺口当成零费用。accounting_valid 只说明账本是否已
核对且无超额失败，formal_ready 始终 false，不表示策略结果可正式评分。

汇总器是宿主 owner 接口，可能包含隐藏任务成本，不作为候选/optimizer Data Product
公开。暂无 API/CLI。真实适配器仍须把这些全局执行身份接入分发与核对，防止外部
结果未知时盲重试或把同一次物理调用注册成两个逻辑执行。

候选计算增量：CandidateComputeCosts 已将 guardian 的候选 CPU 用量接入研究预算，
支持与进程 claim 原子开始、来源/结算原子记录、未知保持以及活动/终态核对。
范围仅为候选 CPU，完整资源计量和正式硬上界仍缺失；显式 fixture 额度不能作为
真实费用能力。详见[候选计费契约](lagent-candidate-compute-contracts.md)。

公开冻结强制传原 initialize 选择 ID，要求其定义/初始候选与计划一致，保存原选择
关联；新 campaign 在同一写事务内确认当前选择版本及开放状态，并核对候选原 bytes。
既有内部未绑定选择的调用仍兼容，但不会由公开重试补写关联或改变历史哈希。已冻结
campaign 可幂等重读；新 campaign 在最终选择冻结后拒绝。两个公开操作只返回元数据，
全部资源/测量/额度详情继续审计读取；formal_ready=false，不授予执行能力。
