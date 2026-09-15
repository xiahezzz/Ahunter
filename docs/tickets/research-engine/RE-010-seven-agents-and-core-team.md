---
id: RE-010
status: done
depends_on: [RE-004, RE-005, RE-006, RE-007, RE-008, RE-009]
adrs: [0002, 0003, 0004, 0011, 0015, 0021, 0023, 0025]
---

# 定义七个 Research Agents 与 A-share Core Team

## 结果

Market、Social、News、Fundamentals、Policy、Hot Money 和 Lockup 成为七个独立、Team-agnostic、版本化的 A Hunter Research Agents，并由 `a_share_core@1` 精确组合。

## 范围

- 建立七个 Agent Manifests、各自 instructions、Data Product 依赖、查询预算和 details Schema。
- 实现通用声明式 Agent Runner：构建 Capsule、调用 Codex Executor、解析并校验公共 Finding 核心与 details。
- 将有价值的 A 股专家规则和提示内容改编进仓库，并记录对应来源与必要 NOTICE。
- 建立 `a_share_core@1` Team Manifest，精确锁定七个 Agent 版本，不包含模型或 Decision Pipeline 覆盖。
- 计算 Research Invocation Key，并在同一 Cycle 中让多个 Teams 共享相同 Agent Finding。
- 对 evidence 引用、claim 完整性、风险、失效条件、时间边界和 details Schema 执行确定性 Finding 质量检查。

## 初始依赖映射

| Agent | 核心 Data Products |
|---|---|
| Market | 证券身份、日线、行情快照、技术指标 |
| Social | 公司信息流、本地 MX 事件 |
| News | 公司信息流、市场/宏观信息流 |
| Fundamentals | 基本面、三张报表、机构预期、行业背景 |
| Policy | 公司信息流、市场/宏观信息流、概念/行业 |
| Hot Money | 日线、热点、资金观察、概念、个股资金、龙虎榜、行业背景 |
| Lockup | 解禁日历、股东活动、公司信息流、基本面 |

Product Manifest 可以将部分观察定义为合法可选字段，但每个 Agent Manifest 列出的 Product 本身都是必需的。

## 不包含

- Bull/Bear、Trader、风险角色或 Portfolio Manager；它们属于 RE-011。
- Team 名称、风格描述或其他 Team Finding 进入 Agent Capsule。
- 隐式记忆或运行时修改 instructions。

## 验收条件

- [x] 七个 Agent 均能在 fixture Snapshot 上输出 Schema-valid Finding。
- [x] Finding 中每个事实性 claim 至少引用一个当前 Snapshot 内的 evidence ID。
- [x] Agent 无法引用未声明产品、未来 evidence 或不存在的 Artifact。
- [x] 两个 Teams 引用同一 Agent 版本和输入时只创建一个 Invocation/Finding。
- [x] 同一 Agent 若需要不同视角，必须使用不同 Agent ID/版本，而不是 Team 参数。
- [x] 任一必需 Agent 失败会阻断所有依赖它的 Teams，但不阻断无依赖 Team。
- [x] `a_share_core@1` 精确包含七个指定 Agent，Catalog 快照测试防止成员漂移。

## 可能触点

- `advisor/research/agents/runner.py`
- `config/research/agents/{market,social,news,fundamentals,policy,hot_money,lockup}/`
- `config/research/teams/a_share_core-v1.yaml`
- `tests/advisor/research/test_agent_runner.py`
- `tests/advisor/research/test_core_team.py`

## 验证

```bash
./.venv311/bin/python -m pytest tests/advisor/research/test_agent_runner.py tests/advisor/research/test_core_team.py
```
