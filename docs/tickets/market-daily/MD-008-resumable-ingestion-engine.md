---
id: MD-008
status: complete
depends_on: [MD-002, MD-003, MD-004, MD-005, MD-006, MD-007]
adrs: [0013, 0033, 0034, 0035, 0038, 0041, 0045]
---

# 实现可恢复 Market Daily 摄取引擎

## 结果

一个与冷启动、定时器和 UI 无关的引擎能按固定股票池执行逐证券摄取、短事务提交、有界回退、稀疏缺席记录、冲突阻断和崩溃恢复。

## 范围

- 定义显式 Run Item 状态机及 `ingest_security_interval` 深接口。
- 每只股票独立执行：计算区间、调用主源最多两次、调用备源最多一次、校验、写入 K 线与因子、记录终态。
- 单只股票使用短批量事务；已提交股票不因后续失败回滚，也不使用全市场巨型事务。
- 相同 K 线 no-op；不同内容标记 `conflicted` 并保留旧值，只有独立显式 repair 接口可以替换。
- `market_daily` 行推导 `traded`，上市区间推导未上市/已退市，仅持久化有证据的停牌缺席。
- 无法证明停牌且没有 K 线时写入 `source_missing` Run Item，不伪造零成交 K 线。
- 完整性按股票和全市场两个范围计算，并提供结构化质量结果。
- 在每个持久化边界注入崩溃，重启后只恢复未完成区间。

## 不包含

- 五年窗口选择、21:00 时钟、长驻循环、LaunchAgent 或 WebUI。

## 验收条件

- [x] 主源成功、主源重试成功、备源成功、全部失败、停牌和冲突路径全部覆盖。
- [x] 一个股票失败不回滚其他股票，Run 最终准确成为 `partial`。
- [x] 重启后成功股票与日期不再请求，未完成股票从持久化游标继续。
- [x] 冲突不会覆盖旧行情，显式 repair 有独立审计记录和测试。
- [x] 全市场 readiness 与单股 readiness 对同一 partial run 返回不同、正确结果。
- [x] 并发执行仍满足单代码日期唯一和 Run Item 单所有者约束。

## 可能触点

- `advisor/market_daily/engine.py`
- `advisor/market_daily/repository.py`
- `tests/advisor/market_daily/test_engine.py`
- `tests/advisor/market_daily/test_engine_recovery.py`
- `tests/advisor/market_daily/test_provider_chain.py`
- `tests/advisor/market_daily/test_readiness.py`

## 验证

```bash
./.venv311/bin/python -m pytest \
  tests/advisor/market_daily/test_engine.py \
  tests/advisor/market_daily/test_engine_recovery.py \
  tests/advisor/market_daily/test_provider_chain.py \
  tests/advisor/market_daily/test_readiness.py
```
