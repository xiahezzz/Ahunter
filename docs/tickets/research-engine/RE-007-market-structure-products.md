---
id: RE-007
status: done
depends_on: [RE-003]
adrs: [0012, 0013, 0018, 0024]
---

# 实现资金、题材、龙虎榜与解禁 Data Products

## 结果

Hot Money 和 Lockup Agents 能查询结构化的热点题材、个股资金、北向观察、龙虎榜、概念归属和限售解禁信息，并能区分“没有事件”和“来源不可用”。

## 范围

- 定义并版本化热点股票、市场资金观察、证券概念/行业、个股资金流、龙虎榜和解禁日历产品。
- 将当前公开 A 股端点的解析、节流和重试逻辑收进 A Hunter Provider Adapters。
- 资金类产品统一方向、金额单位、采样频率和交易日；龙虎榜保留上榜原因、席位类型、买卖方向及净额。
- 解禁产品包含历史记录和明确窗口内的未来计划，并记录数量、占比、计划日期和公告时间。
- 产品合同明确最小必需部分与可选观察；来源暂时不提供某个可选字段时留下可见质量标记。
- 合法空集合表示已成功查询且窗口内无记录，不得被当成抓取失败。

## 不包含

- 从榜单或资金数字直接产生交易动作。
- 由 Agent 直接调用端点。
- 静默合并冲突数值。

## 验收条件

- [x] 所有时间序列遵守交易日和 `as_of`，未来计划只在公告已可见时出现。
- [x] “无龙虎榜/无解禁”和“来源失败”拥有不同状态及测试。
- [x] 资金方向、金额单位和席位净额在规范化后可做确定性校验。
- [x] 来源节流在多个产品并发构建时仍共享限制，不产生请求风暴。
- [x] fixture tests 覆盖热点、概念、资金、龙虎榜、解禁、空结果和格式漂移。

## 可能触点

- `advisor/research/providers/market_structure/`
- `config/research/products/market_structure/`
- `tests/advisor/research/providers/test_market_structure_products.py`

## 验证

```bash
./.venv311/bin/python -m pytest tests/advisor/research/providers/test_market_structure_products.py
```
