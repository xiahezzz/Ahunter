---
id: RE-004
status: done
depends_on: [RE-003]
adrs: [0012, 0013, 0018, 0024]
---

# 实现行情与技术 Data Products

## 结果

Market Agent 和其他需要量价上下文的 Agents 能从 A Hunter 自有适配器获得结构化、带 `as_of` 的证券身份、交易日历、日线、当前行情和技术指标产品。

## 范围

- 定义并版本化至少以下产品：证券身份、A 股交易日历、日线 OHLCV、行情快照、技术指标集合。
- 复用现有 `market_daily` 与 Sina 实现；将所需 mootdx、腾讯和新浪逻辑收进 A Hunter Provider Adapters，不导入外部仓库。
- 技术指标作为日线的确定性派生产品，记录输入日线 Artifact 哈希和算法版本。
- 明确复权、单位、交易所、停牌、涨跌停、成交额、缺口和合法空值语义。
- 通过产品策略配置来源顺序、节流、新鲜度和覆盖窗口。
- 所有网络响应先经 fixture 化解析器，再进入规范 Product Schema。

## 不包含

- 基本面、新闻、资金流或 Agent 提示词。
- 在 Codex 中计算指标。
- 在测试中访问实时行情。

## 验收条件

- [x] 所有记录均带来源时间、抓取时间、`as_of`、Schema 版本和内容哈希。
- [x] 日线按交易日唯一且有序，拒绝未来行、非有限数值和 OHLC 逻辑错误。
- [x] 技术指标只使用 `as_of` 当时可见的日线，并能从相同输入稳定重算。
- [x] 本地已有数据满足质量条件时可复用；不足时按产品策略补齐，不原地污染 sealed Snapshot。
- [x] 来源失败、停牌、无交易日和空历史拥有不同且测试覆盖的质量结果。
- [x] 生产代码中不存在对 `/Users/mac/Documents/TradingAgents-astock` 的引用。

## 可能触点

- `advisor/data_sources/contracts.py`
- `advisor/data_sources/free_sources.py`
- `advisor/research/providers/market.py`
- `advisor/research/providers/local.py`
- `config/research/products/market/`
- `tests/advisor/research/providers/test_market_products.py`

## 验证

```bash
./.venv311/bin/python -m pytest tests/advisor/test_free_sources.py tests/advisor/test_market_backfill.py tests/advisor/research/providers/test_market_products.py
```
