---
id: MD-007
status: complete
depends_on: [MD-004, MD-005]
adrs: [0030, 0032, 0042]
---

# 实现复权因子与 Research Price Series

## 结果

复权因子独立于原始 K 线更新，Research Engine 能用固定因子哈希派生前复权价格，而实际成交价展示始终读取未复权行情。

## 范围

- 定义独立 `AdjustmentFactorProvider`、因子规范化算法版本和确定性内容哈希。
- 通过仓库自有来源适配器获得企业行为/调整观察：Eastmoney 为主源，TDX 的原始日线与除权除息记录为一次完整备源；一个来源必须独立生成完整因子结果，不与 Canonical Daily Bar 拼字段。
- 将每只股票的因子规范化为目标日期因子为 1 的前复权序列，并幂等写入 `market_adjustment_factors`。
- 因子可以独立更新；不创建原始 K 线版本历史，也不改写 `market_daily`。
- 提供纯查询函数，用原始 OHLC 与固定因子派生 Research Price Series。
- 快照输入记录因子集合哈希和算法版本；相同输入稳定复算。
- 用除权 fixture 验证原始价机械跳空不会变成错误收益率或技术信号。

## 不包含

- 在数据库中保存完整前复权 K 线副本。
- 把前复权价用于报告中的实际收盘价、支撑位或限价。
- Agent 或 Codex 参与复权计算。

## 验收条件

- [x] 原始 K 线在因子更新前后内容哈希完全不变。
- [x] 最新日期派生因子为 1，前复权 OHLC 保持合法边界。
- [x] 相同因子输入产生相同集合哈希和 Research Price Series。
- [x] 缺失、负值、非有限因子或跨日期不完整结果 fail closed。
- [x] 除权 fixture 的跨日收益连续，实际价查询仍返回原始价格。
- [x] 自动化测试完全离线。

## 可能触点

- `advisor/market_daily/adjustments.py`
- `advisor/market_daily/providers/adjustments.py`
- `advisor/market_daily/providers/tdx_adjustments.py`
- `advisor/market_daily/providers/adjustment_chain.py`
- `advisor/market_daily/repository.py`
- `tests/advisor/market_daily/test_adjustments.py`
- `tests/advisor/market_daily/test_schema.py`
- `tests/advisor/market_daily/test_research_prices.py`

## 验证

```bash
./.venv311/bin/python -m pytest \
  tests/advisor/market_daily/test_adjustments.py \
  tests/advisor/market_daily/test_schema.py \
  tests/advisor/market_daily/test_research_prices.py \
  tests/advisor/market_daily/test_tdx_adjustment_provider.py
```
