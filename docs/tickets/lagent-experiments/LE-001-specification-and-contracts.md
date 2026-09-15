---
id: LE-001
status: done
phase: phase_1
depends_on: []
---

# 实验规格、候选契约与配置解析

## 结果

把已授权设计变成可校验、可导出且身份稳定的实验规格；草案可以不完整，正式运行不能依赖隐式默认。

设计依据：[实施设计定稿](../../research/lagent-experiment-final-design.md)（D01, D02, D07）。数值均来自可配置 preset，不在代码中另写隐藏默认。统一完成标准见[索引](README.md)。

## 依赖

无；作为后续契约基础。

## 范围

- 审查现有 experiments/contracts.py 未验证起步代码，按定稿修改；不得按已有字段反推产品需求或标记已完成。
- 定义 ExperimentDraft、ResolvedSpecification、Task、CandidatePackage、CandidateProposal、TestPlan 和模块错误契约；采用 schema_version、内容哈希与原始值/生效值/来源。
- 每个数值声明单位、范围、默认来源和 null 语义；平台条件与允许候选变化分开，资金制度放 env.account。
- 实现原 20 万、08-02 启动、08-03 起 5 个交易日、GPT-5.5 high、无 token 上限预设及验证/留出预设；日历解析通过注入接口完成，不能硬写工作日列表。
- 定义候选源代码/提示词/依赖锁/配置的封存清单，提案身份与内容身份分开；未知资金制度返回 unsupported。

## 验收条件

- [x] 相同生效规格哈希稳定；时区、数字单位或规则版本变化产生可解释差异。
- [x] 缺失必填值、负交易日数、冲突日期输入、错误 null 和不合法阶段顺序明确拒绝；草案仍能保存错误明细。
- [x] 金额、持续天数、阶段时刻、并发和阈值可修改，无固定五日逻辑；配置导出显示全部生效值。
- [x] 修改宿主文件不改变已封存候选身份；重复内容提案保留独立来源而不虚增运行数。

## 可能触点

advisor/research/experiments/contracts.py；config/research/ 的实验 preset（实施时新增）；tests/advisor/research/test_experiment_contracts.py

## 验证

经 resolve Interface 验证配置组合、边界和哈希稳定性；不需要模型或真实数据。

## CLI / skill 影响

后续 API/CLI 创建请求与导出字段以本契约为准；本 ticket 若暴露实际入口，必须同步 catalog/skill，否则记录尚无入口。


## 实施证据（2026-09-09）

内部契约、版本化原例预设、注入日历的 resolve、参数来源/哈希验证、候选封存与
提案/TestPlan 契约已实现。47 项定向测试通过；完整 self-test：152 Node＋989 Python
通过。逐项证据及后续依赖见 [实现进度](implementation-progress.md)。

未增加 API/CLI 实验入口，catalog 不变；已同步 skill/workflows 的内部能力、预算
未解析状态和预检边界。CLI maintenance 与 git diff --check 通过。
已重启现有 API LaunchAgent（PID 16823 → 42262），服务及四个 Research catalog
接口均 HTTP 200，runtime 环境可加载新预设。

本 ticket 完成不表示真实数据、运行隔离、费用计量或 Episode 已通过验收；
永久登记与恢复事务由 LE-002 继续实现。
