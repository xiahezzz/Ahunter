---
id: LE-016
status: deferred
phase: phase_2
depends_on: [LE-015]
---

# 后续自动外层 optimizer 与分叉提案

## 结果

在首版可观测实验契约上自动提出与测试 LAgent 改进，不新增第二套评估和进化树。

设计依据：[实施设计定稿](../../research/lagent-experiment-final-design.md)（D01, D07, D08, D10, D11）。数值均来自可配置 preset，不在代码中另写隐藏默认。统一完成标准见[索引](README.md)。

## 依赖

[LE-015](LE-015-real-preflight-and-original-case.md)

## 范围

- 实现 propose：读取允许的历史分支反馈、选择任意可用父候选、写分叉假设及 allowlist 内代码/配置 diff，封存并注册新候选。
- 复用 experiment 提交、预算/evaluate 和自动晋级；不能修改平台、任务、成本或借助隐藏 trace 生成候选。
- optimizer 模型、并发、迭代数、成本/无改善停止阈值配置化，提案成本独立列账且计实验总成本。
- 标记真实 optimizer 来源及每个关键分叉/调试/重新分支动作，失败提案和拒绝变更永久保留。
- 首版先实现单个提案者的串行提案循环；可配置更多策略须有实现能力声明，不能把配置开关当已实现。

## 验收条件

- [ ] 受控改进 fixture 自动形成分叉、测试、晋级或拒绝的完整记录；允许从淘汰节点重新分支。
- [ ] 尝试修改 env/evaluator 或获取隐藏评测数据会被拒绝且记录。
- [ ] 达到迭代/成本/停止条件后不再发新提案，已接受测试按原取消/预算政策收尾。
- [ ] 真实未改进分支也保留，不把树上有节点宣称为已证明递归自我改进。

## 可能触点

advisor/research/experiments/optimizer.py（后续新增）；既有 candidate/experiment/trace Interface

## 验证

先用可控提案器验证完整闭环与逃逸，再另行记录真实 optimizer 实验；不纳入首版完成率。

## CLI / skill 影响

未来新增 optimizer 配置/启动/停止/状态能力时同步 CLI/API/skill；当前无此可调用命令。
