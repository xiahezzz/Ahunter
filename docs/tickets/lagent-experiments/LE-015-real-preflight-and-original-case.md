---
id: LE-015
status: todo
phase: phase_1
depends_on: [LE-003, LE-004, LE-009, LE-014]
---

# 真实能力预检、成本标定与原五日测试

## 结果

用真实历史数据和 GPT-5.5 high 执行用户原案例，产出可追溯收益；能力不满足则交付明确阻断记录。

设计依据：[实施设计定稿](../../research/lagent-experiment-final-design.md)（D02, D04, D08, D10, D12）。数值均来自可配置 preset，不在代码中另写隐藏默认。统一完成标准见[索引](README.md)。

## 依赖

[LE-003](LE-003-historical-data-and-rule-bundles.md), [LE-004](LE-004-time-bounded-data-and-search.md), [LE-009](LE-009-cost-metering-and-calibration.md), [LE-014](LE-014-offline-adversarial-acceptance.md)

## 范围

- 在离线验收通过后，经 CLI 保存原实验草案并运行真实 preflight：历史数据/规则费率、日期搜索连接、模型实际可用性、费用计量/控制能力及执行资源包络。
- 保留 reports/lagent-tests/2026-08-03--2026-08-07/ 的旧 blocked 记录，新增关联测试；不能抹去 Aug2 边界下旧数据不可用事实。
- 前置全部通过才执行 3 次真实基线 calibration 并封存预算；随后执行原 20 万、08-02 23:05 启动、08-03～08-07 的预定调优重复。
- 注册一个说明具体改动假设的真实候选，与同条件基线进行预定验证比较；保存完整进化树和自动选择证据。
- 候选选择冻结后通过 finalize-holdout 固定初始基线/最终候选和样本，再执行预留最终留出；若该期间已暴露则按 exposure 另建未用任务，不改旧任务历史。
- 交付配置、来源/时间/覆盖、usage 成本、模拟账户和成交证据、全部 Test IDs、比较/晋级以及运行限制报告。

## 验收条件

- [ ] 没有真实数据/搜索/费用能力时保留具体 blocked，绝不能用 fixture、猜测分钟/队列或零费用补过。
- [ ] 所有真实主子调用都为配置模型且在许可阶段；没有 token 总量上限、没有隐式增加交易日或资金。
- [ ] 任何展示收益均可从完整真实 Episode 分录核对；预定重复不选最好、校正/重跑保留原记录。
- [ ] 只有真实来源、模型及预定比较完成才标记运行验收完成；仅完成预检或单次调优运行必须如实区分。

## 可能触点

reports/lagent-tests/ 新运行产物；实验 API/CLI；真实能力和交付报告

## 验证

真实执行通过已验证入口完成；外部故障只按既定恢复/新记录政策处理，不为得到正收益反复重跑。

## CLI / skill 影响

记录实际使用 CLI、版本和完成状态；若发现新恢复语义，同步 skill 后再交付。本 ticket 不是本轮立即启动研究的指令。
