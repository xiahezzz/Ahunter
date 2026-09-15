---
id: MD-013
status: complete
depends_on: [MD-006, MD-007, MD-008, MD-010]
adrs: [0012, 0013, 0018, 0038, 0042, 0044]
---

# 将 Market Daily 接入 Research Engine

## 结果

Research Engine 只从经过质量门的本地 Market Daily 基线构建日线产品：实际价使用原始 K 线，跨日收益和技术指标使用固定因子哈希派生的前复权序列。

## 范围

- 调整 `LocalMarketProvider` 和 `market_daily_bars@1` 契约，读取新 Schema、单位、交易日和 readiness。
- 单股 Product Request 只要求该股票窗口完整；全市场扫描要求目标 Ingestion Run `complete`。
- 数据产品同时明确区分 raw rows 与 Research Price Series，不让 Agent 自行决定复权方式。
- 技术指标确定性使用前复权 OHLC；报告实际收盘价、K 线图和价格位使用未复权 OHLC。
- Research Data Snapshot 固定原始行哈希、复权因子集合哈希、算法版本和 Observed Trading Session 证明。
- 本地数据缺失或冲突时 fail closed，不在 Research Run 中临时直连公开行情补洞。
- 移除对旧 Sina 800 行、`amount=0` 和硬编码交易日范围的依赖。
- 更新 Market、Hot Money 等依赖日线的 Agents，保持它们只查询声明的数据产品。

## 不包含

- 改变 Agent 团队成员、Decision Pipeline、Team 结论或报告语言。
- 全市场选股算法本身。

## 验收条件

- [x] 单股完整、其他股票缺失时单股产品可用；全市场产品被阻断。
- [x] 同一 Snapshot 的前复权指标稳定复算，且记录因子集合哈希。
- [x] 图表和报告价格保持原始实际价，收益率不会把除权跳空当作暴跌。
- [x] 来源冲突、缺失因子、未来数据或 incomplete session 均阻断对应产品。
- [x] Research Run 不发生网络行情抓取，也不写 Market Daily 表。
- [x] 现有 Research Engine 端到端 fixture 在新契约下通过。

## 可能触点

- `advisor/research/providers/local.py`
- `advisor/research/providers/market.py`
- `advisor/research/data_products/engine.py`
- `config/research/products/market_daily_bars.yaml`
- `advisor/charts/kline.py`
- `tests/advisor/research/providers/test_local_market.py`
- `tests/advisor/research/providers/test_market_products.py`

## 验证

```bash
./.venv311/bin/python -m pytest \
  tests/advisor/research/providers/test_local_market.py \
  tests/advisor/research/providers/test_market_products.py \
  tests/advisor/research/test_end_to_end.py
```
