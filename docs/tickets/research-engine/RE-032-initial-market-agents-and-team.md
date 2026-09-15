---
id: RE-032
status: done
depends_on: [RE-024, RE-028, RE-029, RE-030, RE-031]
adrs: [0070, 0086, 0087, 0088, 0089]
---

# 发布三名初始 Market Agents 与 Overview Team

## 结果

Catalog 中出现三名可实际运行的 Market Agents 和首个 Market Team `a_share_market_overview@1`，分别覆盖市场广度情绪、一级行业轮动和宏观政策信息，且永不推荐个股。

## 范围

- 发布 `market_breadth@1`：分析全市场广度、交易情绪和流动性；声明 whole-market intraday、whole-market history 与 trading calendar 等最小必要产品。
- 发布 `sector_rotation@1`：分析一级行业强弱与轮动；在上述产品外声明 Industry Sector Taxonomy。
- 发布 `market_macro_policy@1`：分析宏观、政策、监管和市场信息对全市场的影响；声明 Market Information Product，并可读取必要的全市场行情背景。
- 三者均显式声明 Market Scope，使用与 Security Agents 相同的 Agent Manifest、Data Access、Query budget、Run Capsule、Codex Policy 和 Research Finding 核心契约。
- `sector_rotation@1` 自主选择分析字段、窗口、分组和排名方法；Finding 必须结构化披露 inputs、time windows、判断标准、排名/分组依据和 evidence refs，不输出私有推理。
- 三名 Agent 的 instructions 和 details schema 禁止 Research Candidate、股票推荐、Security Decision Stance、目标价、仓位或交易指令。
- 发布 `a_share_market_overview@1`，精确固定上述三个 `@1` Agent；不复用 Security-scoped `market@1` 或 `policy@1` 的稳定 ID。
- 为 whole-market rows 设置经测试的查询行数/字节预算，不靠把全部市场历史直接放进 prompt。

## 不包含

- 修改既有七个 Security Agents、引擎统一板块评分、候选股票、Team-specific Pipeline 或默认首页 Team。
- 允许 Agent 直接访问新浪、东方财富、官方网站、Advisor DB 或 repository workspace。

## 验收条件

- [ ] 三个 Agent 和 Team 能通过完整 Catalog 校验，Scope、Data Access、details schema 与精确成员引用符合预期。
- [ ] 每名 Agent 在离线 Snapshot/fake executor 下产生 evidence-linked Finding，且只能看到自己声明的产品。
- [ ] sector Finding 缺少方法摘要任一必填部分时 schema/quality gate 拒绝。
- [ ] 输出出现 Research Candidate、股票 stance、价格、仓位或交易动作时 fail closed。
- [ ] 同一 Agent 可被未来其他 Market Team 复用；同 Scope Team 生命周期与 Security Team 使用同一代码路径。
- [ ] 既有 `market@1`、`policy@1`、`a_share_core@1` 和 `normal@1` 内容与 Scope 解释不被改写。

## 可能触点

- `config/research/agents/market_breadth.yaml`
- `config/research/agents/sector_rotation.yaml`
- `config/research/agents/market_macro_policy.yaml`
- `config/research/teams/a_share_market_overview.yaml`
- `advisor/research/contracts.py`
- `advisor/research/agents/runner.py`
- `tests/advisor/research/test_market_agents.py`
- `tests/advisor/research/test_core_team.py`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c './.venv311/bin/python -m pytest tests/advisor/research/test_market_agents.py tests/advisor/research/test_core_team.py tests/advisor/research/test_catalog.py'
```
