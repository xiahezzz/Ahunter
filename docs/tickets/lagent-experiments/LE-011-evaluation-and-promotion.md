---
id: LE-011
status: in_progress
phase: phase_1
depends_on: [LE-001, LE-002]
---

# 预定样本评估、比较与自动晋级

## 结果

收益、稳定性与有效性按预设规则决定是否替换当前实验基线，所有样本选择和失败都有记录。

设计依据：[实施设计定稿](../../research/lagent-experiment-final-design.md)（D10, D11）。数值均来自可配置 preset，不在代码中另写隐藏默认。统一完成标准见[索引](README.md)。

## 依赖

[LE-001](LE-001-specification-and-contracts.md), [LE-002](LE-002-records-artifacts-and-idempotency.md)

## 范围

- 实现 task role、exposure 与 final-holdout 封存；原五日 tuning_only，验证期用于选择，最终留出不参与本轮晋级。
- 期末净值计算收益，任务内平均预定重复、角色内按固定权重汇总；不同长度默认分组，不混入年化或隐藏风险目标。
- 封存 3 次 repeat 计划及交错执行顺序；无 seed 能力如实记录，所有预定有效性条件先于评分。
- 实现 10bp 改善、2/3 正配对重复、最差不低于 -1pp 门槛和 tie/unstable/inconclusive 原因。
- 复用符合完整比较指纹及样本计划的基线记录，禁止按最好收益挑选；缺样本不删后重算，补跑新比较关联旧失败。
- evaluate 只给判定，experiment CAS 提交当前基线；过期比较不覆盖较新基线，运行中候选和业务配置不改。

## 验收条件

- [x] 用可手算 NAV fixture 验证收益、权重、配对与阈值等号；ties 保持基线。（内部算术及选择契约测试）
- [x] blocked/failed/cancelled 任一样本使比较 inconclusive，候选合法低收益不能替换。（内部快照及选择契约测试）
- [x] 基线已经晋级时旧比较失效，重新比较当前基线；只赢父版本不能晋级。（合成资格契约及两连接 CAS 测试）
- [x] 留出反馈已暴露会保留 exposure，不能通过改名重新声明未见；不把 3 次重复宣称统计显著。（内部日期/登记与评分测试；真实执行仍待验收）

## 可能触点

advisor/research/experiments/evaluate.py（新增）；Comparison/selection/exposure repository

## 验证

通过 assess/compare 测试全部决定分支，并用并发 CAS 验证选择提交竞争。

## CLI / skill 影响

比较指纹、确切样本、晋级/不晋级理由和 tuning_only 标识通过 CLI 输出；skill 区分调优与泛化。

## 2026-09-09 实现增量

精确分数评分、封存计划矩阵/交错校验、默认长度分组、源于 Episode 原始 NAV 的
评估快照，以及运行前候选对封存/原比较结果保留已实现。新评估必须链接旧评估；
缺失样本的公开汇总保留 null。新增 36 项测试，与登记/配置共 108 项通过。
配置解析不再禁止不同长度任务，由评估器禁止未经明确允许的混合评分。

完整运行指纹、基线复用/CAS、留出冻结及正式有效性/评估预算仍未接通，验收项
保持未完成。详见[评估契约](../../research/lagent-evaluation-contracts.md)。

后续增量：初始基线登记、带 expected_selection_id 的最终冻结和单组留出计划已
接通。冻结与新候选/计划登记使用相同事务边界，冻结后不新增优化工作。日期暴露
跨实验保留，区分登记元数据读取与实际未来反馈。新增 21 项测试，与登记/评估共
82 项通过。当前选择仍保持初始基线；完整比较晋级控制器尚未接入。详见
[选择与留出契约](../../research/lagent-selection-holdout-contracts.md)。

比较晋级增量：冻结可绑定当前 selection revision 与全部任务运行条件声明；完成
比较和提交选择均重新核对精确样本、原评估及正式条件资格。apply_comparison 以
CAS 提交 promoted，其他判定保留原指针，过期比较不能重新绑定。新增 23 项测试，
与原评估/选择/登记共 105 项通过。正向测试使用合成已验收宿主评估契约；真实
Episode 仍不能产生该资格。范围中的基线复用与真实运行资格/计费生产路径尚未
完成，因此 ticket 继续 in_progress，不将内部验收项视为首版真实运行验收。

基线复用增量：按登记序号选择最早合格完整重复组，将新计划基线槽位解析为原
Test IDs，只创建新的候选 Tests。规格、任务/角色、重复/seed、包和实际条件均
须一致；不按收益选取、不拼组、不用补评替换原首评。来源身份/哈希永久封存，
重试不重新选来源，原测试次数和费用不重复计入。新增 16 项测试，与前述模块共
121 项通过，见[复用契约](../../research/lagent-baseline-reuse-contracts.md)。
当前已有内部登记、评估、晋级、留出和复用消费接口；真实资格/计费生产与服务
完整接线仍待验收，ticket 继续 in_progress。
