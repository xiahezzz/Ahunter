---
id: LE-014
status: todo
phase: phase_1
depends_on: [LE-010, LE-011, LE-012, LE-013]
---

# 离线全流程与反作弊恢复验收

## 结果

在调用真实模型前证明时间、账户、比较与恢复机制满足定稿，不用历史收益好看代替正确性。

设计依据：[实施设计定稿](../../research/lagent-experiment-final-design.md)（D01, D03, D04, D05, D06, D08, D09, D10, D11）。数值均来自可配置 preset，不在代码中另写隐藏默认。统一完成标准见[索引](README.md)。

## 依赖

[LE-010](LE-010-durable-episode-orchestration.md), [LE-011](LE-011-evaluation-and-promotion.md), [LE-012](LE-012-api-cli-and-skill.md), [LE-013](LE-013-evolution-tree-and-observability.md)

## 范围

- 组织最小可手算完整 Episode fixtures，覆盖竞价卖出再买、限价部分成交、涨停排队、盘前撤换、收盘估值与多日记忆。
- 加入未来数据/搜索绕过/跨 Episode 泄漏、修改 env/evaluator、重复下单/释放/成本、持久化故障、旧 worker 和迟到模型攻击场景。
- 覆盖无 token cap 的预算停止、执行资源包络失败、候选缺陷与平台失败归因、原比较缺样本及基线 CAS 竞争。
- 通过 CLI/API/UI 展示所有候选、所有测试与关键分叉，验证来源、重跑与复用的真实语义。
- 形成逐项 acceptance 证据映射、失败修复记录和剩余真实运行依赖；不把 fixture 收益列为真实历史表现。

## 验收条件

- [ ] 原验收清单和 D01～D12 每条核心行为有通过证据或明确失败，所有必要项通过才允许进入 LE-015。
- [ ] 账户/事件重放一致，数据未泄漏，模拟交易未进入真实账本。
- [ ] clean-shell 相关测试、完整 self-test、git diff --check、CLI maintenance 均通过；代码重启验证记录齐全。
- [ ] 报告明确离线完成不代表真实数据、模型和费用能力已具备。

## 可能触点

tests/advisor/research/test_experiment_end_to_end.py（新增）；frontend 实验测试；docs/research/lagent-experiment-acceptance.md

## 验证

按仓库 clean-shell 要求运行有关测试和 self-test；不用无关宽泛重复测试代替关键场景。

## CLI / skill 影响

CLI/skill 语义审查与自动维护检查都必须通过，验证报告记录版本和实际命令。
