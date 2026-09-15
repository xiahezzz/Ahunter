---
id: MD-010
status: complete
depends_on: [MD-008, MD-009]
adrs: [0037, 0038, 0044, 0045]
---

# 实现 21:00 增量补洞工作流

## 结果

冷启动完成后，Catch-up 能刷新股票池并按每只股票最后覆盖点补到最新 Observed Trading Session；正常只追加一天，停机或来源故障后自动补洞。

## 范围

- 只有已封印冷启动基线才能进入正常 Catch-up；否则恢复原冷启动。
- 每次运行先刷新 Observed Trading Sessions 和证券主数据，再确定目标 session。
- 最新 session 未变化时成功 no-op，不创建空的失败批次。
- 对每只当前应覆盖证券，从最后一根 K 线或合法缺席之后计算最小缺失区间。
- 新上市股票从上市日开始；已退市股票在退市日后停止；历史数据永不滚动删除。
- 前一日 `source_missing` 自动进入新区间；已完整股票不重复请求。
- Run 可以整体 `partial`，但单股 readiness 只受本股票覆盖影响；全市场扫描必须等待 Run `complete`。
- 所有时间决策使用 `Asia/Shanghai` 和持久化 Observed Trading Session。

## 不包含

- 进程常驻、内部计时器、LaunchAgent 或 UI。

## 验收条件

- [x] 正常日更只请求一个 session，连续停机三天后一次补齐全部缺口。
- [x] 周末/休市、来源故障和最新 session 冲突拥有不同结果。
- [x] 新上市与退市边界不会请求上市前或退市后日期。
- [x] 一个股票失败不阻塞其他股票的单股 readiness，但全市场 readiness 为 false。
- [x] 第二次执行已完成 Catch-up 是完全幂等的 no-op。
- [x] 自动化测试使用 fake clock 和 fixture Provider，不访问网络。

## 可能触点

- `advisor/market_daily/catch_up.py`
- `advisor/market_daily/readiness.py`
- `tests/advisor/market_daily/test_catch_up.py`
- `tests/advisor/market_daily/test_readiness.py`

## 验证

```bash
./.venv311/bin/python -m pytest \
  tests/advisor/market_daily/test_catch_up.py \
  tests/advisor/market_daily/test_readiness.py
```
