---
id: LE-009
status: in_progress
phase: phase_1
depends_on: [LE-001, LE-002]
---

# 成本计量、原子预算与基线标定

## 结果

比较使用同任务相同总成本额度，所有主子研究与执行费用可核对，累计 token 不单独设上限。

设计依据：[实施设计定稿](../../research/lagent-experiment-final-design.md)（D08）。数值均来自可配置 preset，不在代码中另写隐藏默认。统一完成标准见[索引](README.md)。

## 依赖

[LE-001](LE-001-specification-and-contracts.md), [LE-002](LE-002-records-artifacts-and-idempotency.md)

## 范围

- 建立版本化模型/搜索/计算计价表，区分 USD 等价比较成本与真实供应商账单；模型订阅不能直接记为免费。
- 实现调用前最大费用 reserve、usage settle、unknown/unsettled 核对，所有并发共用一个预算；无法证明控制上界的执行器拒绝正式比较。
- 定义 calibration 运行类别与 3 次完整调优标定规则、C/P/1.25/1USD 向上取整公式；标定测试不能同时作为晋级样本。
- 无模型全周期回放测资源包络并乘配置安全系数，生成 env/evaluate 预留 E；隐藏任务只按预定尺度派生，不靠其表现标定。
- 预算不足停止新模型，保留 E 完成市场；超出执行包络是平台失败而非提前截断收益。
- 成本汇总按执行身份去重，计入所有失败/重试/标定/提案，比较引用基线不重复收费。

## 验收条件

- [ ] 两个并发 reserve 不能超支；取消或迟到 invocation 仍保留实际成本，未知费用不能自动变零。
- [ ] token_cap=null 仍可按成本授权；余款不足以覆盖下一调用最大费用时明确停止理由。
- [ ] 标定不完整、usage/价格无法核对或执行包络不足时 blocked；不会自动选择更便宜样本。
- [ ] 改变价格/额度/标定系数产生新比较指纹；环境和研究成本分账但同属总额度。

## 可能触点

advisor/research/experiments/budget.py（新增）；runtime usage 适配；成本与标定持久记录

## 验证

预算 Interface 并发、失败结算、标定派生与去重测试；使用虚构价格 fixture 明确标注，真实费率能力 LE-015 验证。

## CLI / skill 影响

calibrate、预算显示、pending usage 与错误语义由 LE-012 暴露；skill 不承诺现有 Codex CLI 已满足硬成本控制。


## 2026-09-09 原子费用账本增量

新增宿主 `CostBudget`、版本化 USD 等价价格表、精确调用绑定的最大用量证明和实际
usage 回执。explicit 额度从 Test 封存规格读取；完整无模型回放/计分资源测量证据与
安全系数绑定后，研究、environment、evaluation 独立分账且总额度不变。

主子调用共享同一个 CAS 投影；预留、成本记录和证据引用同事务。开始前提交 started，
超时/取消未知保留最大预留，实际结算释放差额。超额不截断真实成本，并标记费用上界
或平台资源失败；供应商账单单列，未知不作零。重试调用独立收费，同一 invocation
与已结算回执不重复收费。旧 owner 不能写，新 owner 可核对迟到 usage。

尚未接真实适配器、运行时强制费用控制、calibrated 三次完整基线与公式派生、全实验
提案/标定聚合或终态 Test 后费用核对。当前仅 explicit 内部账本可用，formal_ready
固定 false；所有验收项仍待集成验证。详见[成本契约](../../research/lagent-cost-budget-contracts.md)。


## 2026-09-09 标定冻结与派生增量

新增 `Calibrations.freeze/complete`：按 Definition 唯一冻结初始根候选、完整 calibration
计划、价格表及所有预定任务的无模型资源尺度，要求在任何 Test 离开 created 前完成。
重新提交不能改计划、价格或任务包络。原始 calibration 样本可使用研究部分无比较
金额上限的测量账本，仍须逐调用证明上界、保留环境额度并遵守阶段 guard；正式样本
不能使用该测量模式，也不能在结果缺失时初始化 calibrated 额度。

结果汇总只接受原计划全部重复的 completed 状态、完整阶段关闭事实和已结算成本；
缺阶段研究计量、环境/计分费用、未知 usage 或资源失败都拒绝。研究成本取最大值，
按原配置倍率、各任务预定阶段数/P 和预先冻结的 E 精确派生，仅最终按配置单位向上
取整；所有任务额度及预算指纹同记录提交，留出额度不读取留出表现。

此为离线合同与 fixture 集成，真实完整 Episode/资源测量器、价格/执行能力、终态后
费用核对及全实验成本汇总仍未完成；formal_ready 继续 false。标定结果记录不参与
普通晋级，比较器仍需在 LE-011 按预定样本用途验证。


## 2026-09-09 终态核对与全实验费用增量

新增 `TerminalCosts`：终态 Test 的已开始/未知调用可追加实际结算，未开始预留可释放；
原预算投影、事件和 Test 状态不变。原状态/哈希/回执绑定与证据引用同事务，冲突回执
拒绝，两个核对者重复提交只计一次。活动 Test 仍必须通过当前 owner，未知费用和
真实超额都不会被清零或抹去。确定 usage/账单的后续修正协议仍未实现。

`effective_budget`、CostBudget.read 和标定汇总读取核对后的费用视图；标定可在终态
未知费用得到核对后完成派生。`ProposalCosts` 单列外层提案已开始/结算事实；
`ExperimentCosts` 按 Test/调用身份汇总全部标定、失败、重试、重跑与提案，比较引用
不会重复收费。未结算、未开始持有、未知供应商账单及已运行缺成本记录分别显示。

仍为宿主内部接口，无外部模型执行或新 API/CLI。真实费率/usage/物理上界、资源
测量器、运行时强制计费与 LE-010/011 结果/比较集成尚未验收；LE-009 保持实现中。

## 2026-09-09 候选 CPU 计量与预算接线

CandidateComputeCosts 已将 guardian wait4 的候选 CPU 接到研究桶：绑定/预留原子
保存，预算 started 与进程 dispatch claim 同事务，实际用量与来源记录同事务结算。
主子共享预算但执行身份独立；超额按实收费、未知保留上界，预留/开始间崩溃、新 lease
和终态核对均有路径。确证未分发才释放 reserved，started 的未分发则结算候选 CPU 零值。

只覆盖候选 CPU，不含 worker/guardian、内存/I/O/回放/评估。当前没有可证明的正式
硬上界，正式启动仍拒绝；只有显式样例上界加 fixture 价格表可开发验证。以上验收项
不因此完成，详见[候选计费契约](../../research/lagent-candidate-compute-contracts.md)。

## 2026-09-09 标定操作公开入口

Calibrations 的条件冻结和原测量派生已接通 API/CLI；公开登记要求绑定原初始选择，
新登记在写事务内核对选择开放及当前版本，并重新验证候选包 bytes。价格/usage/
完整资源证据随请求封存；返回仅记录身份，测量与额度通过审计读取。30 项新接口
测试及标定/比较/API/CLI 相关 122 项通过。真实计量、完整资源包络生产与正式验收
仍未完成，验收项保持待验证。详见 [API 契约](../../research/lagent-experiment-api-contracts.md)。
