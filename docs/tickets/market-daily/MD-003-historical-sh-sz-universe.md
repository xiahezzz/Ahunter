---
id: MD-003
status: complete
depends_on: [MD-001]
adrs: [0029, 0031, 0032, 0036]
---

# 建立沪深历史股票池

## 结果

Market Daily Service 能从沪深交易所公开资料构建一个带上市区间、包含五年窗口内退市股票且不含其他证券类型的确定性股票池。

## 范围

- 为上海和深圳交易所分别实现仓库自有 Security Universe Adapter 和 fixture 化解析器。
- 采集当前上市与已退市普通 A 股，规范代码、名称、交易所、上市日、退市日、ST 状态和来源时间。
- 以“上市区间与请求窗口有交集”选择冷启动股票，避免只使用当前列表造成幸存者偏差。
- 明确排除北交所、B 股、CDR、ETF、基金、债券、可转债、权证和指数。
- 对同一代码的跨页面信息做确定性校验；交易所、证券类型或上市区间冲突时阻断股票池封印。
- 生成稳定排序的 Universe Snapshot 和内容哈希，并将证券主数据幂等写入 `securities`。
- 每日刷新只增加新上市、更新明确状态或写入退市日，不删除历史证券。

## 不包含

- K 线、复权因子、交易日识别或冷启动编排。
- 从行情列表猜测历史退市股票。

## 验收条件

- [x] fixture 同时覆盖当前上市、ST、停牌、窗口内退市、窗口前退市和新上市股票。
- [x] 同一输入无论页面顺序如何都生成相同股票池和哈希。
- [x] 任何北交所或非普通 A 股 fixture 都不会进入股票池。
- [x] 缺失上市日、非法区间和交易所冲突 fail closed。
- [x] 每日刷新不会删除或改写已保存的历史上市区间事实。
- [x] 自动化测试完全离线。

## 可能触点

- `advisor/market_daily/providers/exchanges.py`
- `advisor/market_daily/universe.py`
- `tests/advisor/market_daily/test_universe.py`

## 验证

```bash
./.venv311/bin/python -m pytest tests/advisor/market_daily/test_universe.py
```
