---
id: RE-014
status: done
depends_on: [RE-013]
adrs: [0009, 0012, 0027, 0028]
---

# 实现 Daily Research Batch 与 Team 每日简报

## 结果

一次日常运行可以在统一 `as_of`、Team 选择和执行策略下，为多个证券分别启动 Research Cycle，并为每个 Team 确定性整理一份仅包含该 Team 结果的 Daily Team Brief。

## 范围

- 定义 Daily Research Batch 生命周期、版本指纹、Subject 列表和每个 Cycle 引用。
- 支持显式 `--codes`，并保留现有从合格 MX 事件和当前持仓确定性扩展候选证券的能力；记录每个 Subject 的入选来源。
- 对候选数实施版本化上限，超限时拒绝运行或要求显式缩小范围，不由模型筛选。
- 每个 Subject 使用独立 Research Cycle/Snapshot；同一批次共享 `as_of`、选定 Team IDs 和 Codex Execution Policy 版本。
- 各 Cycle 可按全局并发预算执行，单个 Subject 失败不抹去其他 Subject 结果。
- 为每个 Team 确定性渲染 `daily-brief.md`，按 Subject 列出该 Team 的报告入口或 blocked 状态。
- Brief 只读取已完成 Team Reports，不再次调用 Codex，不产生排名或新结论。

## 不包含

- 多 Subject Portfolio Manager。
- 跨 Team 今日总结、比较或总建议。
- 由模型选择候选证券。

## 验收条件

- [x] 每个 Subject 对应独立 Cycle ID 和 Snapshot；失败与重试不串扰。
- [x] 同一 Batch 的所有 Cycles 记录完全相同的 Team 集合、`as_of` 和执行策略版本。
- [x] Daily Team Brief 只包含对应 Team 数据，且不出现其他 Team 的结论。
- [x] Brief 渲染期间 fake Codex 调用计数保持为零。
- [x] 候选来源、去重顺序和上限稳定且有测试；超限 fail closed。
- [x] blocked Subject 仅以状态和链接出现，不生成替代建议。

## 可能触点

- `advisor/research/batch.py`
- `advisor/research/reporting/daily.py`
- `advisor/scheduler/premarket.py`
- `tests/advisor/research/test_daily_batch.py`
- `tests/advisor/research/test_daily_brief.py`

## 验证

```bash
./.venv311/bin/python -m pytest tests/advisor/research/test_daily_batch.py tests/advisor/research/test_daily_brief.py tests/advisor/test_coordinator.py
```
