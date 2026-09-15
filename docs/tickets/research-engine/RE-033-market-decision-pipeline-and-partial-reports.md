---
id: RE-033
status: done
depends_on: [RE-011, RE-013, RE-023, RE-032]
adrs: [0008, 0022, 0075, 0076, 0086, 0095]
---

# 实现 Market Decision Pipeline 与 partial 报告

## 结果

所有 Market Teams 通过一个 engine-owned、版本化的 Market Decision Pipeline 生成 typed Market Insights；报告可明确区分全部通过、部分有效和整体不可发布，同时保留 Security Pipeline 的既有结论语义。

## 范围

- 建立 Scope 选择的固定 Pipeline contract：所有 Market Teams 使用同一 Market Pipeline，所有 Security Teams 使用既有 Security Pipeline；Team 不能自定义拓扑、模型、质量门或报告 schema。
- Market Team Conclusion 使用公共 evidence/quality/risks/invalidation/time-horizon envelope，内部包含一个或多个 typed Insights，而不是强制单一 Market Regime 或五级股票 stance。
- 首版 Insight kinds 至少覆盖 market breadth/sentiment/liquidity、first-level industry strength/rotation、macro/policy/market information；Pipeline schema 可版本化扩展。
- 保留通用 Research Candidate kind 供未来 Market Team 使用，但它只能包含 code、选择风格/方法、evidence-linked rationale、risks 和可选 rank；不得携带 Security Stance、目标价、仓位，且绝不自动触发 Security Request。
- `a_share_market_overview@1` 的 contracted Insights 明确禁用 Research Candidate 和所有 per-security conclusions。
- 每个 Insight 独立为 passed 或 blocked 并携带有限原因。全部通过为 `passed`；至少一项通过且至少一项因数据不足、timeout 或 schema-invalid 被阻断时发布 `partial` 报告；无可发布 Insight 或整体质量门失败为 `blocked` 且无报告。
- `failed` 只用于 orchestration、persistence 或 publication 使 coherent result 无法安全完成的情况。
- partial JSON/Markdown 明示每个 blocked Insight 及 cause class，不静默省略、不泄漏异常正文、prompt、日志或 chain-of-thought。
- 修订 repository operating rules，使“每个成员仍是 required”与 Market typed partial 语义一致：失败成员必须留下 blocked Insight，不得被悄悄移除；Security Conclusion 仍保持 indivisible/all-or-nothing。

## 不包含

- Team-specific Pipeline、跨 Team 聚合/比较、首页市场结论、真实交易或让模型决定 Research Subject。
- Engine-owned 统一行业强度公式或要求所有 Market Teams 输出同一种 Insight 组合。

## 验收条件

- [ ] 相同 Market Team 在固定 inputs 下只能走固定 Market Pipeline，无法通过 Manifest 改写 stages 或 schema。
- [ ] 3/3 Insights 通过产生 passed，2/3 或 1/3 通过产生 partial，0/3 或整体质量门失败产生 blocked 且无 report。
- [ ] 单 Agent timeout/schema-invalid 只阻断对应 Insight；persistence/publication 整体失败映射为 failed。
- [ ] partial Report 同时包含有效 Insights、每项 blocked cause、公共风险/失效条件和精确 provenance。
- [ ] Overview Team 输出任何 Candidate/per-security stance 时拒绝；通用 Candidate schema 也拒绝价格、仓位和自动 child request。
- [ ] 既有 Security Pipeline、五级 Decision Stance、Team 报告和 all-or-nothing 行为回归通过。

## 可能触点

- `advisor/research/decision/contracts.py`
- `advisor/research/decision/pipeline.py`
- `advisor/research/reporting/cycle.py`
- `advisor/research/state_machine.py`
- `config/research/pipelines/`
- `agents.md`
- `tests/advisor/research/test_market_decision_pipeline.py`
- `tests/advisor/research/test_team_reporting.py`
- `tests/advisor/research/test_decision_pipeline.py`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c './.venv311/bin/python -m pytest tests/advisor/research/test_market_decision_pipeline.py tests/advisor/research/test_decision_pipeline.py tests/advisor/research/test_team_reporting.py'
```
