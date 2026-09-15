# LAgent 预定样本评估与结果快照

实现位置：`advisor/research/experiments/evaluate.py`。依据定稿 D10；属于 LE-011
内部增量，尚无实验 API/CLI 或真实模型调用。实验选择控制器单独提交合格比较。

## 精确评分

`terminal_return` 从期初、期末 NAV 计算原始期间净收益，不扣除研究费用，不强制
平仓、不年化。十进制输入转精确分数，均值、任务权重、配对差和门槛判断全部用
分数计算。输出保留分子/分母；50 位十进制只用于展示，不参与判断。

`compare_returns` 校验封存规格与 TestPlan、全部验证任务、完整重复矩阵、交错顺序、
角色与候选对。未知稳定性规则拒绝执行。重复数、改善值、正配对比例和最差差值
直接读取封存配置。等分始终 not_improved，包括 minimum_improvement=0 的情况。
改善至少达到门槛且严格为正，正配对数达比例、最差配对不低于下限，才返回 eligible。
eligible 只是纯算术判定；它既不证明输入有效，也不修改任何实验基线。

每任务平均预定重复，再按固定权重汇总；每个 repeat index 先跨任务加权后比较。
blocked/failed/cancelled、未完成、无效或缺失样本使整体 inconclusive，聚合值留空。
额外样本不能替换原计划样本。合法低收益仍保留。不同长度默认分组，并拒绝产生
跨长度的单一晋级平均；显式 allow_mixed_durations 才使用冻结权重混合原始期间收益。
配置解析器现允许登记这些任务，分组限制由评估器执行。调优与最终留出分别输出
tuning_only/holdout_only，均不参与正式选择。无 seed 能力保留 unsupported，不宣称
配对控制了模型随机性，也不宣称三次重复达到统计显著。

## Episode 证据快照

`EpisodeAssessments.assess` 只接受 typed Test，且执行已停在 evaluating 或终态。
在同一 SQLite 快照中读取完整阶段、Episode 原程序与游标、物理清理状态、原程序
已提交的 NAV IDs、预算和终态费用核销。NAV 必须属于同一 Test 并覆盖每日与末日，
不会选择最近或最高估值。调用方不能传入收益分。

评估记录保存引用、投影序号/哈希、缺失原因及非正式 NAV 诊断；不修改账户、费用、
Test 状态或旧成绩。同一身份重试返回原快照；新评估必须通过 rescore_of 链接旧评估。
缺失样本、未知成本和未知物理清理不能被解释为零收益。当前 ReplayProgram 只支持
fixture，且完整环境/评估资源账单与正式回放验收尚不可用，因此 valid/formal_ready
均不会因此变真；共享队列的 evaluating/waiting 仍待正式评估阶段接线。

## 比较封存与公开汇总

`ComparisonAssessments.freeze` 在全部样本仍为 created、尚无预算时封存候选对、
完整计划、包哈希和登记指纹。只接受调优或选择验证计划。修复原 inconclusive
比较须新计划并链接旧失败，不能把原样本集合缩小。

`complete` 按原 Test 身份、首个评估记录的序号取样，不按收益挑选，保存确切评估
IDs/哈希及完成时状态。未就绪也可以封存 inconclusive；之后补评或补账不改变原
比较。登记指纹不冒充完整实际运行指纹。可选 expected_selection_id 将比较绑定到
冻结时的当前基线及原定义，并在写事务中检查 revision；未绑定的旧诊断不能晋级。

可选 runtime_conditions 必须覆盖全部预定任务，明确语料哈希/世代、搜索政策、
实际模型/推理档、执行器、费用、价格表、预算哈希及评分版本。全部哈希引用验证
原 bytes 并永久保留，完整指纹还覆盖封存规格、候选包与精确样本计划。缺任务、
维度或原 bytes 拒绝封存。该字段是运行条件声明，不是运行已经验收的证明。

完成比较及提交选择时，qualified_comparison 均重新核对原始 Test/评估 IDs 和哈希、
完成状态、正式有效性、原样本/规格/包、实际条件哈希和正式 NAV 标识，并重新计算
配对结果。只有合格宿主评估与预定条件匹配且达到门槛才给 eligible；缺资格仍为
inconclusive。eligible/promotion_authorized 表示允许尝试提交选择，不表示指针已移动。
实际初始基线及晋级 CAS 见[选择契约](lagent-selection-holdout-contracts.md)。

当前 EpisodeAssessments 仍不产生正式合格评估或实际条件哈希；提供条件声明不能
将 fixture 变成正式证据。正向控制器测试使用合成的已验收宿主评估契约，验证消费
边界，不能替代真实数据/模型/资源资格生产路径。真实资格采集仍待接入；预定基线
复用消费路径见[基线复用契约](lagent-baseline-reuse-contracts.md)。

optimizer 仍只能读取显式汇总字段。inconclusive 的不可计算数值、正配对数返回 null，
预定重复数仍为确切整数；不能把未知成绩填成 0。完整评估、样本、trace 和 artifacts
继续沿 Test/plan/comparison links 继承隐藏范围，owner 读取依旧先记 exposure。

## 验证及剩余工作

`tests/advisor/research/test_experiment_evaluate.py` 新增 36 项测试，包含可手算 NAV、
10bp/−1pp 等号、零门槛等分、配置比例、不同长度/权重、失败样本、实际五日回放
诊断、非原程序高 NAV 排除、跨 Test NAV 拒绝、原始评估选取、原失败链接及事务
提交前/后恢复。与原登记、配置测试合计 108 项通过。

初始基线/最终冻结、final-holdout 注册与暴露日期防改名校验已接入内部登记路径，
见[选择与留出契约](lagent-selection-holdout-contracts.md)。LE-011 尚需完整运行指纹
及实际运行资格生产路径，以及正式有效性/评估预算接入。基线复用消费接口已验证，比较晋级
CAS/历史标记及过期比较处理已有合成契约验证。
这里的算术、快照测试不替代这些验收，也不代表真实历史样本已经跑过。

`test_experiment_promotion.py` 新增 23 项测试，与评估/选择/登记共 105 项通过：
合格比较和实际提交分离、等分/低收益/不稳定保留、失败状态覆盖乐观评估标志、
缺运行维度/原 bytes、实际条件不符、旧比较不可重新绑定、两合格比较 CAS 竞争、
提交前后恢复、重算拒绝不一致结果，以及晋级后留出选对原始/最终版本。
