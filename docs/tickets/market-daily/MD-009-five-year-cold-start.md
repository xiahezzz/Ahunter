---
id: MD-009
status: complete
depends_on: [MD-008]
adrs: [0029, 0031, 0033, 0040, 0046]
---

# 实现五年全市场冷启动工作流

## 结果

一个显式 Cold Start Request 能冻结目标交易日、五个自然年窗口和历史沪深股票池，逐股构建可恢复的全市场基线，并且只在完整覆盖后封印为 `complete`。

## 范围

- 以最近一个 Observed Trading Session `D` 为目标，计算闭区间 `[D - 5个自然年, D]`，非交易日起点前移到首个观察交易日。
- 冻结与窗口有上市区间交集的 Historical Shanghai-Shenzhen A-share Universe 及其哈希；运行中股票池变化不修改当前 Run。
- 为每只证券创建有界区间 Run Item，调用 MD-008 引擎并持续更新进度。
- 窗口前未上市、窗口内退市和有证据停牌均按已确认语义计算覆盖。
- 物理逐股提交；任何 `source_missing` 或 `conflicted` 使 Run 保持 `partial`，不得发布全市场基线。
- 重复提交相同目标日返回同一 Run；服务重启继续未完成项目。
- 完成后保留全部历史，后续不因超过五年而删除。
- 提供只作用于行情表的 legacy reset 操作，但不在自动化测试或普通启动时触发真实清理。

## 不包含

- 21:00 日常补洞、Service 常驻循环、LaunchAgent、WebUI 或真实数据库清理。

## 验收条件

- [x] 窗口计算覆盖闰年、非交易日起点和不同时区输入。
- [x] 当前上市、窗口内退市、窗口后新上市、ST 和停牌 fixture 的 Run Item 边界正确。
- [x] 冷启动只有在所有证券达到有证据终态时变为 `complete`。
- [x] partial Run 重放只处理失败或缺失部分，已完成数据哈希不变。
- [x] 股票池冻结后新增证券不会进入当前 Run，而会留给后续 Catch-up。
- [x] reset 操作有精确目标表测试，绝不删除账本、报告或研究控制面数据。

## 可能触点

- `advisor/market_daily/cold_start.py`
- `advisor/market_daily/repository.py`
- `tests/advisor/market_daily/test_cold_start.py`
- `tests/advisor/market_daily/test_market_reset.py`

## 验证

```bash
./.venv311/bin/python -m pytest \
  tests/advisor/market_daily/test_cold_start.py \
  tests/advisor/market_daily/test_market_reset.py
```
